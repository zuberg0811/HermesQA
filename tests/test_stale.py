"""app.review.stale: khoá/chứng chỉ hết hạn và code lỗi thời. Không cần mạng/LLM/Docker; ngày "hôm nay" cố định."""
import base64
import json
import os
from datetime import date, datetime, timedelta, timezone

from app.review import calibrate, postprocess, stale
from app.review.diff_utils import ChangedFile, parse_diff

TODAY = date(2026, 10, 29)


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")


def _jwt(exp_days: int) -> str:
    exp = int((datetime.combine(TODAY, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=exp_days)).timestamp())
    return f"{_b64({'alg': 'HS256', 'typ': 'JWT'})}.{_b64({'sub': 'svc', 'exp': exp})}.c2lnbmF0dXJlX2JpdHM"


def _cf(tmp_path, path: str, content: str, added: set[int] | None = None) -> ChangedFile:
    """Ghi file vào repo giả và dựng ChangedFile: mọi dòng là ngữ cảnh, `added` là các dòng '+' (mặc định: tất cả)."""
    full = tmp_path / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    lines = content.splitlines()
    nos = set(range(1, len(lines) + 1))
    return ChangedFile(path=path, added_lines=nos, new_lines={i + 1: t for i, t in enumerate(lines)},
                       added_only=added if added is not None else nos)


def _ids(findings):
    return sorted(f.title.split(":", 1)[0] for f in findings)


# ---------------------------------------------------------------- khoá / chứng chỉ

def test_jwt_expired_and_expiring_soon(tmp_path):
    src = f'TOKEN_OLD = "{_jwt(-40)}"\nTOKEN_SOON = "{_jwt(10)}"\nTOKEN_OK = "{_jwt(400)}"\n'
    out = stale.scan([_cf(tmp_path, "app/auth.py", src)], str(tmp_path), today=TODAY)
    jwt = [f for f in out if f.title.startswith("KEY-JWT")]
    assert [(f.line, f.severity) for f in jwt] == [(1, "high"), (2, "medium")]
    assert "2026-09-19" in jwt[0].title and "còn 10 ngày" in jwt[1].title
    assert jwt[0].source == "stale" and jwt[0].role == "static:stale" and jwt[0].anchor == "tool"
    assert jwt[0].existing_code.startswith("TOKEN_OLD")


def test_jwt_outside_hunk_still_reported_but_fixture_in_tests_ignored(tmp_path):
    src = f'TOKEN = "{_jwt(-5)}"\nX = 1\n'
    cf = _cf(tmp_path, "app/auth.py", src, added={2})          # diff chỉ đụng dòng 2
    assert _ids(stale.scan([cf], str(tmp_path), today=TODAY)) == ["KEY-JWT"]
    assert stale.scan([_cf(tmp_path, "tests/test_auth.py", src)], str(tmp_path), today=TODAY) == []


def test_jwt_without_exp_or_example_is_ignored(tmp_path):
    no_exp = f"{_b64({'alg': 'HS256'})}.{_b64({'sub': 'x'})}.c2lnbmF0dXJlX2JpdHM"
    src = f'A = "{no_exp}"\n# example token: "{_jwt(-5)}"\n'
    assert stale.scan([_cf(tmp_path, "a.py", src)], str(tmp_path), today=TODAY) == []


def _self_signed_pem(not_after: datetime) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test.local")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(not_after - timedelta(days=365))
            .not_valid_after(not_after).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_certificate_expired(tmp_path):
    pem = _self_signed_pem(datetime(2026, 1, 15, tzinfo=timezone.utc))
    src = "CA_BUNDLE = '''\n" + pem + "'''\n"
    out = stale.scan([_cf(tmp_path, "config/tls.py", src)], str(tmp_path), today=TODAY)
    cert = [f for f in out if f.title.startswith("KEY-CERT")]
    assert len(cert) == 1 and cert[0].line == 2 and cert[0].severity == "high" and "2026-01-15" in cert[0].title


def test_certificate_valid_is_silent(tmp_path):
    pem = _self_signed_pem(datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert stale.scan([_cf(tmp_path, "config/tls.py", pem)], str(tmp_path), today=TODAY) == []


def test_dated_key_annotation(tmp_path):
    src = ('API_KEY = "abc"  # expires 2026-03-01\n'
           'LICENSE_KEY = "zzz"  # valid until 2026-11-10\n'
           'RELEASE = "2024-01-01"  # ngày phát hành, không phải hạn khoá\n'
           'OTHER_KEY = "k"  # expires 2027-12-31\n')
    out = stale.scan([_cf(tmp_path, "settings.py", src)], str(tmp_path), today=TODAY)
    kd = [f for f in out if f.title.startswith("KEY-DATE")]
    assert [(f.line, f.severity) for f in kd] == [(1, "high"), (2, "medium")]


# ---------------------------------------------------------------- code lỗi thời

def test_deprecated_api_only_on_added_lines_unless_scope_file(tmp_path):
    src = "import datetime\nnow = datetime.datetime.utcnow()\nlater = datetime.datetime.utcnow()\n"
    cf = _cf(tmp_path, "app/t.py", src, added={3})
    assert [f.line for f in stale.scan([cf], str(tmp_path), today=TODAY)] == [3]
    assert [f.line for f in stale.scan([cf], str(tmp_path), today=TODAY, scope="file")] == [2, 3]


def test_python_context_rules(tmp_path):
    src = ("import time, re, requests\n"
           "async def handler():\n"
           "    time.sleep(1)\n"                     # trong coroutine -> báo
           "    requests.get(URL)\n"                 # gọi mạng đồng bộ trong coroutine -> báo
           "def sync():\n"
           "    time.sleep(1)\n"                     # hàm thường -> không
           "    s = ''\n"
           "    for x in xs:\n"
           "        s += str(x)\n"                   # nối chuỗi trong loop -> báo
           "        rx = re.compile(x)\n"            # compile trong loop -> báo
           "    s += 'tail'\n"                       # ngoài loop -> không
           "    first = sorted(xs)[0]\n")
    out = stale.scan([_cf(tmp_path, "app/w.py", src)], str(tmp_path), today=TODAY)
    assert {(f.title.split(":")[0], f.line) for f in out} == {
        ("PY-SLEEP-ASYNC", 3), ("PY-BLOCKING-ASYNC", 4), ("PY-STR-CONCAT-LOOP", 9), ("PY-RECOMPILE-LOOP", 10), ("PY-SORTED-FIRST", 12)}


def test_python2_and_removed_apis(tmp_path):
    src = ("import urllib2\nfrom collections import OrderedDict\n"
           "x = collections.Mapping\nfor k, v in d.iteritems(): pass\n"
           "# xrange(3) trong comment thì không báo\nlogger.warn('x')\n")
    out = stale.scan([_cf(tmp_path, "old.py", src)], str(tmp_path), today=TODAY)
    assert _ids(out) == ["PY-COLLECTIONS-ABC", "PY-LOGGER-WARN", "PY2-DICT-ITER", "PY2-IMPORT"]
    assert all(f.category == "bug" for f in out)


def test_js_go_java_rules(tmp_path):
    js = "const b = new Buffer(8);\nReactDOM.render(<App/>, el);\nitems.forEach(async (i) => { await f(i); });\nconst y = d.getYear();\n"
    go = 'import "golang.org/x/net/context"\nb, _ := ioutil.ReadAll(r)\nrand.Seed(1)\n'
    java = "Integer n = new Integer(5);\nimport org.apache.log4j.Logger;\n"
    out = stale.scan([_cf(tmp_path, "a.js", js), _cf(tmp_path, "m.go", go), _cf(tmp_path, "A.java", java)], str(tmp_path), today=TODAY)
    assert _ids(out) == ["GO-IOUTIL", "GO-RAND-SEED", "GO-XNET-CONTEXT", "JAVA-BOXING-CTOR", "JAVA-LOG4J1",
                         "JS-FOREACH-ASYNC", "JS-GETYEAR", "JS-NEW-BUFFER", "JS-REACTDOM-RENDER"]


def test_eol_image_runner_and_ci_actions(tmp_path):
    docker = "FROM python:3.8-slim-buster AS build\nMAINTAINER me\nFROM python:3.12-slim\nFROM ${BASE}\n"
    wf = ("jobs:\n  t:\n    runs-on: ubuntu-20.04\n    steps:\n      - uses: actions/checkout@v2\n"
          "      - uses: actions/upload-artifact@v3\n      - uses: actions/setup-python@v5\n"
          "        with:\n          python-version: ['3.8', '3.12']\n      - run: echo \"::set-output name=a::b\"\n")
    out = stale.scan([_cf(tmp_path, "Dockerfile", docker), _cf(tmp_path, ".github/workflows/ci.yml", wf)], str(tmp_path), today=TODAY)
    by = {}
    for f in out:
        by.setdefault(f.title.split(":")[0], []).append(f)
    assert [f.line for f in by["EOL-IMAGE"]] == [1, 1]            # Python 3.8 và Debian buster, cùng dòng
    assert "Python 3.8" in by["EOL-IMAGE"][0].title and "2024-10-07" in by["EOL-IMAGE"][0].title
    assert "python:3.14" in by["EOL-IMAGE"][0].suggested_fix     # bản mới nhất còn được hỗ trợ tại TODAY
    assert by["DOCKER-MAINTAINER"][0].line == 2
    assert by["EOL-RUNNER"][0].severity == "high" and "ubuntu-20.04" in by["EOL-RUNNER"][0].title
    assert {f.line for f in by["CI-ACTION-V12"]} == {5} and {f.line for f in by["CI-ARTIFACT-V3"]} == {6}
    assert [f.title for f in by["EOL-RUNTIME"]] == ["EOL-RUNTIME: Python 3.8 đã hết hạn hỗ trợ từ 2024-10-07"]
    assert by["CI-SET-OUTPUT"][0].line == 10
    # 3.12 còn hỗ trợ, FROM với biến ${BASE} không đoán
    assert not any("3.12" in f.title for f in out)


def test_eol_soon_is_low_severity(tmp_path):
    out = stale.scan([_cf(tmp_path, "Dockerfile", "FROM python:3.10\n")], str(tmp_path), today=TODAY)   # EOL 2026-10-31: còn 2 ngày
    assert len(out) == 1 and out[0].severity == "low" and "sắp hết hạn" in out[0].title


def test_abandoned_dependencies(tmp_path):
    files = [_cf(tmp_path, "requirements.txt", "pycrypto==2.6.1\nDjango==3.2.1\nrequests>=2\n"),
             _cf(tmp_path, "package.json", '{\n  "dependencies": {\n    "request": "^2.88.0",\n    "axios": "^1"\n  }\n}\n'),
             _cf(tmp_path, "go.mod", "module x\nrequire (\n\tgithub.com/dgrijalva/jwt-go v3.2.0+incompatible\n)\n"),
             _cf(tmp_path, "pom.xml", "<dependency><artifactId>log4j</artifactId></dependency>\n")]
    out = stale.scan(files, str(tmp_path), today=TODAY)
    assert _ids(out) == ["DEP-ABANDONED", "DEP-ABANDONED", "DEP-ABANDONED", "DEP-ABANDONED", "EOL-RUNTIME"]
    assert {f.file for f in out} == {"requirements.txt", "package.json", "go.mod", "pom.xml"}
    assert any("Django 3.2" in f.title for f in out)
    assert any(f.severity == "high" and f.category == "security" and "pycrypto" in f.title for f in out)


def test_past_date_comparison(tmp_path):
    src = ("if now < datetime(2025, 1, 1):\n    legacy()\n"
           "if now < datetime(2030, 1, 1):\n    ok()\n"
           "CUTOFF = datetime(2020, 1, 1)\n")                 # không có phép so sánh -> không báo
    js = "if (Date.now() > new Date('2024-06-01')) {}\n"
    out = stale.scan([_cf(tmp_path, "f.py", src), _cf(tmp_path, "f.js", js)], str(tmp_path), today=TODAY)
    assert [(f.file, f.line) for f in out] == [("f.py", 1), ("f.js", 1)]
    assert all(f.title.startswith("DATE-PAST") and f.category == "maintainability" for f in out)


def test_clean_code_and_skipped_files(tmp_path):
    files = [_cf(tmp_path, "app/ok.py", "from datetime import datetime, timezone\nnow = datetime.now(timezone.utc)\n"),
             _cf(tmp_path, "README.md", "FROM python:2.7\nnew Buffer(1)\n"),            # tài liệu: bỏ qua
             _cf(tmp_path, "node_modules/x/index.js", "new Buffer(1)\n"),               # generated: bỏ qua
             _cf(tmp_path, "data.bin", "x", added=set())]
    files[-1].is_binary = True
    assert stale.scan(files, str(tmp_path), today=TODAY) == []


def test_falls_back_to_hunk_when_file_missing(tmp_path):
    cf = ChangedFile(path="gone.py", added_lines={7}, new_lines={7: "x = d.has_key('a')"}, added_only={7})
    out = stale.scan([cf], str(tmp_path / "nope"), today=TODAY)
    assert [(f.line, f.title.split(":")[0]) for f in out] == [(7, "PY2-DICT-ITER")]


# ---------------------------------------------------------------- khớp với phần còn lại của pipeline

DIFF = """diff --git a/app/t.py b/app/t.py
--- a/app/t.py
+++ b/app/t.py
@@ -1,2 +1,3 @@
 import datetime
+now = datetime.datetime.utcnow()
 x = 1
"""


def test_integration_with_parse_diff_dedupe_policy_and_calibrate(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "t.py").write_text("import datetime\nnow = datetime.datetime.utcnow()\nx = 1\n", encoding="utf-8")
    files = parse_diff(DIFF, [])
    out = stale.scan(files, str(tmp_path), today=TODAY)
    assert len(out) == 1 and out[0].line == 2
    # LLM báo cùng chỗ -> gộp vào finding stale (tất định) như các static tool khác
    llm = postprocess.Finding(file="app/t.py", line=2, severity="low", category="maintainability", title="utcnow naive",
                              explanation="x", confidence=0.8, source="llm", role="SE")
    kept = postprocess.dedupe(out + [llm])
    assert len(kept) == 1 and kept[0].source == "stale" and "utcnow naive" in kept[0].explanation
    inline, overflow = postprocess.apply_policy(kept, files, 0.6, "medium", 10)
    assert [f.line for f in inline] == [2] and not overflow
    assert postprocess.fingerprint(kept[0]) == postprocess.fingerprint(out[0])
    assert calibrate.rule_key(out[0]) == "stale:PY-UTCNOW"


def test_cli_wires_stale_without_docker(tmp_path):
    """--skip-static vẫn chạy stale; --no-stale tắt. Không gọi LLM (--roles rỗng)."""
    import subprocess, sys
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "t.py").write_text("import datetime\nnow = datetime.datetime.utcnow()\nx = 1\n", encoding="utf-8")
    diff = tmp_path / "in.diff"
    diff.write_text(DIFF, encoding="utf-8")
    env = {**os.environ, "GITHUB_APP_ID": "1", "GITHUB_WEBHOOK_SECRET": "x", "PYTHONIOENCODING": "utf-8"}
    base = [sys.executable, "-m", "app.cli", "--repo", str(repo), "--diff-file", str(diff), "--skip-static", "--roles", "",
            "--no-runlog", "--out", str(tmp_path / "r.md"), "--json", str(tmp_path / "f.json")]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run(base, cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    found = json.loads((tmp_path / "f.json").read_text(encoding="utf-8"))
    assert [f["title"].split(":")[0] for f in found] == ["PY-UTCNOW"]
    assert "stale ✓ 1" in (tmp_path / "r.md").read_text(encoding="utf-8")
    r = subprocess.run(base + ["--no-stale"], cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    assert json.loads((tmp_path / "f.json").read_text(encoding="utf-8")) == []
