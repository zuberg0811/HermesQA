"""Sinh report Markdown (và summary body cho PR review)."""
from collections import Counter
from jinja2 import Template
from app.review.schemas import Finding, SEVERITY_RANK

TEMPLATE = Template("""# HermesQA Review Report — PR #{{ pr.pr_number }}: {{ pr.title }}

**Repo:** {{ pr.owner }}/{{ pr.repo }} · **Commit:** `{{ pr.head_sha[:8] }}` · **Vai đã chạy:** {{ roles | join(", ") }}
{% if tools %}
**Static tools:** {% for t, st in tools.items() %}{% if st.state == "ok" %}{{ t }} ✓ {{ st.findings }}{% elif st.state == "failed" %}**{{ t }} ✗ THẤT BẠI**{% else %}{{ t }} – không áp dụng{% endif %}{% if not loop.last %} · {% endif %}{% endfor %}
{% if tools.values() | selectattr("state", "equalto", "failed") | list %}
> ⚠️ Có công cụ static thất bại — kết quả dưới đây **thiếu** phần của công cụ đó, không phải "không có vấn đề".
{% endif %}{% endif %}

## Tổng quan
{% for r, s in summaries.items() %}- **{{ r }}:** {{ s }}
{% endfor %}
| Severity | Số lượng |
|---|---|
{% for sev in ["critical","high","medium","low"] %}| {{ sev }} | {{ counts.get(sev, 0) }} |
{% endfor %}
## Chi tiết ({{ findings | length }} phát hiện)
{% for f in findings %}
### {{ loop.index }}. [{{ f.severity | upper }}] {{ f.title }}
- **File:** `{{ f.file }}:{{ f.line }}` · **Loại:** {{ f.category }} · **Nguồn:** {{ f.role }} ({{ "%.0f" % (f.confidence*100) }}%)
- {{ f.explanation }}
{% if f.suggested_fix %}- **Gợi ý sửa:**
```
{{ f.suggested_fix }}
```
{% endif %}{% endfor %}
{% if not findings %}Không phát hiện vấn đề đáng kể. 🎉{% endif %}

---
_Tạo bởi HermesQA · static tools: semgrep, bandit, gitleaks, hadolint, ruff · LLM: {{ model }}_
""")


SCOPE_BLOCK = Template("""{% if skipped %}
## Phạm vi review
Các file sau **không** được LLM review (hoặc chỉ được review một phần). Không có nhận xét ở đây
không có nghĩa là không có vấn đề:
{% for s in skipped %}- {{ s.describe() }}
{% endfor %}{% endif %}{% if removed %}
<details><summary>{{ removed | length }} nhận xét của LLM bị loại sau khi kiểm chứng</summary>

{% for r in removed %}- `{{ r.file }}:{{ r.line }}` {{ r.title }} — căn cứ {{ r.ground }}: {{ r.check }}
{% endfor %}
</details>
{% endif %}""")


def build_markdown(pr: dict, roles: list[str], summaries: dict, findings: list[Finding], model: str,
                   tools: dict | None = None, skipped: list | None = None, removed: list | None = None) -> str:
    findings = sorted(findings, key=lambda f: (-SEVERITY_RANK[f.severity], f.file, f.line))
    md = TEMPLATE.render(pr=pr, roles=roles, summaries=summaries, findings=findings,
                         counts=Counter(f.severity for f in findings), model=model, tools=tools)
    scope = SCOPE_BLOCK.render(skipped=skipped or [], removed=removed or [])
    if scope.strip():
        marker = "\n---\n_Tạo bởi HermesQA"
        head, sep, tail = md.rpartition(marker)
        md = f"{head}\n{scope}{sep}{tail}" if sep else md + "\n" + scope
    return md


def build_pr_summary(summaries: dict, inline: list[Finding], overflow: list[Finding], report_url: str | None,
                     skipped: list | None = None, already_posted: int = 0) -> str:
    counts = Counter(f.severity for f in inline + overflow)
    lines = ["## 🤖 HermesQA Review", ""]
    for r, s in summaries.items():
        lines.append(f"- **{r}:** {s}")
    lines += ["", f"**Phát hiện:** {counts.get('critical',0)} critical · {counts.get('high',0)} high · "
              f"{counts.get('medium',0)} medium · {counts.get('low',0)} low",
              f"Đã comment inline {len(inline)} mục."]
    if already_posted:
        lines.append(f"{already_posted} nhận xét từ lần review trước vẫn còn hiệu lực và không được đăng lại.")
    if skipped:
        lines += ["", f"⚠️ {len(skipped)} file không được review đầy đủ:"]
        lines += [f"- {s.describe()}" for s in skipped[:15]]
        if len(skipped) > 15:
            lines.append(f"- … và {len(skipped) - 15} file khác (xem report đầy đủ)")
    if overflow:
        lines += ["", "<details><summary>Các phát hiện khác (không comment inline)</summary>", ""]
        lines += [f"- `{f.file}:{f.line}` [{f.severity}] {f.title}" for f in overflow[:30]]
        if len(overflow) > 30:
            lines.append(f"- … và {len(overflow) - 30} phát hiện khác (xem report đầy đủ)")
        lines += ["", "</details>"]
    if report_url:
        lines.append(f"\n📄 Report đầy đủ: {report_url}")
    lines.append("\n<sub>Phản hồi 👍/👎 vào từng comment để HermesQA học convention của repo.</sub>")
    return "\n".join(lines)
