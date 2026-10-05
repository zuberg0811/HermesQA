### Python
**Báo:**
- Tham số mặc định khả biến (`def f(x=[])`, `def f(x={})`), biến lớp khả biến dùng chung ngoài ý muốn.
- `except:` trần hoặc `except Exception: pass` nuốt lỗi; mất traceback khi raise lại (thiếu `from err`).
- So sánh bằng `is` với literal; `== None` / `!= None` (mức thấp); điều kiện luôn đúng kiểu `x == "a" or "b"`.
- Biên: truy cập `xs[0]`, `max()`, chia cho `len(xs)` khi tập có thể rỗng; lệch một ở `range`/slice.
- Tên chưa định nghĩa, biến dùng trước khi gán, import/biến khai báo mà không dùng (mức thấp).
- Bảo mật khi dữ liệu KHÔNG phải hằng tin cậy: `eval`/`exec`, `subprocess(..., shell=True)`, `os.system` với chuỗi ghép,
  `pickle.loads`, `yaml.load` không `SafeLoader`, SQL ghép chuỗi/f-string, `hashlib.md5`/`sha1` cho mật khẩu,
  `verify=False`, `debug=True` trong cấu hình chạy thật.
- Tài nguyên mở mà không `with` trên đường đi MỚI do diff thêm vào.
- Hiệu năng rõ ràng: nối chuỗi `+=` trong vòng lặp, tra `in list` lặp lại, biên dịch regex trong vòng lặp.
- `print()` dùng làm logging trong code chạy thật; logic trùng lặp với hàm đã có trong cùng file (mức thấp).

**Không báo:**
- Thiếu đóng kết nối/đóng file ở code CŨ không nằm trong các dòng `+`.
- "Thiếu validation đầu vào" chung chung khi không chỉ ra được đầu vào nào gây lỗi gì.
- Vấn đề đồng thời khi không có bằng chứng code chạy đa luồng/async.
- Type hint, docstring, đặt tên — trừ khi sai nghĩa.
