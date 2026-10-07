"""Render tai lieu Markdown cua du an ra PDF kho A4.

Dung chung cho BAO_CAO_TIEN_DO.md, docs/DE_XUAT_*.md va cac bao cao khac, de moi
ban PDF co cung dinh dang.

Cach dung:
    python tools/md2pdf.py BAO_CAO_TIEN_DO.md
    python tools/md2pdf.py docs/DE_XUAT_CAI_TIEN_TU_OPEN_CODE_REVIEW.md
    python tools/md2pdf.py bao_cao.md ten_khac.pdf --keep-html

Khong co doi so thu hai thi PDF ghi canh file .md voi cung ten.

Yeu cau:
  - thu vien `markdown` (da co trong requirements.txt)
  - Microsoft Edge (Windows) hoac Chrome/Chromium: dung o che do headless de in.
    Khong can cai wkhtmltopdf hay LaTeX.

LUU Y ve bang: python-markdown cat bang lam hai khi co DONG TRONG giua cac hang.
Script tu phat hien va canh bao truoc khi render, vi loi nay chi lo ra trong PDF
(hang bi roi ra thanh chu tho con dau |), rat de bo sot khi chi doc .md.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import markdown
except ImportError:
    sys.exit("Thieu thu vien: pip install markdown")

CSS = """
@page { size: A4; margin: 16mm 14mm; }
* { box-sizing: border-box; }
body {
  font-family: "Segoe UI", "Times New Roman", serif;
  font-size: 10.5pt; line-height: 1.5; color: #1a1a1a; margin: 0;
}
h1 { font-size: 19pt; border-bottom: 2.5px solid #2c3e50; padding-bottom: 6px;
     margin: 0 0 14px; color: #1a2a3a; }
h2 { font-size: 14pt; margin: 20px 0 8px; padding-bottom: 3px;
     border-bottom: 1px solid #ccd; color: #22364a; page-break-after: avoid; }
h3 { font-size: 12pt; margin: 15px 0 6px; color: #2c3e50; page-break-after: avoid; }
h4 { font-size: 10.8pt; margin: 12px 0 5px; color: #34495e; page-break-after: avoid; }
p { margin: 6px 0; text-align: justify; }
table { border-collapse: collapse; width: 100%; margin: 9px 0;
        font-size: 9pt; page-break-inside: avoid; }
th, td { border: 1px solid #b8c4d0; padding: 4px 6px; text-align: left;
         vertical-align: top; }
th { background: #eaeff4; font-weight: 600; }
tr:nth-child(even) td { background: #f8fafb; }
code { font-family: Consolas, "Courier New", monospace; font-size: 8.8pt;
       background: #f1f3f5; padding: 1px 3px; border-radius: 2px;
       border: 1px solid #e3e6e8; }
pre { background: #f6f8fa; border: 1px solid #d7dde3; border-radius: 3px;
      padding: 8px 10px; overflow-x: auto; page-break-inside: avoid; }
pre code { background: none; border: none; font-size: 8.3pt; padding: 0; }
blockquote { border-left: 3.5px solid #5b8fb5; background: #f4f8fb;
             margin: 9px 0; padding: 7px 12px; color: #2b3a47;
             page-break-inside: avoid; }
blockquote p { margin: 3px 0; }
ul, ol { margin: 6px 0; padding-left: 22px; }
li { margin: 3px 0; }
hr { border: none; border-top: 1px solid #ccd4dc; margin: 16px 0; }
strong { color: #10243a; }
a { color: #1a5c8a; text-decoration: none; }
"""

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def warn_blank_lines_in_tables(text: str) -> list[int]:
    """So dong trong nam GIUA hai hang bang -> markdown se cat bang lam hai."""
    lines = text.split("\n")
    bad = []
    for i in range(1, len(lines) - 1):
        if (not lines[i].strip()
                and lines[i - 1].startswith("|")
                and lines[i + 1].startswith("|")):
            bad.append(i + 1)
    return bad


def md_to_html(text: str, title: str) -> str:
    body = markdown.markdown(
        text, extensions=["tables", "fenced_code", "toc", "sane_lists"])
    return ('<!DOCTYPE html><html lang="vi"><head><meta charset="utf-8">'
            f"<title>{title}</title><style>{CSS}</style></head>"
            f"<body>{body}</body></html>")


def find_browser() -> str:
    for p in BROWSERS:
        if os.path.isfile(p):
            return p
    for name in ("msedge", "chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    sys.exit("Khong tim thay Edge/Chrome de in PDF. Cai Edge hoac Chrome roi chay lai.")


def html_to_pdf(html_path: str, pdf_path: str) -> None:
    browser = find_browser()
    url = "file:///" + os.path.abspath(html_path).replace("\\", "/")
    proc = subprocess.run(
        [browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={os.path.abspath(pdf_path)}", url],
        capture_output=True, text=True, timeout=180)
    if not os.path.isfile(pdf_path) or os.path.getsize(pdf_path) < 1024:
        sys.exit(f"In PDF that bai.\n{proc.stderr[-800:]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Render Markdown ra PDF A4")
    ap.add_argument("src", help="file .md dau vao")
    ap.add_argument("dst", nargs="?", help="file .pdf dau ra (mac dinh: canh file .md)")
    ap.add_argument("--keep-html", action="store_true", help="giu lai file .html trung gian")
    a = ap.parse_args()

    if not os.path.isfile(a.src):
        sys.exit(f"Khong co file: {a.src}")
    dst = a.dst or os.path.splitext(a.src)[0] + ".pdf"

    with open(a.src, encoding="utf-8") as f:
        text = f.read()

    bad = warn_blank_lines_in_tables(text)
    if bad:
        print(f"CANH BAO: co dong trong giua bang o dong {bad} — markdown se cat bang "
              f"lam hai va cac hang do se thanh chu tho trong PDF. Nen bo cac dong trong do.")

    html = md_to_html(text, os.path.basename(a.src))
    if a.keep_html:
        html_path = os.path.splitext(dst)[0] + ".html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        html_to_pdf(html_path, dst)
    else:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                          encoding="utf-8")
        try:
            tmp.write(html)
            tmp.close()
            html_to_pdf(tmp.name, dst)
        finally:
            os.unlink(tmp.name)

    n_tables = html.count("<table>")
    print(f"OK: {dst} ({os.path.getsize(dst):,} bytes, {n_tables} bang)")


if __name__ == "__main__":
    main()
