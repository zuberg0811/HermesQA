### Go
**Báo:**
- Bỏ qua `err` (`_ =`, hoặc không kiểm tra) ở lời gọi có thể thất bại; dùng giá trị trước khi kiểm tra `err`.
- Dereference con trỏ/map/interface có thể `nil` (kết quả truy vấn không tìm thấy, type assertion không `ok`).
- SQL ghép chuỗi/`fmt.Sprintf` thay vì tham số hoá; lệnh shell ghép từ đầu vào.
- Goroutine rò rỉ, ghi map đồng thời, thiếu `defer Close()`/`defer Unlock()` trên đường đi mới.
- `context` không được truyền xuống lời gọi I/O mới.

**Không báo:**
- `err` bị bỏ qua có chủ đích và có chú thích lý do; lỗi của `Close()` trên đường đọc.
- Đặt tên, độ dài hàm, chú thích — trừ khi sai nghĩa.
