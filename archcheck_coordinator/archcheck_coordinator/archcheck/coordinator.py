"""Coordinator Agent (LangGraph, mô hình hub-and-spoke).

Coordinator CHỈ: nhận yêu cầu, giữ State chung, quyết định thứ tự theo phụ thuộc dữ liệu,
giao Task, kiểm tra *hình thức* của Result, quyết định thử lại/dừng, tổng hợp báo cáo.
Coordinator KHÔNG import tree_sitter, không phân tích mã/UML, không tự phán đoán vi phạm.

Luồng:  START -> coordinator <-> {arch_agent, code_agent, consistency_agent, recommend_agent}
        coordinator -> finalize -> END
Các Agent không gọi nhau; mọi chuyển giao dữ liệu đi qua State do Coordinator quản lý.
(Bản này chạy tuần tự; arch_agent và code_agent độc lập nên có thể song song hóa sau.)
"""
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

from .graph_utils import nodes_of
from .models import (DEFAULT_EXCLUDES, AnalysisState, ArchitectureResult, ArchitectureTask,
                     CodeAnalysisResult, CodeAnalysisTask, ConsistencyResult, ConsistencyTask,
                     RecommendationResult, RecommendationTask)
from .storage import Storage

# (nút, khóa trong State, model kiểm tra). Thứ tự = thứ tự phụ thuộc dữ liệu.
STEPS = [
    ('arch_agent', 'pa_result', ArchitectureResult),
    ('code_agent', 'ca_result', CodeAnalysisResult),
    ('consistency_agent', 'consistency_result', ConsistencyResult),
    ('recommend_agent', 'recommendation_result', RecommendationResult),
]

NodeName = Literal['arch_agent', 'code_agent', 'consistency_agent', 'recommend_agent', 'finalize']


@dataclass
class AgentSet:
    """Bốn Agent chuyên môn; mỗi Agent có phương thức run(task) -> result."""
    architecture: Any
    source_code: Any
    consistency: Any
    recommendation: Any


@dataclass
class CoordinatorConfig:
    max_retry: int = 2                    # số lần THỬ LẠI tối đa cho mỗi bước (tổng lần giao = max_retry + 1)
    max_parse_error_ratio: float = 0.2    # quá ngưỡng file lỗi cú pháp -> CA bị từ chối
    exclude_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    source_roots: list[str] = field(default_factory=lambda: ['.'])

    def recursion_limit(self) -> int:
        return 2 * len(STEPS) * (self.max_retry + 1) + 10


# --------------------------------------------------------------------------------------
# Kiểm tra hình thức kết quả
# --------------------------------------------------------------------------------------
def check_result(node: str, model, raw, cfg: CoordinatorConfig) -> tuple[bool, bool, str]:
    """Trả về (hợp_lệ, có_thể_thử_lại, lý_do)."""
    if raw is None:
        return False, True, 'không có kết quả'
    try:
        res = model.model_validate(raw)
    except ValidationError as exc:
        return False, True, f'kết quả sai schema ({exc.error_count()} lỗi)'
    if res.status == 'failed':
        return False, res.retryable, res.error or 'Agent báo failed'
    if node == 'arch_agent' and not res.components:
        return False, False, 'PA không có component'
    if node == 'code_agent':
        if not nodes_of(res.graph):
            return False, False, 'đồ thị CA rỗng'
        files = max(int(res.stats.get('files', 0)), 1)
        ratio = len(res.parse_errors) / files
        if ratio > cfg.max_parse_error_ratio:
            return False, False, f'quá nhiều file lỗi cú pháp ({ratio:.0%} > {cfg.max_parse_error_ratio:.0%})'
    return True, False, ''


def collect_warnings(state: AnalysisState) -> list[str]:
    w: list[str] = []
    ca = state.get('ca_result') or {}
    if ca.get('status') == 'partial':
        w.append(f'CA chưa đầy đủ: {len(ca.get("parse_errors", []))} file có lỗi cú pháp; '
                 'kết quả "không có vi phạm" không đảm bảo kiến trúc hoàn toàn nhất quán.')
    if ca.get('unresolved'):
        w.append(f'{len(ca["unresolved"])} import/lời gọi không phân giải được (giới hạn của phân tích tĩnh theo tên).')
    w.extend((state.get('pa_result') or {}).get('warnings', []))
    return w


# --------------------------------------------------------------------------------------
# Nút Coordinator
# --------------------------------------------------------------------------------------
def make_coordinator(cfg: CoordinatorConfig):
    def coordinator(state: AnalysisState) -> Command[NodeName]:
        retries = dict(state.get('retries') or {})
        for node, key, model in STEPS:
            attempts = retries.get(node, 0)
            if attempts == 0:                      # chưa giao bước này lần nào
                retries[node] = 1
                return Command(update={'retries': retries, 'stage': node,
                                       'trace': [f'coordinator -> {node} (lần 1)']}, goto=node)
            ok, retryable, reason = check_result(node, model, state.get(key), cfg)
            if ok:
                continue
            if retryable and attempts <= cfg.max_retry:
                retries[node] = attempts + 1
                return Command(update={'retries': retries, 'stage': node,
                                       'trace': [f'coordinator -> {node} (lần {attempts + 1}; lý do: {reason})']},
                               goto=node)
            return Command(update={'status': 'failed', 'failed_step': node,
                                   'errors': [{'agent': node, 'error': reason, 'final': True}],
                                   'trace': [f'coordinator: dừng ở {node}: {reason}']},
                           goto='finalize')
        return Command(update={'status': 'done', 'stage': 'finalize',
                               'trace': ['coordinator: mọi bước hợp lệ -> finalize']}, goto='finalize')
    return coordinator


# --------------------------------------------------------------------------------------
# Nút worker: bọc Agent, bắt ngoại lệ, KHÔNG chứa logic chuyên môn
# --------------------------------------------------------------------------------------
def _worker(name: str, key: str, build_task, call):
    def node(state: AnalysisState):
        attempt = (state.get('retries') or {}).get(name, 1)
        try:
            result = call(build_task(state, attempt))
            data = result.model_dump()
            update = {key: data, 'trace': [f'{name}: {result.status}']}
            if result.status == 'failed':
                update['errors'] = [{'agent': name, 'error': result.error, 'attempt': attempt}]
            return update
        except Exception as exc:   # ngoại lệ không lường trước -> coi là có thể thử lại
            msg = f'{type(exc).__name__}: {exc}'
            return {key: {'status': 'failed', 'error': msg, 'retryable': True},
                    'errors': [{'agent': name, 'error': msg, 'attempt': attempt}],
                    'trace': [f'{name}: ngoại lệ']}
    return node


def make_workers(agents: AgentSet, cfg: CoordinatorConfig) -> dict:
    return {
        'arch_agent': _worker(
            'arch_agent', 'pa_result',
            lambda s, a: ArchitectureTask(run_id=s['run_id'], plan_path=s['plan_path'], attempt=a),
            agents.architecture.run),
        'code_agent': _worker(
            'code_agent', 'ca_result',
            lambda s, a: CodeAnalysisTask(run_id=s['run_id'], project_path=s['project_path'],
                                          exclude_dirs=cfg.exclude_dirs, source_roots=cfg.source_roots,
                                          attempt=a),
            agents.source_code.run),
        'consistency_agent': _worker(
            'consistency_agent', 'consistency_result',
            lambda s, a: ConsistencyTask(run_id=s['run_id'],
                                         pa=ArchitectureResult.model_validate(s['pa_result']),
                                         ca=CodeAnalysisResult.model_validate(s['ca_result']), attempt=a),
            agents.consistency.run),
        'recommend_agent': _worker(
            'recommend_agent', 'recommendation_result',
            lambda s, a: RecommendationTask(
                run_id=s['run_id'],
                violations=ConsistencyResult.model_validate(s['consistency_result']).violations,
                warnings=collect_warnings(s), attempt=a),
            agents.recommendation.run),
    }


# --------------------------------------------------------------------------------------
# Tổng hợp báo cáo
# --------------------------------------------------------------------------------------
def build_report(state: AnalysisState) -> dict:
    cons = state.get('consistency_result') or {}
    recs = {r['violation_id']: r for r in (state.get('recommendation_result') or {}).get('recommendations', [])}
    violations = [{**v, 'recommendation': recs.get(v['id'])} for v in cons.get('violations', [])]
    ca = state.get('ca_result') or {}
    status = state.get('status', 'done')
    warnings = collect_warnings(state)
    complete = status == 'done' and ca.get('status') == 'ok'
    visible = [v for v in violations if not (v['recommendation'] or {}).get('suppressed')]
    if status != 'done':
        verdict = f'Phân tích KHÔNG hoàn tất (dừng ở {state.get("failed_step")}).'
    elif not visible:
        verdict = ('Không phát hiện vi phạm cấu trúc trong phạm vi phân tích.' if complete else
                   'Chưa thấy vi phạm, nhưng CA chưa đầy đủ - xem cảnh báo.')
    else:
        verdict = f'Phát hiện {len(visible)} điểm không nhất quán cần xem xét.'
    return {
        'run_id': state.get('run_id'), 'status': status, 'failed_step': state.get('failed_step'),
        'verdict': verdict, 'complete': complete, 'warnings': warnings,
        'errors': state.get('errors', []), 'trace': state.get('trace', []),
        'pa': state.get('pa_result'),
        'ca_summary': {'stats': ca.get('stats'), 'parse_errors': ca.get('parse_errors', []),
                       'unresolved': len(ca.get('unresolved', [])), 'externals': ca.get('externals', [])},
        'ca_graph': ca.get('graph'),
        'consistency_summary': cons.get('summary'),
        'violations': violations,
    }


def make_finalize(storage: Storage | None):
    def finalize(state: AnalysisState):
        report = build_report(state)
        if storage is not None:
            try:
                storage.save_run(state['run_id'], report['status'], state.get('project_path', ''),
                                 state.get('plan_path', ''),
                                 {'verdict': report['verdict'], 'violations': len(report['violations']),
                                  'warnings': report['warnings'], 'failed_step': report['failed_step']})
            except Exception as exc:   # lưu thất bại không được làm hỏng kết quả phân tích
                report['warnings'].append(f'Không lưu được run vào SQLite: {exc}')
        return {'report': report, 'status': report['status'], 'trace': ['finalize: xong']}
    return finalize


# --------------------------------------------------------------------------------------
# Đồ thị LangGraph + facade
# --------------------------------------------------------------------------------------
def build_app(agents: AgentSet, cfg: CoordinatorConfig | None = None, storage: Storage | None = None):
    cfg = cfg or CoordinatorConfig()
    g = StateGraph(AnalysisState)
    g.add_node('coordinator', make_coordinator(cfg))
    for name, fn in make_workers(agents, cfg).items():
        g.add_node(name, fn)
        g.add_edge(name, 'coordinator')          # worker luôn quay về Coordinator
    g.add_node('finalize', make_finalize(storage))
    g.add_edge(START, 'coordinator')
    g.add_edge('finalize', END)
    return g.compile()


def default_agents(storage: Storage | None = None, llm=None, llm_model: str = 'none') -> AgentSet:
    from .agents import (AdaptiveRecommendationAgent, ArchitectureAnalysisAgent,
                         ConsistencyAnalysisAgent, SourceCodeAnalysisAgent)
    return AgentSet(architecture=ArchitectureAnalysisAgent(),
                    source_code=SourceCodeAnalysisAgent(),
                    consistency=ConsistencyAnalysisAgent(),
                    recommendation=AdaptiveRecommendationAgent(storage=storage, llm=llm, llm_model=llm_model))


class CoordinatorService:
    """Điểm vào cho UI/CLI: analyze() và submit_feedback() (phản hồi đi qua Coordinator tới Recommendation Agent)."""

    def __init__(self, agents: AgentSet | None = None, config: CoordinatorConfig | None = None,
                 storage: Storage | None = None):
        self.storage = storage or Storage()
        self.config = config or CoordinatorConfig()
        self.agents = agents or default_agents(self.storage)
        self.app = build_app(self.agents, self.config, self.storage)

    def analyze(self, project_path, plan_path, run_id: str | None = None) -> AnalysisState:
        initial = {'run_id': run_id or uuid.uuid4().hex[:12], 'project_path': str(project_path),
                   'plan_path': str(plan_path), 'status': 'running', 'failed_step': None,
                   'retries': {}, 'errors': [], 'trace': []}
        return self.app.invoke(initial, config={'recursion_limit': self.config.recursion_limit()})

    def submit_feedback(self, violation_id: str, verdict: str, note: str = ''):
        self.agents.recommendation.record_feedback(violation_id, verdict, note)
