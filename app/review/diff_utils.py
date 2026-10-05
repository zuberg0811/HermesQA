"""Parse unified diff -> danh sách file/hunk, ghim finding vào dòng, và che dòng chứa bí mật."""
import fnmatch
import os
from dataclasses import dataclass, field
from unidiff import PatchSet

REDACTED = "[ĐÃ CHE: static tool đánh dấu dòng này có bí mật — không gửi lên LLM]"


@dataclass
class ChangedFile:
    path: str
    added_lines: set[int] = field(default_factory=set)   # dòng phía RIGHT (head) có trong diff: thêm + context
    patch_text: str = ""
    is_binary: bool = False
    is_deleted: bool = False
    new_lines: dict[int, str] = field(default_factory=dict)   # số dòng (head) -> nội dung, cho dòng thêm + context
    added_only: set[int] = field(default_factory=set)         # chỉ các dòng '+'
    rows: list[tuple[int | None, str]] = field(default_factory=list)  # từng dòng của patch kèm số dòng head (None = header/dòng xoá)


def parse_diff(diff_text: str, ignore_globs: list[str]) -> list[ChangedFile]:
    out: list[ChangedFile] = []
    for pf in PatchSet(diff_text):
        path = pf.path
        if any(fnmatch.fnmatch(path, g) for g in ignore_globs):
            continue
        cf = ChangedFile(path=path, is_binary=pf.is_binary_file, is_deleted=pf.is_removed_file)
        cf.rows = [(None, f"--- {pf.source_file}"), (None, f"+++ {pf.target_file}")]
        for hunk in pf:
            cf.rows.append((None, str(hunk).splitlines()[0]))
            for ln in hunk:
                text = ln.value.rstrip("\r\n")
                no = ln.target_line_no if (ln.is_added or ln.is_context) else None
                if no:
                    cf.added_lines.add(no)
                    cf.new_lines[no] = text
                    if ln.is_added:
                        cf.added_only.add(no)
                cf.rows.append((no, f"{ln.line_type}{text}"))
        cf.patch_text = render_patch(cf)
        out.append(cf)
    return out


def render_patch(cf: ChangedFile, redact: set[int] | frozenset[int] = frozenset()) -> str:
    """Dựng lại patch từ rows. Dòng có số nằm trong `redact` bị thay bằng REDACTED (giữ dấu +/khoảng trắng đầu dòng)."""
    return "\n".join((row[:1] + REDACTED) if (no is not None and no in redact) else row for no, row in cf.rows)


def commentable_line(cf: ChangedFile, line: int) -> int | None:
    """GitHub chỉ cho comment vào dòng nằm trong diff. Nếu lệch, snap về dòng gần nhất (<=3 dòng)."""
    if line in cf.added_lines:
        return line
    near = [l for l in cf.added_lines if abs(l - line) <= 3]
    return min(near, key=lambda l: abs(l - line)) if near else None


def _norm(s: str) -> str:
    return " ".join(s.split())


def _match_runs(seq: list[tuple[int, str]], target: list[str]) -> list[int]:
    """Số dòng bắt đầu của mọi đoạn LIÊN TIẾP trong seq khớp đúng target."""
    hits = []
    n = len(target)
    for i in range(len(seq) - n + 1):
        if all(seq[i + j][1] == target[j] for j in range(n)):
            hits.append(seq[i][0])
    return hits


def resolve_anchor(cf: ChangedFile, existing_code: str, hint_line: int = 0) -> int | None:
    """Tìm dòng mà `existing_code` (LLM chép nguyên văn từ diff) nằm ở đó — bằng so khớp chuỗi.

    Vì sao: LLM hiểu code tốt nhưng ĐẾM DÒNG kém. Bắt nó chép lại đoạn code rồi để phần mềm
    tự tìm vị trí thì bền hơn nhiều so với tin con số nó đưa (ý tưởng của open-code-review).
    Thứ tự thử:
      1. khớp cả đoạn, đúng thứ tự, bỏ qua khác biệt khoảng trắng và dòng trống;
      2. thử lại sau khi bỏ dấu +/- đầu dòng (LLM hay chép cả ký hiệu của diff);
      3. khớp dòng dài nhất của đoạn như một chuỗi con (LLM hay chép thiếu đầu/cuối dòng).
    Nhiều chỗ khớp -> ưu tiên dòng '+', rồi dòng gần `hint_line` (số dòng LLM đưa) nhất.
    Không khớp -> None; caller quay về số dòng của LLM.
    """
    if not existing_code or not cf.new_lines:
        return None
    seq = [(n, _norm(c)) for n, c in sorted(cf.new_lines.items()) if _norm(c)]
    raw = [l for l in existing_code.splitlines() if l.strip()]
    if not raw:
        return None
    variants = [[_norm(l) for l in raw]]
    stripped = [_norm(l.lstrip()[1:]) if l.lstrip()[:1] in "+-" else _norm(l) for l in raw]
    stripped = [t for t in stripped if t]
    if stripped and stripped != variants[0]:
        variants.append(stripped)

    hits: list[int] = []
    for target in variants:
        hits = _match_runs(seq, target)
        if hits:
            break
    if not hits:
        longest = max(variants[-1], key=len)
        if len(longest) >= 8:
            hits = [n for n, c in seq if longest in c]
    if not hits:
        return None
    preferred = [h for h in hits if h in cf.added_only] or hits
    return min(preferred, key=lambda n: abs(n - hint_line)) if hint_line else preferred[0]


def anchor_finding(f, cf: ChangedFile | None) -> str:
    """Đặt f.line và f.anchor. Trả về cách ghim: code | line | snap | none."""
    if cf is None:
        f.anchor = "none"
        return f.anchor
    line = resolve_anchor(cf, f.existing_code, f.line)
    if line:
        f.line, f.anchor = line, "code"
    elif f.line in cf.added_lines:
        f.anchor = "line"
    else:
        snapped = commentable_line(cf, f.line)
        if snapped:
            f.line, f.anchor = snapped, "snap"
        else:
            f.anchor = "none"
    return f.anchor


def file_context(repo_dir: str, path: str, lines: set[int], radius: int = 30, max_chars: int = 0,
                 redact: set[int] | frozenset[int] = frozenset()) -> str:
    """Lấy nội dung file quanh các dòng thay đổi (±radius) để đưa cho agent.

    max_chars=0 nghĩa là KHÔNG cắt ở đây: việc giới hạn kích thước do selection.build_batches làm,
    nơi mọi lần cắt đều được ghi nhận. (Trước đây hàm này tự cắt 12000 ký tự mà không báo ai.)
    """
    if ".." in path.replace("\\", "/").split("/"):
        return ""
    try:
        with open(os.path.join(repo_dir, path), encoding="utf-8", errors="replace") as f:
            src = f.read().splitlines()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return ""

    def show(i: int) -> str:
        return REDACTED if i in redact else src[i - 1]

    if not lines:
        text = "\n".join(f"{i:5d}| {show(i)}" for i in range(1, min(len(src), 200) + 1))
    else:
        keep = set()
        for l in lines:
            keep.update(range(max(1, l - radius), min(len(src), l + radius) + 1))
        chunks, prev = [], None
        for i in sorted(keep):
            if prev is not None and i != prev + 1:
                chunks.append("      | ...")
            chunks.append(f"{i:5d}| {show(i)}")
            prev = i
        text = "\n".join(chunks)
    return text[:max_chars] if max_chars else text
