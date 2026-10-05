### Manifest dependency (requirements.txt, package.json, go.mod, pom.xml...)
**Báo:**
- Dependency bị bỏ ghim phiên bản hoặc nới thành khoảng mở (`>=`, `*`, `latest`) ở dòng diff sửa.
- Thêm dependency lớn cho một việc nhỏ; dependency đã bị bỏ rơi hoặc có lỗ hổng bạn BIẾT CHẮC.
- Manifest đổi mà lockfile không đổi theo (khi repo có lockfile).

**Không báo:**
- Dependency vốn đã không ghim từ trước và diff không đụng tới.
- Gợi ý nâng phiên bản chung chung.
