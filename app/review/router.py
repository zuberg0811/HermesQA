"""Quyết định vai (skill) nào cần chạy dựa trên loại file thay đổi — tiết kiệm token."""
from app.review.diff_utils import ChangedFile

DEVOPS_PATTERNS = ("Dockerfile", "docker-compose", ".github/workflows", ".gitlab-ci", "Makefile",
                   "k8s/", "helm/", "terraform", ".tf", "nginx", ".env", "requirements", "package.json", "pyproject")
TEST_PATTERNS = ("test_", "_test.", "/tests/", ".spec.", ".test.")
CODE_EXT = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".kt", ".rb", ".php", ".cs", ".rs", ".c", ".cpp", ".sql")


def select_roles(files: list[ChangedFile], enabled: list[str]) -> list[str]:
    paths = [f.path for f in files if not f.is_deleted]
    has_code = any(p.endswith(CODE_EXT) and not any(t in p for t in TEST_PATTERNS) for p in paths)
    has_devops = any(any(k in p for k in DEVOPS_PATTERNS) for p in paths)
    roles = []
    if has_code:
        roles += ["SE", "QA"]          # QA cũng cần xem code để phát hiện thiếu test / edge case
    if has_devops:
        roles.append("DevOps")
    if not roles and paths:           # thay đổi khác (docs, config) -> chỉ SE nhìn nhanh
        roles.append("SE")
    return [r for r in roles if r in enabled]
