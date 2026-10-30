### Go
**Báo:**
- Bỏ qua `err` (`_ =`, hoặc không kiểm tra) ở lời gọi có thể thất bại; dùng giá trị trước khi kiểm tra `err`.
- Dereference con trỏ/map/interface có thể `nil` (kết quả truy vấn không tìm thấy, type assertion không `ok`).
- SQL ghép chuỗi/`fmt.Sprintf` thay vì tham số hoá; lệnh shell ghép từ đầu vào.
- Goroutine rò rỉ, ghi map đồng thời, thiếu `defer Close()`/`defer Unlock()` trên đường đi mới.
- `context` không được truyền xuống lời gọi I/O mới.
- Lỗi thời: API deprecated ở dòng MỚI mà bộ quét `stale` chưa báo (`interface{}` thay vì `any` trong file đã dùng `any`,
  `sort.Slice` khi `slices.SortFunc` đã có); không tối ưu: cấp phát slice/map trong vòng lặp nóng mà có thể `make`
  sẵn dung lượng, nối chuỗi `+=` trong vòng lặp (dùng `strings.Builder`).

**Không báo:**
- `err` bị bỏ qua có chủ đích và có chú thích lý do; lỗi của `Close()` trên đường đọc.
- Đặt tên, độ dài hàm, chú thích — trừ khi sai nghĩa.
