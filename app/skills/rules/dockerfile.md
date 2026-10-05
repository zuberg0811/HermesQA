### Dockerfile / docker-compose
**Báo (chỉ ở dòng diff thêm hoặc sửa):**
- Ảnh cơ sở dùng `latest` hoặc không ghi tag; chạy bằng root (thiếu `USER`, hoặc `USER root` ở cuối).
- `COPY . .` / `ADD .` kéo cả `.git`, `.env`, secrets vào image; `ADD` dùng cho việc `COPY` làm được.
- `apt-get install` thiếu `-y`, thiếu `update` cùng layer, không dọn `/var/lib/apt/lists`; `pip install` thiếu `--no-cache-dir`.
- `sudo` trong `RUN`; bí mật truyền qua `ARG`/`ENV`; `CMD`/`ENTRYPOINT` dạng shell làm tiến trình không nhận SIGTERM.
- compose: cổng nội bộ mở ra `0.0.0.0`, `privileged: true`, mount socket Docker không cần thiết.

**Không báo:**
- Thiếu `HEALTHCHECK`, thiếu multi-stage, thứ tự layer, ghim digest — khi diff KHÔNG đụng tới phần đó.
  Đây là gợi ý cải thiện, không phải lỗi do thay đổi này gây ra.
- Cùng một vấn đề mà static tool (hadolint/semgrep) đã báo ở dòng đó.
