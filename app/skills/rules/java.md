### Java / Kotlin
**Báo:**
- Tài nguyên (`Connection`, `Statement`, `ResultSet`, stream) mở mà không try-with-resources/`use` trên đường đi mới.
- SQL ghép chuỗi thay vì `PreparedStatement` có tham số; `MessageDigest` MD5/SHA-1 cho mật khẩu.
- NPE: dùng kết quả có thể `null` mà không kiểm tra; `Optional.get()` không kiểm tra; `equals` trên đối tượng có thể null.
- So sánh chuỗi/đối tượng bằng `==`; `catch (Exception e) {}` nuốt lỗi; trạng thái dùng chung không đồng bộ.
- Lỗi thời: API deprecated ở dòng MỚI mà bộ quét `stale` chưa báo (`java.util.Date` khi codebase đã dùng `java.time`,
  `Runtime.exec(String)`, `Thread.sleep` chờ điều kiện thay vì cơ chế đồng bộ); không tối ưu: nối chuỗi `+` trong
  vòng lặp, `contains` lặp lại trên `List` lớn (dùng `Set`), boxing trong vòng lặp nóng.

**Không báo:**
- Thiếu `final`, thứ tự modifier, Javadoc.
- Vấn đề luồng khi không có bằng chứng lớp được dùng đa luồng.
