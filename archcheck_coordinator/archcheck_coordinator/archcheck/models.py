"""Hợp đồng dữ liệu (data contract) giữa Coordinator và các Agent + State chung của LangGraph.

Nguyên tắc: Agent chỉ biết Task (vào) và Result (ra). Coordinator chỉ kiểm tra *hình thức*
của Result (đúng schema, status, không rỗng), không diễn giải nội dung chuyên môn.
"""
import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

DEFAULT_EXCLUDES = ['venv', '.venv', '.git', '__pycache__', 'node_modules', 'build', 'dist', '.tox']

Status = Literal['ok', 'partial', 'failed']


class AgentResult(BaseModel):
    """Phần chung của mọi kết quả Agent.

    retryable=True: lỗi có thể là tạm thời (ngoại lệ, hết thời gian) -> Coordinator được phép thử lại.
    retryable=False: lỗi mang tính xác định (đường dẫn sai, PA rỗng) -> thử lại cùng đầu vào vô nghĩa.
    """
    status: Status = 'ok'
    error: str | None = None
    retryable: bool = False


# ----------------------------- Architecture Analysis Agent -----------------------------
class Component(BaseModel):
    name: str
    module_prefixes: list[str] = Field(default_factory=list)  # ánh xạ component -> tiền tố module


class ArchitectureTask(BaseModel):
    run_id: str
    plan_path: str
    attempt: int = 1


class ArchitectureResult(AgentResult):
    components: list[Component] = Field(default_factory=list)
    allowed_dependencies: list[tuple[str, str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ----------------------------- Source Code Analysis Agent -----------------------------
class CodeAnalysisTask(BaseModel):
    run_id: str
    project_path: str
    exclude_dirs: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    source_roots: list[str] = Field(default_factory=lambda: ['.'])  # thư mục gốc để tính tên module
    attempt: int = 1


class CodeAnalysisResult(AgentResult):
    graph: dict = Field(default_factory=dict)          # node-link của NetworkX MultiDiGraph
    stats: dict = Field(default_factory=dict)
    parse_errors: list[str] = Field(default_factory=list)
    unresolved: list[dict] = Field(default_factory=list)
    externals: list[str] = Field(default_factory=list)  # thư viện ngoài được import


# ----------------------------- Consistency Analysis Agent -----------------------------
class Evidence(BaseModel):
    rel: str
    source: str
    target: str
    line: int | None = None
    file: str | None = None
    confidence: Literal['high', 'heuristic'] = 'high'


ViolationType = Literal[
    'forbidden_dependency',   # có trong CA, không được phép trong PA (divergence)
    'missing_dependency',     # được phép/dự kiến trong PA, không thấy trong CA (absence)
    'dependency_cycle',
    'unmapped_module',
    'component_without_code',
]


class Violation(BaseModel):
    id: str
    type: ViolationType
    severity: Literal['high', 'medium', 'low', 'info']
    from_component: str | None = None
    to_component: str | None = None
    message: str
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Literal['high', 'heuristic'] = 'high'


class ConsistencyTask(BaseModel):
    run_id: str
    pa: ArchitectureResult
    ca: CodeAnalysisResult
    attempt: int = 1


class ConsistencyResult(AgentResult):
    violations: list[Violation] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)


# ----------------------------- Adaptive Recommendation Agent -----------------------------
class RecommendationTask(BaseModel):
    run_id: str
    violations: list[Violation]
    warnings: list[str] = Field(default_factory=list)
    attempt: int = 1


class Recommendation(BaseModel):
    violation_id: str
    explanation: str
    suggested_actions: list[str] = Field(default_factory=list)
    priority: Literal['high', 'medium', 'low', 'info'] = 'info'
    source: Literal['rule', 'rule+llm'] = 'rule'
    suppressed: bool = False
    suppress_reason: str | None = None


class RecommendationResult(AgentResult):
    recommendations: list[Recommendation] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ----------------------------- State chung của LangGraph -----------------------------
class AnalysisState(TypedDict, total=False):
    run_id: str
    project_path: str
    plan_path: str
    status: Literal['running', 'done', 'failed']
    stage: str
    failed_step: str | None
    pa_result: dict | None
    ca_result: dict | None
    consistency_result: dict | None
    recommendation_result: dict | None
    retries: dict[str, int]                           # số lần Coordinator đã giao mỗi bước
    errors: Annotated[list[dict], operator.add]       # reducer: nối danh sách
    trace: Annotated[list[str], operator.add]
    report: dict
