"""Architecture Analysis Agent: trích Planned Architecture (PA) từ mô tả kiến trúc.

Hỗ trợ trong bản này (xác định, không dùng LLM):
  - PlantUML component diagram (.puml/.plantuml/.pu): khai báo `component [Tên]`, quan hệ `[A] --> [B]`,
    và chỉ thị ánh xạ module trong chú thích:   ' @map Tên = tiền_tố1, tiền_tố2
    (không có @map: mặc định tiền tố = tên component viết thường)
  - JSON: {"components":[{"name":..,"module_prefixes":[..]}], "allowed_dependencies":[["A","B"]]}

Chưa hỗ trợ: alias, interface/port, package lồng nhau, mô tả kiến trúc dạng văn bản tự do (cần LLM).
`llm` được giữ làm điểm mở rộng cho trường hợp đó; chưa được sử dụng.
"""
import json
import re
from pathlib import Path

from ..models import ArchitectureResult, ArchitectureTask, Component

COMP_RE = re.compile(r'^\s*component\s+\[?(\w+)\]?', re.I)
REL_RE = re.compile(r'^\s*\[?(\w+)\]?\s*[-.]+(?:[a-z]+[-.]*)?>\s*\[?(\w+)\]?', re.I)
MAP_RE = re.compile(r"^\s*'\s*@map\s+(\w+)\s*=\s*(.+?)\s*$")


class ArchitectureAnalysisAgent:
    def __init__(self, llm=None):
        self.llm = llm

    def run(self, task: ArchitectureTask) -> ArchitectureResult:
        path = Path(task.plan_path)
        if not path.is_file():
            return ArchitectureResult(status='failed', retryable=False,
                                      error=f'Không tìm thấy tài liệu kiến trúc: {path}')
        text = path.read_text(encoding='utf8')
        suffix = path.suffix.lower()
        if suffix == '.json':
            return self._from_json(text)
        if suffix in ('.puml', '.plantuml', '.pu', '.txt'):
            return self._from_plantuml(text)
        return ArchitectureResult(status='failed', retryable=False,
                                  error=f'Định dạng chưa hỗ trợ: {suffix}')

    @staticmethod
    def _from_plantuml(text: str) -> ArchitectureResult:
        names: list[str] = []
        maps: dict[str, list[str]] = {}
        rels: list[tuple[str, str]] = []
        for line in text.splitlines():
            m = MAP_RE.match(line)
            if m:
                maps[m.group(1)] = [p.strip() for p in m.group(2).split(',') if p.strip()]
                continue
            if line.strip().startswith("'"):
                continue
            m = COMP_RE.match(line)
            if m:
                if m.group(1) not in names:
                    names.append(m.group(1))
                continue
            m = REL_RE.match(line)
            if m:
                a, b = m.group(1), m.group(2)
                for n in (a, b):
                    if n not in names:
                        names.append(n)
                if (a, b) not in rels:
                    rels.append((a, b))
        if not names:
            return ArchitectureResult(status='failed', retryable=False,
                                      error='Không tìm thấy component nào trong PlantUML')
        warnings = []
        comps = []
        for n in names:
            prefixes = maps.get(n)
            if not prefixes:
                prefixes = [n.lower()]
                warnings.append(f'Component {n} không có @map; dùng tiền tố mặc định "{n.lower()}"')
            comps.append(Component(name=n, module_prefixes=prefixes))
        if not rels:
            warnings.append('PA không có quan hệ phụ thuộc nào: mọi phụ thuộc liên-component sẽ bị coi là vi phạm')
        return ArchitectureResult(components=comps, allowed_dependencies=rels, warnings=warnings)

    @staticmethod
    def _from_json(text: str) -> ArchitectureResult:
        try:
            data = json.loads(text)
            res = ArchitectureResult(
                components=[Component(**c) for c in data.get('components', [])],
                allowed_dependencies=[tuple(p) for p in data.get('allowed_dependencies', [])])
        except Exception as exc:   # JSON hỏng/sai cấu trúc: lỗi xác định
            return ArchitectureResult(status='failed', retryable=False, error=f'JSON PA không hợp lệ: {exc}')
        if not res.components:
            return ArchitectureResult(status='failed', retryable=False, error='PA không có component nào')
        return res
