### Java / Kotlin
**Báo:**
- Tài nguyên (`Connection`, `Statement`, `ResultSet`, stream) mở mà không try-with-resources/`use` trên đường đi mới.
- SQL ghép chuỗi thay vì `PreparedStatement` có tham số; `MessageDigest` MD5/SHA-1 cho mật khẩu.
- NPE: dùng kết quả có thể `null` mà không kiểm tra; `Optional.get()` không kiểm tra; `equals` trên đối tượng có thể null.
- So sánh chuỗi/đối tượng bằng `==`; `catch (Exception e) {}` nuốt lỗi; trạng thái dùng chung không đồng bộ.

**Không báo:**
- Thiếu `final`, thứ tự modifier, Javadoc.
- Vấn đề luồng khi không có bằng chứng lớp được dùng đa luồng.
