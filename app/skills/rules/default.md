### Mọi loại file
**Báo:**
- Logic sai, thiếu điều kiện biên, ngoại lệ bị nuốt.
- Bí mật (token, mật khẩu, khoá) hoặc dữ liệu cá nhân xuất hiện trong mã nguồn hay log.
- Thay đổi làm vỡ hợp đồng với nơi gọi (đổi tên trường, đổi kiểu trả về, đổi mặc định).

**Không báo:**
- Sở thích phong cách khi repo không có quy ước và linter không báo.
- Vấn đề nằm ở code cũ mà diff này không đụng tới.
- Đề xuất refactor lớn cho một thay đổi nhỏ.
- Điều bạn không kiểm chứng được từ diff và context (nếu vẫn muốn nêu: hạ confidence xuống dưới 0.6).
