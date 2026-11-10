"""Source Code Analysis Agent.

Nhiệm vụ: phân tích mã nguồn Python bằng Tree-sitter, trích module/class/function và quan hệ
phụ thuộc, dựng đồ thị NetworkX (MultiDiGraph).

Tree-sitter chỉ cho cú pháp. Các phần sau do Agent này TỰ xây dựng (và có giới hạn, xem README):
  - quy đổi đường dẫn file -> tên module; phân giải import tuyệt đối và tương đối trong dự án;
  - phân giải lời gọi hàm/kế thừa theo *tên* (heuristic, không suy luận kiểu).

Cạnh trong đồ thị (thuộc tính 'rel'): contains | imports | inherits | calls
"""
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
import tree_sitter_python as tspy
from tree_sitter import Language, Parser

from ..graph_utils import graph_to_dict
from ..models import CodeAnalysisResult, CodeAnalysisTask

_PARSER = None


def _parser() -> Parser:
    global _PARSER
    if _PARSER is None:
        _PARSER = Parser(Language(tspy.language()))
    return _PARSER


def _t(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode('utf8', errors='replace')


@dataclass
class ModuleSource:
    src: bytes
    is_package: bool
    file: str          # đường dẫn tương đối so với project_path


# --------------------------------------------------------------------------------------
# Bước 1: trích "dữ kiện" (facts) từ cây cú pháp của một file
# --------------------------------------------------------------------------------------
def extract_facts(module: str, src: bytes, is_package: bool = False) -> dict:
    tree = _parser().parse(src)
    facts = {'module': module, 'is_package': is_package, 'imports': [], 'classes': [],
             'functions': [], 'calls': [], 'has_error': tree.root_node.has_error}

    def walk(node, cls=None, func=None):
        t = node.type
        if t == 'import_from_statement':
            mod = node.child_by_field_name('module_name')
            if mod is not None:
                names = []
                for n in node.children_by_field_name('name'):
                    if n.type == 'aliased_import':
                        n = n.child_by_field_name('name')
                    if n is not None and n.type == 'dotted_name':
                        names.append(_t(n, src))
                facts['imports'].append({'target': _t(mod, src), 'names': names,
                                         'relative': mod.type == 'relative_import',
                                         'line': node.start_point[0] + 1})
        elif t == 'import_statement':
            for c in node.named_children:
                n = c.child_by_field_name('name') if c.type == 'aliased_import' else c
                if n is not None and n.type == 'dotted_name':
                    facts['imports'].append({'target': _t(n, src), 'names': [], 'relative': False,
                                             'line': node.start_point[0] + 1})
        elif t == 'class_definition' and func is None:
            name = _t(node.child_by_field_name('name'), src)
            sup = node.child_by_field_name('superclasses')
            bases = []
            if sup is not None:
                for b in sup.named_children:
                    if b.type in ('identifier', 'attribute'):
                        bases.append(_t(b, src).split('.')[-1])
            facts['classes'].append({'name': name, 'line': node.start_point[0] + 1, 'bases': bases})
            for ch in node.children:
                walk(ch, cls=name, func=None)
            return
        elif t == 'function_definition':
            if func is None:   # hàm lồng nhau không đăng ký riêng; lời gọi bên trong tính cho hàm bao ngoài
                name = _t(node.child_by_field_name('name'), src)
                qual = f'{cls}.{name}' if cls else name
                facts['functions'].append({'qualname': qual, 'cls': cls, 'line': node.start_point[0] + 1})
                for ch in node.children:
                    walk(ch, cls=cls, func=qual)
                return
        elif t == 'call' and func is not None:
            f = node.child_by_field_name('function')
            callee = None
            if f is not None and f.type == 'identifier':
                callee = _t(f, src)
            elif f is not None and f.type == 'attribute':
                attr = f.child_by_field_name('attribute')
                callee = _t(attr, src) if attr is not None else None
            if callee:
                facts['calls'].append({'caller': func, 'callee': callee, 'line': node.start_point[0] + 1})
        for ch in node.children:
            walk(ch, cls, func)

    walk(tree.root_node)
    return facts


# --------------------------------------------------------------------------------------
# Bước 2: dựng đồ thị từ dữ kiện của toàn dự án
# --------------------------------------------------------------------------------------
def _resolve_relative(module: str, is_package: bool, spec: str) -> str | None:
    dots = len(spec) - len(spec.lstrip('.'))
    rest = spec[dots:]
    pkg = module.split('.') if is_package else module.split('.')[:-1]
    up = dots - 1
    if up > len(pkg):
        return None
    base = pkg[:len(pkg) - up]
    return '.'.join(base + ([rest] if rest else []))


def _is_dunder(name: str) -> bool:
    return name.startswith('__') and name.endswith('__')


def _pick(cands: list[tuple[str, str]], caller_module: str, imported: set[str]) -> str | None:
    """Chọn 1 ứng viên cho lời gọi; ưu tiên cùng module, rồi module đã import. None nếu mơ hồ."""
    if len(cands) == 1:
        return cands[0][0]
    local = [c for c in cands if c[1] == caller_module]
    if len(local) == 1:
        return local[0][0]
    imp = [c for c in cands if c[1] in imported]
    if len(imp) == 1:
        return imp[0][0]
    return None


def build_graph(project: dict[str, ModuleSource]):
    G = nx.MultiDiGraph()
    unresolved: list[dict] = []
    externals: set[str] = set()
    facts = {m: extract_facts(m, s.src, s.is_package) for m, s in project.items()}

    index: dict[str, list[tuple[str, str]]] = {}   # tên ngắn -> [(node_id, module)]
    for m, f in facts.items():
        G.add_node(m, kind='module', file=project[m].file)
        for c in f['classes']:
            cid = f'{m}.{c["name"]}'
            G.add_node(cid, kind='class', module=m, line=c['line'])
            G.add_edge(m, cid, rel='contains')
            index.setdefault(c['name'], []).append((cid, m))
        for fn in f['functions']:
            fid = f'{m}.{fn["qualname"]}'
            G.add_node(fid, kind='function', module=m, line=fn['line'])
            owner = f'{m}.{fn["cls"]}' if fn['cls'] else m
            G.add_edge(owner, fid, rel='contains')
            short = fn['qualname'].split('.')[-1]
            if not _is_dunder(short):
                index.setdefault(short, []).append((fid, m))

    imported_modules: dict[str, set[str]] = {}
    for m, f in facts.items():
        targets: set[str] = set()
        for imp in f['imports']:
            spec = imp['target']
            if imp['relative']:
                base = _resolve_relative(m, f['is_package'], spec)
                if base is None:
                    unresolved.append({'module': m, 'target': spec, 'line': imp['line'],
                                       'reason': 'import tương đối vượt quá gốc dự án'})
                    continue
            else:
                base = spec
            hit = False
            if base and base in facts and base != m:
                G.add_edge(m, base, rel='imports', line=imp['line'])
                targets.add(base)
                hit = True
            for name in imp['names']:
                sub = f'{base}.{name}' if base else name
                if sub in facts and sub != m:
                    G.add_edge(m, sub, rel='imports', line=imp['line'])
                    targets.add(sub)
                    hit = True
            if not hit:
                if imp['relative']:
                    unresolved.append({'module': m, 'target': spec, 'line': imp['line'],
                                       'reason': 'import tương đối không khớp module trong dự án'})
                elif base:
                    externals.add(base.split('.')[0])
        imported_modules[m] = targets

    for m, f in facts.items():
        for c in f['classes']:
            cid = f'{m}.{c["name"]}'
            for b in c['bases']:
                cands = index.get(b, [])
                cands = [x for x in cands if G.nodes[x[0]].get('kind') == 'class']
                picked = _pick(cands, m, imported_modules[m]) if cands else None
                if picked and picked != cid:
                    G.add_edge(cid, picked, rel='inherits', line=c['line'], confidence='heuristic')
        for call in f['calls']:
            cands = index.get(call['callee'], [])
            if not cands:
                continue   # thư viện chuẩn/ngoài dự án
            picked = _pick(cands, m, imported_modules[m])
            caller_id = f'{m}.{call["caller"]}'
            if picked is None:
                unresolved.append({'module': m, 'callee': call['callee'], 'line': call['line'],
                                   'reason': 'lời gọi mơ hồ (nhiều ứng viên cùng tên)'})
            elif picked != caller_id:
                G.add_edge(caller_id, picked, rel='calls', line=call['line'], confidence='heuristic')

    parse_errors = sorted(project[m].file for m, f in facts.items() if f['has_error'])
    return G, unresolved, sorted(externals), parse_errors


# --------------------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------------------
class SourceCodeAnalysisAgent:
    def run(self, task: CodeAnalysisTask) -> CodeAnalysisResult:
        root = Path(task.project_path)
        if not root.is_dir():
            return CodeAnalysisResult(status='failed', retryable=False,
                                      error=f'Không tìm thấy thư mục dự án: {root}')
        project, read_errors = self._collect(task, root)
        if not project:
            return CodeAnalysisResult(status='failed', retryable=False,
                                      error='Không tìm thấy file .py nào để phân tích')

        G, unresolved, externals, parse_errors = build_graph(project)
        parse_errors = sorted(set(parse_errors + read_errors))

        kinds, rels = {}, {}
        for _, d in G.nodes(data=True):
            kinds[d['kind']] = kinds.get(d['kind'], 0) + 1
        for _, _, d in G.edges(data=True):
            rels[d['rel']] = rels.get(d['rel'], 0) + 1
        stats = {'files': len(project), 'nodes': G.number_of_nodes(), 'edges': G.number_of_edges(),
                 'nodes_by_kind': kinds, 'edges_by_rel': rels, 'unresolved': len(unresolved)}
        return CodeAnalysisResult(status='partial' if parse_errors else 'ok',
                                  graph=graph_to_dict(G), stats=stats, parse_errors=parse_errors,
                                  unresolved=unresolved, externals=externals)

    @staticmethod
    def _collect(task: CodeAnalysisTask, root: Path):
        excludes = set(task.exclude_dirs)
        project: dict[str, ModuleSource] = {}
        read_errors: list[str] = []
        for r in task.source_roots:
            base = root / r
            if not base.is_dir():
                continue
            for p in sorted(base.rglob('*.py')):
                rel = p.relative_to(base)
                if any(part in excludes for part in rel.parts[:-1]):
                    continue
                parts = list(rel.with_suffix('').parts)
                is_pkg = parts[-1] == '__init__'
                if is_pkg:
                    parts = parts[:-1]
                if not parts:
                    continue
                module = '.'.join(parts)
                if module in project:
                    continue
                file_rel = str(p.relative_to(root))
                try:
                    project[module] = ModuleSource(p.read_bytes(), is_pkg, file_rel)
                except OSError:
                    read_errors.append(file_rel)
        return project, read_errors
