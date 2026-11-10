"""Adaptive Recommendation Agent: giải thích vi phạm, đề xuất hướng xử lý, nhận phản hồi của dev.

- Phần giải thích/đề xuất mặc định dựa trên luật (rule-based, xác định).
- LLM là tùy chọn: truyền `llm` = hàm (prompt: str) -> str. Kết quả được cache trong SQLite với khóa
  SHA-256. Prompt chỉ chứa bằng chứng trích từ đồ thị; LLM lỗi -> tự động dùng bản rule-based.
- Cơ chế "thích nghi" ở mức tối thiểu (ĐỀ XUẤT, nhóm chưa thống nhất): phản hồi của dev được lưu
  theo violation.id; ở lần phân tích sau, vi phạm dev đánh dấu 'intended'/'false_positive' bị ẩn
  (suppressed) kèm lý do. Chưa có cơ chế học phức tạp hơn.
"""
import json

from ..models import Recommendation, RecommendationResult, RecommendationTask, Violation
from ..storage import Storage

VALID_VERDICTS = ('accepted_fix', 'intended', 'false_positive', 'ignore_for_now')
SUPPRESSING = ('intended', 'false_positive')


def _ev_text(e) -> str:
    return f'{e.source} → {e.target} ({e.rel}, {e.file}:{e.line})'


def rule_based(v: Violation) -> tuple[str, list[str]]:
    ev = v.evidence[0] if v.evidence else None
    if v.type == 'forbidden_dependency':
        text = (f'Phụ thuộc {v.from_component} → {v.to_component} không có trong kiến trúc dự kiến (PA). '
                f'Có {len(v.evidence)} bằng chứng trong mã nguồn' + (f', ví dụ: {_ev_text(ev)}.' if ev else '.'))
        acts = ['Nếu phụ thuộc là ngoài ý muốn: loại bỏ hoặc đảo phụ thuộc (đưa qua interface đặt ở component '
                'được phép, hoặc dùng callback/sự kiện).',
                f'Nếu phụ thuộc là chủ ý: cập nhật PA để cho phép {v.from_component} → {v.to_component} '
                '(ghi lại lý do) rồi chạy lại phân tích.']
        if v.confidence == 'heuristic':
            acts.append('Bằng chứng chỉ gồm lời gọi hàm suy luận theo tên (heuristic): cần kiểm tra thủ công.')
    elif v.type == 'dependency_cycle':
        text = v.message + '. Chu trình làm tăng ghép nối và gây khó kiểm thử, thay đổi.'
        acts = ['Xác định cạnh vi phạm PA trong chu trình và loại bỏ/đảo nó trước.',
                'Tách phần dùng chung thành module/component riêng mà cả hai phía cùng phụ thuộc.']
    elif v.type == 'missing_dependency':
        text = v.message + '. Có thể chưa triển khai, ánh xạ module chưa đúng, hoặc phụ thuộc xảy ra động.'
        acts = ['Kiểm tra bảng ánh xạ module → component (@map).',
                'Nếu phụ thuộc không còn cần thiết: cập nhật PA cho khớp thực tế.']
    elif v.type == 'component_without_code':
        text = v.message + '. Có thể component chưa được hiện thực hoặc tiền tố module sai.'
        acts = ['Kiểm tra tiền tố module trong @map.', 'Nếu component đã bị bỏ: cập nhật PA.']
    else:
        text = v.message
        acts = ['Thêm tiền tố module này vào ánh xạ của một component, hoặc bổ sung component vào PA.']
    return text, acts


class AdaptiveRecommendationAgent:
    def __init__(self, storage: Storage | None = None, llm=None, llm_model: str = 'none'):
        self.storage = storage
        self.llm = llm
        self.llm_model = llm_model

    # ---- API phản hồi (được gọi QUA Coordinator) ----
    def record_feedback(self, violation_id: str, verdict: str, note: str = ''):
        if verdict not in VALID_VERDICTS:
            raise ValueError(f'verdict phải thuộc {VALID_VERDICTS}')
        if self.storage is None:
            raise RuntimeError('Cần Storage để lưu phản hồi')
        self.storage.add_feedback(violation_id, verdict, note)

    def run(self, task: RecommendationTask) -> RecommendationResult:
        feedback = self.storage.latest_feedback() if self.storage else {}
        notes = list(task.warnings)
        recs = []
        for v in task.violations:
            text, actions = rule_based(v)
            source = 'rule'
            if self.llm is not None and v.type in ('forbidden_dependency', 'dependency_cycle'):
                extra = self._llm_explain(v)
                if extra:
                    text, source = f'{text}\n\nGiải thích bổ sung (LLM): {extra}', 'rule+llm'
                else:
                    notes.append(f'LLM không khả dụng cho {v.id}; dùng bản rule-based')
            rec = Recommendation(violation_id=v.id, explanation=text, suggested_actions=actions,
                                 priority=v.severity, source=source)
            if v.id in feedback:
                verdict, note = feedback[v.id]
                if verdict in SUPPRESSING:
                    rec.suppressed = True
                    rec.suppress_reason = f'Dev đã đánh dấu "{verdict}" ở lần trước' + (f': {note}' if note else '')
                else:
                    rec.explanation += f'\n[Phản hồi trước: {verdict}' + (f' - {note}' if note else '') + ']'
            recs.append(rec)
        return RecommendationResult(recommendations=recs, notes=notes)

    def _llm_explain(self, v: Violation) -> str | None:
        prompt = ('Bạn là trợ lý kiến trúc phần mềm. Chỉ dựa trên bằng chứng JSON dưới đây, giải thích ngắn gọn '
                  '(tối đa 5 câu, tiếng Việt) nguyên nhân có thể và hướng khắc phục. Không suy đoán ngoài bằng chứng.\n'
                  + json.dumps(v.model_dump(), ensure_ascii=False, sort_keys=True))
        key = Storage.make_cache_key(self.llm_model, prompt)
        if self.storage:
            cached = self.storage.cache_get(key)
            if cached:
                return cached
        try:
            answer = self.llm(prompt)
        except Exception:
            return None
        if answer and self.storage:
            self.storage.cache_put(key, answer)
        return answer or None
