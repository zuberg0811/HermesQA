"""Parse unified diff -> danh sách file/hunk, và map line để comment inline."""
import fnmatch
from dataclasses import dataclass, field
from unidiff import PatchSet


@dataclass
class ChangedFile:
    path: str
    added_lines: set[int] = field(default_factory=set)   # số dòng phía RIGHT (head) có trong diff
    patch_text: str = ""
    is_binary: bool = False
    is_deleted: bool = False


def parse_diff(diff_text: str, ignore_globs: list[str]) -> list[ChangedFile]:
    out: list[ChangedFile] = []
    for pf in PatchSet(diff_text):
        path = pf.path
        if any(fnmatch.fnmatch(path, g) for g in ignore_globs):
            continue
        cf = ChangedFile(path=path, is_binary=pf.is_binary_file, is_deleted=pf.is_removed_file, patch_text=str(pf))
        for hunk in pf:
            for ln in hunk:
                if ln.is_added or ln.is_context:
                    if ln.target_line_no:
                        cf.added_lines.add(ln.target_line_no)
        out.append(cf)
    return out


def commentable_line(cf: ChangedFile, line: int) -> int | None:
    """GitHub chỉ cho comment vào dòng nằm trong diff. Nếu lệch, snap về dòng gần nhất (<=3 dòng)."""
    if line in cf.added_lines:
        return line
    near = [l for l in cf.added_lines if abs(l - line) <= 3]
    return min(near, key=lambda l: abs(l - line)) if near else None


def file_context(repo_dir: str, path: str, lines: set[int], radius: int = 30, max_chars: int = 12000) -> str:
    """Lấy nội dung file quanh các dòng thay đổi (±radius) để đưa cho agent."""
    try:
        with open(f"{repo_dir}/{path}", encoding="utf-8", errors="replace") as f:
            src = f.read().splitlines()
    except (FileNotFoundError, IsADirectoryError):
        return ""
    if not lines:
        return "\n".join(f"{i+1:5d}| {t}" for i, t in enumerate(src[:200]))[:max_chars]
    keep = set()
    for l in lines:
        keep.update(range(max(1, l - radius), min(len(src), l + radius) + 1))
    chunks, prev = [], None
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            chunks.append("      | ...")
        chunks.append(f"{i:5d}| {src[i-1]}")
        prev = i
    return "\n".join(chunks)[:max_chars]
