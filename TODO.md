# TODO — Day 27: HITL PR Review Agent

## Phạm vi và trạng thái

Luồng cần hoàn thành: **URL PR → lấy diff → LLM phân tích → định tuyến theo confidence → người duyệt (nếu cần) → đăng bình luận → ghi audit → xem lại phiên**.

- [x] Clone repo vào `D:\VIN_AI\week5\VinUni-AI20k-Day27-Track03-HITL` (commit `25159a3`).
- [x] Đọc `README.md` và xác định các mốc `# TODO` trong `exercises/` và `app.py`.
- [x] Hoàn thành mã, chạy kiểm chứng và cập nhật các ô bên dưới theo kết quả thực tế.

## 1. Chuẩn bị

- [x] Chạy `uv sync --locked` tại thư mục gốc; xác nhận Python >= 3.11. `streamlit` đã có trong `pyproject.toml`.
- [x] Tạo `.env` từ `.env.example`; điền `OPENROUTER_API_KEY`, `GITHUB_TOKEN` và `GITHUB_USER`. Giữ `.env` ngoài Git.
- [x] Chọn PR thử nghiệm: `VinUni-AI20k/PR-Demo/pull/1` cho nhánh duyệt tay, `/pull/2` cho nhánh escalation; đăng bình luận bằng token của tài khoản `Chienne12`.

## 2. Exercise 1 — Đọc PR và định tuyến (`exercises/exercise_1_confidence.py`)

- [x] Trong `node_analyze`, đưa title + diff từ `node_fetch_pr` vào LLM và nhận `PRAnalysis` qua `with_structured_output`.
- [x] Trong `node_route`, so `analysis.confidence` với ngưỡng trong `common/schemas.py`: `>= 0.73` → `auto_approve`; `0.58–<0.73` → `human_approval`; `< 0.58` → `escalate`.
- [x] Trong `build_graph`, nối `START → fetch_pr → analyze → route`, thêm 3 cạnh điều kiện và nối các node cuối tới `END`.
- [x] Chạy cả hai PR bằng lệnh Exercise 1 trong README; xác nhận console in ra confidence và nhánh đã chọn. Nếu kết quả LLM thay đổi, kiểm tra giá trị thực tế trước khi kết luận routing sai.

## 3. Exercise 2 — Duyệt tay (`exercises/exercise_2_hitl.py`)

- [x] `node_human_approval`: gọi `interrupt()` với `kind="approval_request"`, diff preview, summary, reasoning, comments và confidence; nhận `choice`/`feedback` khi chạy tiếp.
- [x] Compile graph với `MemorySaver`; trong `main`, giữ cùng `thread_id`, hiện payload rồi gọi `Command(resume=...)` đến khi hết interrupt.
- [x] Thử `approve`, `reject`, `edit`: `approve` và `edit` đăng bình luận, `reject` dừng. Đã kiểm chứng bằng mock; nhánh `edit` đã đăng bình luận thật trên hai PR demo.

## 4. Exercise 3 — Escalation (`exercises/exercise_3_escalation.py`)

- [x] Bổ sung prompt để LLM tạo câu hỏi cụ thể trong `escalation_questions` khi confidence thấp.
- [x] `node_escalate`: gửi `kind="escalation"`, risk factors và câu hỏi qua `interrupt()`; nhận câu trả lời theo từng câu hỏi.
- [x] `node_synthesize`: đưa diff + các câu trả lời vào LLM để tạo `PRAnalysis` mới; nối `escalate → synthesize → human_approval → commit`.
- [x] Chạy PR #2; xác nhận thấy câu hỏi, nhập câu trả lời và thấy review đã được tổng hợp lại trước bước commit.

## 5. Exercise 4 — Audit và chạy tiếp (`exercises/exercise_4_audit.py`)

- [x] Hoàn thành `audit()` bằng `write_audit_event(thread_id, pr_url, entry)`.
- [x] Thêm `AuditEntry` cho `analyze`, `route`, `auto_approve`, `commit`, `synthesize`; dùng confidence và `risk_level_for()` phù hợp với từng bước, đo `execution_time_ms`.
- [x] Ghi sự kiện trước và sau `interrupt()` ở `human_approval` và `escalate`; kiểm tra resume có ghi lặp sự kiện `pending` hay không, vì node bắt đầu chạy lại sau interrupt.
- [x] Chạy Exercise 4 với `AsyncSqliteSaver`, lưu `thread_id`, rồi chạy `uv run python -m audit.replay --thread <thread_id>` và `--list`; đối chiếu thứ tự sự kiện với đường đi thực tế. Thử chạy tiếp cùng `thread_id` sau khi khởi động lại tiến trình.

## 6. Exercise 5 — Streamlit (`app.py`)

- [x] Import `build_graph` từ Exercise 4 và hoàn thành `run_graph`: lần đầu `ainvoke` với `pr_url`/`thread_id`, lần sau `ainvoke(Command(resume=...), cfg)` với cùng `thread_id`.
- [x] Nối 3 nút `Approve` / `Reject` / `Edit` trong `render_approval_card`; hiện diff, reasoning và comments trước khi người dùng chọn.
- [x] Trong `render_escalation_card`, hiện từng câu hỏi, thu đủ câu trả lời và gửi dict để resume.
- [x] Lấy recent sessions từ `audit_events` cho sidebar; chọn một phiên phải dùng lại đúng `thread_id` và PR URL.
- [x] Sau `synthesize`, hiển thị bước xác nhận cuối trước khi đăng bình luận.
- [x] Chạy `uv run streamlit run app.py`; kiểm tra cả 3 nhánh, reload trang khi đang chờ người duyệt, và kết quả cuối.

## 7. Kiểm chứng trước khi nộp

- [x] Xác nhận `auto_approve` chỉ chạy ở confidence `>= 0.73`, `human_approval` ở `0.58–<0.73`, `escalate` ở `< 0.58`; thử cả hai giá trị biên.
- [x] Xác nhận `reject` không đăng bình luận; `approve` chỉ đăng một lần; `edit` và escalation theo đúng hành vi đã quyết định. Kiểm tra GitHub comment thực tế bằng tài khoản/token thử nghiệm.
- [x] Kiểm tra audit có đủ `action`, `confidence`, `risk_level`, `reviewer_id`, `decision`, `reason`, `execution_time_ms`; không có sự kiện lặp ngoài ý muốn.
- [x] Chạy `git status --short`; chắc chắn `.env` và `hitl_audit.db` không được đưa vào commit.

## Điểm cần chốt theo yêu cầu bài nộp

README ghi deliverable **PostgreSQL audit trail**, đồng thời quy định rõ scaffold của lab dùng SQLite để chạy không cần thiết lập thêm (`common/db.py`, `AsyncSqliteSaver`, `audit/schema.sql`). Bài hiện dùng SQLite theo scaffold. Nếu giảng viên yêu cầu PostgreSQL thực tế, cần chuyển storage riêng.

## Kết quả chạy thật

- PR #1: `human_approval` (65%), chọn `edit`, 6 sự kiện audit; [bình luận trên GitHub](https://github.com/VinUni-AI20k/PR-Demo/pull/1#issuecomment-6197011645).
- PR #2: `escalate` (54%) → trả lời 4 câu hỏi → xác nhận bản review → `edit`, 9 sự kiện audit; [bình luận trên GitHub](https://github.com/VinUni-AI20k/PR-Demo/pull/2#issuecomment-6197036763).
- `uv run python -m audit.replay --list` và `--thread <thread_id>` đọc lại đúng hai phiên sau khi khởi động tiến trình khác.
