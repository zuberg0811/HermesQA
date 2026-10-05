### CI/CD (GitHub Actions, GitLab CI)
**Báo:**
- `pull_request_target` kèm checkout code của fork; `permissions: write-all`; secret in ra log (`echo`, `set -x`).
- Action bên thứ ba dùng tag di động (`@main`, `@master`) — chuỗi cung ứng.
- Bước deploy không có điều kiện nhánh/tag, hoặc chạy trước khi test.

**Không báo:**
- Action chính chủ (`actions/*`) dùng tag phiên bản chính (`@v4`).
- Tối ưu cache, đặt tên job.
