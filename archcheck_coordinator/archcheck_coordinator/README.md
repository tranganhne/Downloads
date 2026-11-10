# archcheck – Coordinator Agent điều phối 5 Agent (LangGraph)

Python 3.11+. Công nghệ: LangGraph, Tree-sitter (py-tree-sitter + tree-sitter-python), NetworkX,
SQLite + SQLAlchemy, Pydantic, Streamlit, Docker.

## Chạy
```bash
pip install -r requirements.txt
python run_cli.py sample/shop sample/plan.puml      # CLI
streamlit run streamlit_app.py                      # giao diện demo
pytest                                              # test
docker build -t archcheck . && docker run -p 8501:8501 archcheck
```

## Ánh xạ với nghiên cứu
| Nội dung nghiên cứu | Nơi cài đặt |
|---|---|
| Hub-and-spoke: Agent không gọi nhau | `coordinator.py`: mọi worker `add_edge(worker, 'coordinator')` |
| Thứ tự theo phụ thuộc dữ liệu PA → CA → so sánh → đề xuất | `STEPS`, nút `coordinator` (dùng `Command`) |
| Hợp đồng Task/Result (Pydantic) | `models.py` |
| Coordinator chỉ kiểm tra hình thức kết quả | `check_result()` |
| Thử lại / dừng / lỗi xác định không thử lại | `retryable`, `max_retry`, `recursion_limit` |
| CA `partial` đi tiếp kèm cảnh báo | `collect_warnings()`, `build_report()` |
| Source Code Analysis: Tree-sitter + NetworkX | `agents/source_code_agent.py` |
| So sánh PA–CA (divergence/absence, chu trình) | `agents/consistency_agent.py` |
| LLM Cache SQLite + SHA-256; feedback | `storage.py`, `agents/recommendation_agent.py` |
| Phản hồi dev đi qua Coordinator | `CoordinatorService.submit_feedback()` |

## Trạng thái và giới hạn (đọc kỹ)
- **Chưa được chạy kiểm thử** trong môi trường tạo code (không cài được langgraph/tree-sitter/pydantic).
  Mới kiểm tra cú pháp biên dịch và tuần tự hóa đồ thị NetworkX. Cần chạy `pytest` và sửa theo phiên bản thư viện thực tế.
- Architecture Agent: chỉ đọc PlantUML component diagram (có chỉ thị `' @map`) và JSON; chưa có phần đọc
  mô tả văn bản bằng LLM. Consistency Agent chỉ kiểm tra cấu trúc, không kiểm tra hành vi động.
- Lời gọi hàm/kế thừa được phân giải theo *tên* (heuristic, cạnh gắn `confidence='heuristic'`); không có suy luận kiểu.
  Import động, alias phức tạp, `from x import *` không được xử lý.
- LLM là tùy chọn (mặc định tắt). "Thích nghi" mới là mức tối thiểu (ẩn vi phạm dev đã phản hồi `intended`/`false_positive`);
  đây là đề xuất, chưa được nhóm thống nhất.
- Chưa có Alembic migration, chưa chạy song song `arch_agent` và `code_agent`, chưa có checkpointer LangGraph.
