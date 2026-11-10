"""Consistency Analysis Agent: so sánh PA với CA (kiểm tra nhất quán cấu trúc).

Ý tưởng theo Software Reflexion Model (Murphy, Notkin, Sullivan): nâng đồ thị mã nguồn lên mức
component qua ánh xạ module -> component, rồi so với PA để tìm
  - divergence : phụ thuộc có trong mã nhưng không được PA cho phép;
  - absence    : phụ thuộc PA dự kiến nhưng không thấy trong mã.
Thêm: chu trình phụ thuộc, module chưa ánh xạ, component không có mã.
Agent này KHÔNG dùng LLM; kết quả xác định và tái lập được.
"""
import hashlib
import itertools

import networkx as nx

from ..graph_utils import graph_from_dict
from ..models import (ConsistencyResult, ConsistencyTask, Evidence, Violation)

SEVERITY_ORDER = {'high': 0, 'medium': 1, 'low': 2, 'info': 3}
LIFTED_RELS = ('imports', 'calls', 'inherits')


def _vid(*parts) -> str:
    return hashlib.sha256('|'.join(str(p) for p in parts).encode('utf8')).hexdigest()[:12]


class _ComponentMapper:
    def __init__(self, components):
        pairs = []
        for c in components:
            for p in c.module_prefixes:
                p = p.strip().rstrip('*').rstrip('.')
                if p:
                    pairs.append((p, c.name))
        self.pairs = sorted(pairs, key=lambda x: -len(x[0]))   # khớp tiền tố dài nhất trước

    def component_of(self, module: str) -> str | None:
        for p, name in self.pairs:
            if module == p or module.startswith(p + '.'):
                return name
        return None


class ConsistencyAnalysisAgent:
    MAX_CYCLES = 50
    MAX_UNMAPPED = 100

    def run(self, task: ConsistencyTask) -> ConsistencyResult:
        pa, ca = task.pa, task.ca
        G = graph_from_dict(ca.graph)
        mapper = _ComponentMapper(pa.components)
        allowed = {tuple(p) for p in pa.allowed_dependencies}

        module_comp = {n: mapper.component_of(n)
                       for n, d in G.nodes(data=True) if d.get('kind') == 'module'}

        def module_of(n):
            d = G.nodes[n]
            return n if d.get('kind') == 'module' else d.get('module')

        # ---- nâng cạnh lên mức component ----
        observed: dict[tuple[str, str], list[Evidence]] = {}
        for u, v, d in G.edges(data=True):
            rel = d.get('rel')
            if rel not in LIFTED_RELS:
                continue
            cu, cv = module_comp.get(module_of(u)), module_comp.get(module_of(v))
            if cu is None or cv is None or cu == cv:
                continue
            mu = module_of(u)
            observed.setdefault((cu, cv), []).append(Evidence(
                rel=rel, source=u, target=v, line=d.get('line'),
                file=G.nodes[mu].get('file') if mu in G else None,
                confidence='heuristic' if d.get('confidence') == 'heuristic' else 'high'))

        violations: list[Violation] = []

        # ---- divergence ----
        for (a, b), ev in observed.items():
            if (a, b) in allowed:
                continue
            has_import = any(e.rel == 'imports' for e in ev)
            violations.append(Violation(
                id=_vid('forbidden', a, b), type='forbidden_dependency',
                severity='high' if has_import else 'medium',
                from_component=a, to_component=b,
                confidence='high' if has_import else 'heuristic',
                message=f'Component {a} phụ thuộc {b} nhưng PA không cho phép ({len(ev)} bằng chứng)',
                evidence=sorted(ev, key=lambda e: (e.rel != 'imports', e.source, e.target))[:20]))

        # ---- absence ----
        mapped_components = {c for c in module_comp.values() if c}
        for c in pa.components:
            if c.name not in mapped_components:
                violations.append(Violation(
                    id=_vid('nocode', c.name), type='component_without_code', severity='medium',
                    from_component=c.name,
                    message=f'Component {c.name} có trong PA nhưng không có module nào ánh xạ tới'))
        for (a, b) in sorted(allowed):
            if (a, b) not in observed and a in mapped_components and b in mapped_components:
                violations.append(Violation(
                    id=_vid('missing', a, b), type='missing_dependency', severity='low',
                    from_component=a, to_component=b,
                    message=f'PA dự kiến {a} → {b} nhưng không thấy phụ thuộc này trong mã nguồn'))

        # ---- chu trình ở mức component ----
        D = nx.DiGraph(list(observed.keys()))
        seen_cycles = set()
        for cyc in itertools.islice(nx.simple_cycles(D), self.MAX_CYCLES):
            i = cyc.index(min(cyc))
            cyc = cyc[i:] + cyc[:i]
            key = tuple(cyc)
            if key in seen_cycles:
                continue
            seen_cycles.add(key)
            ev = []
            for x, y in zip(cyc, cyc[1:] + cyc[:1]):
                es = sorted(observed[(x, y)], key=lambda e: e.rel != 'imports')
                ev.append(es[0])
            violations.append(Violation(
                id=_vid('cycle', *cyc), type='dependency_cycle', severity='medium',
                from_component=cyc[0], to_component=cyc[-1],
                confidence='high' if all(e.rel == 'imports' for e in ev) else 'heuristic',
                message='Chu trình phụ thuộc giữa các component: ' + ' → '.join(cyc + [cyc[0]]),
                evidence=ev))

        # ---- module chưa ánh xạ ----
        unmapped = sorted(m for m, c in module_comp.items() if c is None)
        for m in unmapped[:self.MAX_UNMAPPED]:
            violations.append(Violation(
                id=_vid('unmapped', m), type='unmapped_module', severity='info', from_component=None,
                message=f'Module {m} chưa được ánh xạ tới component nào của PA'))

        violations.sort(key=lambda v: (SEVERITY_ORDER[v.severity], v.type, v.id))
        by_type: dict[str, int] = {}
        for v in violations:
            by_type[v.type] = by_type.get(v.type, 0) + 1
        summary = {'total': len(violations), 'by_type': by_type,
                   'lifted_component_edges': len(observed), 'unmapped_modules': len(unmapped)}
        return ConsistencyResult(status='partial' if ca.status == 'partial' else 'ok',
                                 violations=violations, summary=summary)
