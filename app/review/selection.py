"""Chọn file nào được gửi cho LLM, vai nào xem file nào, và chia lô theo ngân sách ký tự.

Đây là phần TẤT ĐỊNH của pipeline: các quyết định ở đây không được phép sai và không
giao cho model. Ba nguyên tắc:
  1. File có thể chứa bí mật (.env, khoá riêng...) KHÔNG BAO GIỜ được gửi lên LLM.
     Static tools (gitleaks) vẫn quét chúng trong sandbox không mạng.
  2. File sinh tự động / lockfile / thư viện vendored không đáng tốn token.
  3. Không cắt im lặng. Thay đổi lớn thì chia lô; một file quá lớn thì cắt và GHI NHẬN,
     để report nói rõ phần nào chưa được review.
Ý tưởng lấy từ bộ lọc 6 cổng của alibaba/open-code-review (Apache-2.0).
"""
import fnmatch
from dataclasses import dataclass

from app.review.diff_utils import ChangedFile
from app.review.router import is_code, is_devops, is_test

_SECRET_NAMES = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", "_netrc", ".npmrc", ".pypirc",
    ".dockercfg", "credentials.json", "service-account*.json",
)
# Bản mẫu của file env không chứa giá trị thật -> vẫn review được.
_SECRET_TEMPLATES = (".env.example", ".env.sample", ".env.template", ".env.dist")
_SECRET_DIRS = (".ssh", "secrets", ".aws", ".gnupg")

_GENERATED_NAMES = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json", "poetry.lock",
    "Pipfile.lock", "uv.lock", "Cargo.lock", "go.sum", "composer.lock", "Gemfile.lock",
    "*.min.js", "*.min.css", "*.map", "*.pb.go", "*_pb2.py", "*_pb2_grpc.py", "*.generated.*",
    "*.gen.go", "*.snap",
)
_GENERATED_DIRS = (
    "node_modules", "bower_components", "vendor", "dist", "__pycache__", ".venv", "venv",
    "site-packages", "__snapshots__", ".next", ".nuxt", "target", ".terraform",
)

TRUNCATION_MARK = "[... ĐÃ CẮT BỚT: file quá lớn, phần còn lại KHÔNG được review ...]"

REASON_TEXT = {
    "secret": "có thể chứa bí mật — không gửi lên LLM (static tools vẫn quét trong sandbox)",
    "generated": "file sinh tự động / lockfile / thư viện bên ngoài",
    "truncated": "quá lớn — chỉ review phần đầu",
    "binary": "file nhị phân",
    "deleted": "file bị xoá",
}


@dataclass
class Skipped:
    path: str
    reason: str          # khoá trong REASON_TEXT
    detail: str = ""

    def describe(self) -> str:
        text = REASON_TEXT.get(self.reason, self.reason)
        return f"`{self.path}` — {text}" + (f" ({self.detail})" if self.detail else "")


def _parts(path: str) -> list[str]:
    return path.replace("\\", "/").split("/")


def is_secret_path(path: str) -> bool:
    parts = _parts(path)
    base = parts[-1]
    if base in _SECRET_TEMPLATES:
        return False
    if any(d in _SECRET_DIRS for d in parts[:-1]):
        return True
    return any(fnmatch.fnmatch(base, pat) for pat in _SECRET_NAMES)


def is_generated_path(path: str) -> bool:
    parts = _parts(path)
    if any(d in _GENERATED_DIRS for d in parts[:-1]):
        return True
    return any(fnmatch.fnmatch(parts[-1], pat) for pat in _GENERATED_NAMES)


def select_for_llm(files: list[ChangedFile]) -> tuple[list[ChangedFile], list[Skipped]]:
    """Trả (file gửi LLM, file bị loại kèm lý do). Thứ tự cổng: binary/deleted -> secret -> generated.
    Cổng secret đứng trước mọi cấu hình của người dùng: không có cách nào bật lại."""
    kept, skipped = [], []
    for f in files:
        if f.is_binary:
            skipped.append(Skipped(f.path, "binary"))
        elif f.is_deleted:
            skipped.append(Skipped(f.path, "deleted"))
        elif is_secret_path(f.path):
            skipped.append(Skipped(f.path, "secret"))
        elif is_generated_path(f.path):
            skipped.append(Skipped(f.path, "generated"))
        else:
            kept.append(f)
    return kept, skipped


def files_for_role(role: str, files: list[ChangedFile]) -> list[ChangedFile]:
    """Mỗi vai chỉ nhận file thuộc chuyên môn của nó.

    Trước đây cả 3 vai nhận mọi file, nên SE bình luận Dockerfile, DevOps bình luận logic,
    QA đòi test cho file cấu hình — ba nguồn nhiễu đo được trên bộ eval.
      SE     : code không phải test (không nhận Dockerfile/manifest thuần)
      QA     : code + file test
      DevOps : Dockerfile, CI, IaC, manifest dependency
    Nếu bộ lọc làm rỗng danh sách thì trả [] — vai đó không có gì để xem.
    """
    if role == "QA":
        return [f for f in files if is_code(f.path)]
    if role == "DevOps":
        return [f for f in files if is_devops(f.path)]
    if role == "SE":
        picked = [f for f in files if not is_test(f.path) and (is_code(f.path) or not is_devops(f.path))]
        return picked
    return list(files)


def _cut(text: str, limit: int) -> str:
    """Cắt ở ranh giới dòng, không cắt giữa dòng."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    nl = head.rfind("\n")
    return (head[:nl] if nl > 0 else head) + "\n" + TRUNCATION_MARK


def build_batches(items: list[dict], max_prompt_chars: int, max_file_chars: int) -> tuple[list[list[dict]], list[Skipped]]:
    """items: [{"path", "patch", "context"}]. Chia thành các lô, mỗi lô <= max_prompt_chars.

    Thay cho `files[:25]` + `patch[:8000]` trước đây (cắt im lặng: PR 40 file thì 15 file cuối
    không ai xem mà report vẫn như đã xem hết). Giờ:
      - mọi file đều vào một lô nào đó;
      - file đơn lẻ vượt max_file_chars bị cắt, và việc cắt được trả về để ghi vào report.
    """
    truncated: list[Skipped] = []
    sized: list[dict] = []
    for it in items:
        patch, ctx = it["patch"], it["context"]
        total = len(patch) + len(ctx)
        if total > max_file_chars:
            patch_budget = int(max_file_chars * 0.6)
            new_patch = _cut(patch, patch_budget)
            new_ctx = _cut(ctx, max(0, max_file_chars - len(new_patch)))
            truncated.append(Skipped(it["path"], "truncated", f"giữ {len(new_patch) + len(new_ctx)}/{total} ký tự"))
            it = {**it, "patch": new_patch, "context": new_ctx}
        sized.append(it)

    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_size = 0
    for it in sized:
        size = len(it["patch"]) + len(it["context"])
        if cur and cur_size + size > max_prompt_chars:
            batches.append(cur)
            cur, cur_size = [], 0
        cur.append(it)
        cur_size += size
    if cur:
        batches.append(cur)
    return batches, truncated
