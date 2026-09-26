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


def build_markdown(pr: dict, roles: list[str], summaries: dict, findings: list[Finding], model: str,
                   tools: dict | None = None) -> str:
    findings = sorted(findings, key=lambda f: (-SEVERITY_RANK[f.severity], f.file, f.line))
    return TEMPLATE.render(pr=pr, roles=roles, summaries=summaries, findings=findings,
                           counts=Counter(f.severity for f in findings), model=model, tools=tools)


def build_pr_summary(summaries: dict, inline: list[Finding], overflow: list[Finding], report_url: str | None) -> str:
    counts = Counter(f.severity for f in inline + overflow)
    lines = ["## 🤖 HermesQA Review", ""]
    for r, s in summaries.items():
        lines.append(f"- **{r}:** {s}")
    lines += ["", f"**Phát hiện:** {counts.get('critical',0)} critical · {counts.get('high',0)} high · "
              f"{counts.get('medium',0)} medium · {counts.get('low',0)} low",
              f"Đã comment inline {len(inline)} mục."]
    if overflow:
        lines += ["", "<details><summary>Các phát hiện khác (không comment inline)</summary>", ""]
        lines += [f"- `{f.file}:{f.line}` [{f.severity}] {f.title}" for f in overflow[:30]]
        lines += ["", "</details>"]
    if report_url:
        lines.append(f"\n📄 Report đầy đủ: {report_url}")
    lines.append("\n<sub>Phản hồi 👍/👎 vào từng comment để HermesQA học convention của repo.</sub>")
    return "\n".join(lines)
