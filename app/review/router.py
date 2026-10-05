"""Quyết định vai (skill) nào cần chạy dựa trên loại file thay đổi — tiết kiệm token và giảm nhiễu."""
from app.review.diff_utils import ChangedFile

DEVOPS_PATTERNS = ("Dockerfile", "docker-compose", ".github/workflows", ".gitlab-ci", "Makefile",
                   "k8s/", "helm/", "terraform", ".tf", "nginx", ".env", "requirements", "package.json", "pyproject")
TEST_PATTERNS = ("/test_", "_test.", "/tests/", "/test/", "/__tests__/", ".spec.", ".test.")
CODE_EXT = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".kt", ".rb", ".php", ".cs", ".rs", ".c", ".cpp", ".sql")


def _norm(path: str) -> str:
    return "/" + path.replace("\\", "/").lstrip("/")


def is_test(path: str) -> bool:
    return any(t in _norm(path) for t in TEST_PATTERNS)


def is_code(path: str) -> bool:
    return path.endswith(CODE_EXT)


def is_devops(path: str) -> bool:
    return any(k in path for k in DEVOPS_PATTERNS)


def select_roles(files: list[ChangedFile], enabled: list[str]) -> list[str]:
    paths = [f.path for f in files if not f.is_deleted]
    has_code = any(is_code(p) and not is_test(p) for p in paths)
    has_test = any(is_code(p) and is_test(p) for p in paths)
    has_devops = any(is_devops(p) for p in paths)
    roles = []
    if has_code:
        roles += ["SE", "QA"]          # QA cũng cần xem code để phát hiện thiếu test / edge case
    elif has_test:
        roles.append("QA")             # PR chỉ sửa file test là việc của QA, không phải SE
    if has_devops:
        roles.append("DevOps")
    if not roles and paths:           # thay đổi khác (docs, config) -> chỉ SE nhìn nhanh
        roles.append("SE")
    return [r for r in roles if r in enabled]
