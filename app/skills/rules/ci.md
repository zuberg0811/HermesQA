### CI/CD (GitHub Actions, GitLab CI)
**Báo:**
- `pull_request_target` kèm checkout code của fork; `permissions: write-all`; secret in ra log (`echo`, `set -x`).
- Action bên thứ ba dùng tag di động (`@main`, `@master`) — chuỗi cung ứng.
- Bước deploy không có điều kiện nhánh/tag, hoặc chạy trước khi test.
- Lỗi thời ở dòng diff sửa mà bộ quét `stale` chưa báo: action bên thứ ba bị archive, runner/runtime trong ma trận đã EOL,
  cú pháp workflow bị GitHub bỏ (`::set-output`, `set-env`, `actions/create-release`).

**Không báo:**
- Action chính chủ (`actions/*`) dùng tag phiên bản chính (`@v4`).
- Tối ưu cache, đặt tên job.
