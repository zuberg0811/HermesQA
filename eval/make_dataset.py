"""Sinh dataset eval cho HermesQA: 1 repo git với nhánh `main` sạch + N nhánh `case/<id>`,
mỗi nhánh inject 1 lỗi biết trước. Ground truth (file, line, categories) được tính tự động
từ nội dung file nên số dòng luôn chính xác.

Dùng:
  python eval/make_dataset.py --repo eval/dataset-repo --truth eval/ground_truth.json
"""
import argparse
import json
import os
import shutil
import subprocess

# ---------------- Base repo (nhánh main, không lỗi) ----------------

DB_PY = '''import sqlite3


def get_connection(path="app.db"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def get_user(conn, user_id):
    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    return cur.fetchone()


def list_orders(conn, user_id, status):
    cur = conn.execute(
        "SELECT * FROM orders WHERE user_id = ? AND status = ?",
        (user_id, status),
    )
    return cur.fetchall()
'''

UTILS_PY = '''import hashlib
import logging

log = logging.getLogger(__name__)


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


def chunk(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
'''

API_PY = '''import logging

from app.db import get_connection, get_user, list_orders
from app.utils import safe_int

log = logging.getLogger(__name__)


def handle_get_user(request):
    conn = get_connection()
    user = get_user(conn, safe_int(request.get("id")))
    if user is None:
        return {"status": 404}
    return {"status": 200, "user": dict(user)}


def handle_list_orders(request):
    conn = get_connection()
    orders = list_orders(conn, safe_int(request.get("id")), request.get("status", "open"))
    return {"status": 200, "orders": [dict(o) for o in orders]}
'''

TESTS_PY = '''from app.utils import chunk, safe_int


def test_chunk_splits_evenly():
    assert chunk([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]


def test_chunk_handles_remainder():
    assert chunk([1, 2, 3], 2) == [[1, 2], [3]]


def test_safe_int_parses_string():
    assert safe_int("42") == 42


def test_safe_int_falls_back_on_garbage():
    assert safe_int("abc", default=7) == 7
'''

DOCKERFILE = '''FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
USER 1000
CMD ["python", "-m", "app.api"]
'''

REQS = '''flask==3.0.3
requests==2.32.3
'''

BASE = {
    "app/__init__.py": "",
    "app/db.py": DB_PY,
    "app/utils.py": UTILS_PY,
    "app/api.py": API_PY,
    "tests/test_utils.py": TESTS_PY,
    "Dockerfile": DOCKERFILE,
    "requirements.txt": REQS,
}

# ---------------- Cases ----------------
# Mỗi case: id, edits = [(path, old, new)] (old="" nghĩa là file mới / thay cả file),
# truth = [(path, marker, categories, note)] — marker là chuỗi duy nhất nằm trên dòng lỗi.

CASES = []


def case(cid, edits, truth):
    CASES.append({"id": cid, "edits": edits, "truth": truth})


SEC = ["security"]
DEVOPS_C = ["devops", "security"]
BUG = ["bug"]
BUG_STYLE = ["bug", "style"]
TEST_C = ["test"]
STYLE = ["style", "maintainability"]
PERF = ["performance"]

# --- Security (12) ---
case("sqli-concat",
     [("app/db.py",
       'cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))',
       'query = "SELECT * FROM users WHERE id = " + str(user_id)\n    cur = conn.execute(query)')],
     [("app/db.py", '"SELECT * FROM users WHERE id = " +', SEC, "SQL injection qua string concat")])

case("sqli-fstring",
     [("app/db.py",
       '    cur = conn.execute(\n        "SELECT * FROM orders WHERE user_id = ? AND status = ?",\n        (user_id, status),\n    )',
       '    cur = conn.execute(\n        f"SELECT * FROM orders WHERE user_id = {user_id} AND status = \'{status}\'"\n    )')],
     [("app/db.py", 'f"SELECT * FROM orders WHERE user_id =', SEC, "SQL injection qua f-string")])

case("cmd-shell-true",
     [("app/utils.py",
       "import hashlib\nimport logging",
       "import hashlib\nimport logging\nimport subprocess"),
      ("app/utils.py",
       "def safe_int(value, default=0):",
       'def ping(host):\n    return subprocess.run("ping -c 1 " + host, shell=True, capture_output=True)\n\n\ndef safe_int(value, default=0):')],
     [("app/utils.py", 'shell=True', SEC, "Command injection: shell=True với input")])

case("cmd-os-system",
     [("app/api.py",
       "import logging",
       "import logging\nimport os"),
      ("app/api.py",
       "def handle_get_user(request):",
       'def handle_convert(request):\n    os.system("convert " + request.get("path", ""))\n    return {"status": 200}\n\n\ndef handle_get_user(request):')],
     [("app/api.py", 'os.system("convert " +', SEC, "Command injection qua os.system")])

case("secret-aws",
     [("app/utils.py",
       "log = logging.getLogger(__name__)",
       'log = logging.getLogger(__name__)\n\nAWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"\nAWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"')],
     [("app/utils.py", 'AKIAIOSFODNN7EXAMPLE', SEC, "AWS key hard-code"),
      ("app/utils.py", 'wJalrXUtnFEMI', SEC, "AWS secret hard-code")])

case("secret-stripe",
     [("app/api.py",
       "log = logging.getLogger(__name__)",
       'log = logging.getLogger(__name__)\n\nSTRIPE_API_KEY = "sk_' + 'live_51Hn6PqKZvKuzvFa2QqXyZ9abcdefghijklmnop"')],
     [("app/api.py", 'sk_' + 'live_51Hn6', SEC, "Stripe key hard-code")])

case("eval-input",
     [("app/utils.py",
       "def safe_int(value, default=0):",
       "def calculate(expression):\n    return eval(expression)\n\n\ndef safe_int(value, default=0):")],
     [("app/utils.py", 'return eval(expression)', SEC, "eval() trên input người dùng")])

case("yaml-unsafe-load",
     [("app/utils.py",
       "import hashlib\nimport logging",
       "import hashlib\nimport logging\nimport yaml"),
      ("app/utils.py",
       "def safe_int(value, default=0):",
       "def load_config(text):\n    return yaml.load(text)\n\n\ndef safe_int(value, default=0):")],
     [("app/utils.py", 'return yaml.load(text)', SEC, "yaml.load không SafeLoader")])

case("pickle-load",
     [("app/api.py",
       "import logging",
       "import logging\nimport pickle"),
      ("app/api.py",
       "def handle_get_user(request):",
       'def handle_import(request):\n    payload = pickle.loads(request.get("data", b""))\n    return {"status": 200, "items": payload}\n\n\ndef handle_get_user(request):')],
     [("app/api.py", 'pickle.loads(request.get', SEC, "pickle.loads trên dữ liệu không tin cậy")])

case("md5-password",
     [("app/utils.py",
       "    return hashlib.sha256((salt + password).encode()).hexdigest()",
       "    return hashlib.md5(password.encode()).hexdigest()")],
     [("app/utils.py", 'hashlib.md5(password', SEC, "MD5 hash mật khẩu, bỏ salt")])

case("requests-verify-false",
     [("app/api.py",
       "import logging",
       "import logging\n\nimport requests"),
      ("app/api.py",
       "def handle_get_user(request):",
       'def fetch_profile(url):\n    return requests.get(url, verify=False, timeout=10).json()\n\n\ndef handle_get_user(request):')],
     [("app/api.py", 'verify=False', SEC, "Tắt kiểm tra TLS certificate")])

case("flask-debug",
     [("app/api.py",
       "log = logging.getLogger(__name__)",
       'log = logging.getLogger(__name__)\n\n\ndef run_server():\n    from flask import Flask\n    server = Flask(__name__)\n    server.run(host="0.0.0.0", debug=True)')],
     [("app/api.py", 'debug=True', SEC, "Flask debug mode mở trên 0.0.0.0")])

# --- DevOps (8) ---
case("docker-latest",
     [("Dockerfile", "FROM python:3.12-slim", "FROM python:latest")],
     [("Dockerfile", "FROM python:latest", DEVOPS_C, "Không pin version base image")])

case("docker-no-user-shell-cmd",
     [("Dockerfile",
       'USER 1000\nCMD ["python", "-m", "app.api"]',
       "CMD python -m app.api")],
     [("Dockerfile", "CMD python -m app.api", DEVOPS_C, "Chạy root + CMD shell form")])

case("docker-copy-everything",
     [("Dockerfile", "COPY app ./app", "COPY . .")],
     [("Dockerfile", "COPY . .", DEVOPS_C, "COPY toàn bộ context (kèm secrets/.git)")])

case("docker-apt-no-clean",
     [("Dockerfile",
       "WORKDIR /app",
       "WORKDIR /app\nRUN apt-get install curl")],
     [("Dockerfile", "RUN apt-get install curl", DEVOPS_C, "apt-get không update/không clean/không -y")])

case("docker-root-user",
     [("Dockerfile", "USER 1000", "USER root")],
     [("Dockerfile", "USER root", DEVOPS_C, "Chạy container bằng root")])

case("docker-add-not-copy",
     [("Dockerfile", "COPY app ./app", "ADD app ./app")],
     [("Dockerfile", "ADD app ./app", DEVOPS_C, "Dùng ADD thay vì COPY")])

case("reqs-unpinned",
     [("requirements.txt", "flask==3.0.3\nrequests==2.32.3\n", "flask\nrequests\n")],
     [("requirements.txt", "flask", DEVOPS_C + ["maintainability"], "Bỏ pin version dependency")])

case("docker-sudo",
     [("Dockerfile",
       "RUN pip install --no-cache-dir -r requirements.txt",
       "RUN sudo pip install -r requirements.txt")],
     [("Dockerfile", "RUN sudo pip install", DEVOPS_C, "Dùng sudo trong Dockerfile")])

# --- Bug (7) ---
case("mutable-default-arg",
     [("app/utils.py",
       "def safe_int(value, default=0):",
       "def collect(item, bucket=[]):\n    bucket.append(item)\n    return bucket\n\n\ndef safe_int(value, default=0):")],
     [("app/utils.py", "bucket=[]", BUG, "Mutable default argument")])

case("bare-except",
     [("app/utils.py",
       "    except (TypeError, ValueError):\n        return default",
       "    except:\n        return default")],
     [("app/utils.py", "    except:", BUG_STYLE, "Bare except nuốt mọi lỗi (kể cả KeyboardInterrupt)")])

case("off-by-one",
     [("app/utils.py",
       "    return [items[i:i + size] for i in range(0, len(items), size)]",
       "    return [items[i:i + size] for i in range(0, len(items) - 1, size)]")],
     [("app/utils.py", "len(items) - 1", BUG, "Off-by-one: mất phần tử cuối")])

case("eq-none",
     [("app/api.py",
       "    if user is None:",
       "    if user == None:")],
     [("app/api.py", "user == None", BUG_STYLE, "So sánh None bằng == thay vì is")])

case("div-no-zero-check",
     [("app/utils.py",
       "def safe_int(value, default=0):",
       "def average(numbers):\n    return sum(numbers) / len(numbers)\n\n\ndef safe_int(value, default=0):")],
     [("app/utils.py", "sum(numbers) / len(numbers)", BUG, "ZeroDivisionError khi list rỗng")])

case("truthy-or-bug",
     [("app/api.py",
       '    orders = list_orders(conn, safe_int(request.get("id")), request.get("status", "open"))',
       '    status = request.get("status", "open")\n    if status == "open" or "pending":\n        status = "open"\n    orders = list_orders(conn, safe_int(request.get("id")), status)')],
     [("app/api.py", 'or "pending"', BUG, 'Điều kiện luôn True: x == "a" or "b"')])

case("undefined-name",
     [("app/api.py",
       '    return {"status": 200, "orders": [dict(o) for o in orders]}',
       '    return {"status": 200, "orders": [dict(o) for o in orders], "total": total_count}')],
     [("app/api.py", "total_count", BUG_STYLE, "Biến chưa định nghĩa (NameError)")])

# --- Test/QA (4) ---
case("skip-test",
     [("tests/test_utils.py",
       "from app.utils import chunk, safe_int",
       "import pytest\n\nfrom app.utils import chunk, safe_int"),
      ("tests/test_utils.py",
       "def test_chunk_handles_remainder():",
       '@pytest.mark.skip(reason="flaky")\ndef test_chunk_handles_remainder():')],
     [("tests/test_utils.py", '@pytest.mark.skip', TEST_C, "Skip test không có lý do chính đáng")])

case("weak-assert",
     [("tests/test_utils.py",
       "    assert chunk([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]",
       "    assert chunk([1, 2, 3, 4], 2) is not None")],
     [("tests/test_utils.py", "is not None", TEST_C, "Assertion yếu: không kiểm tra giá trị")])

case("new-func-no-test",
     [("app/utils.py",
       "def safe_int(value, default=0):",
       'def parse_duration(text):\n    unit = text[-1]\n    value = int(text[:-1])\n    if unit == "h":\n        return value * 3600\n    if unit == "m":\n        return value * 60\n    return value\n\n\ndef safe_int(value, default=0):')],
     [("app/utils.py", "def parse_duration(text):", TEST_C, "Hàm mới nhiều nhánh logic, không có test")])

case("removed-assert",
     [("tests/test_utils.py",
       '    assert safe_int("abc", default=7) == 7',
       '    safe_int("abc", default=7)')],
     [("tests/test_utils.py", '    safe_int("abc", default=7)', TEST_C, "Test mất assertion, chỉ còn gọi hàm")])

# --- Style / Performance / Maintainability (4) ---
case("string-concat-loop",
     [("app/utils.py",
       "def safe_int(value, default=0):",
       'def build_csv(rows):\n    out = ""\n    for row in rows:\n        out += ",".join(str(c) for c in row) + "\\n"\n    return out\n\n\ndef safe_int(value, default=0):')],
     [("app/utils.py", 'out += ","', PERF, "Nối chuỗi trong vòng lặp O(n^2)")])

case("unused-import",
     [("app/api.py", "import logging", "import json\nimport logging")],
     [("app/api.py", "import json", STYLE, "Import không dùng")])

case("print-not-logging",
     [("app/api.py",
       "    conn = get_connection()\n    user = get_user(conn, safe_int(request.get(\"id\")))",
       '    conn = get_connection()\n    print("fetching user", request.get("id"))\n    user = get_user(conn, safe_int(request.get("id")))')],
     [("app/api.py", 'print("fetching user"', STYLE, "print() thay vì logging")])

case("duplicate-helper",
     [("app/utils.py",
       "def safe_int(value, default=0):",
       "def split_list(values, size):\n    return [values[i:i + size] for i in range(0, len(values), size)]\n\n\ndef safe_int(value, default=0):")],
     [("app/utils.py", "def split_list(values, size):", STYLE, "Trùng lặp logic với chunk()")])


# ---------------- Generator ----------------

def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write(repo, files):
    for path, content in files.items():
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full) or repo, exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)


def _line_of(content: str, marker: str, path: str, cid: str) -> int:
    hits = [i + 1 for i, ln in enumerate(content.splitlines()) if marker in ln]
    if len(hits) != 1:
        raise SystemExit(f"[{cid}] marker {marker!r} xuất hiện {len(hits)} lần trong {path}")
    return hits[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="eval/dataset-repo")
    ap.add_argument("--truth", default="eval/ground_truth.json")
    a = ap.parse_args()

    shutil.rmtree(a.repo, ignore_errors=True)
    os.makedirs(a.repo)
    subprocess.run(["git", "init", "-q", "-b", "main", a.repo], check=True)
    _git(a.repo, "config", "user.email", "eval@hermesqa.local")
    _git(a.repo, "config", "user.name", "HermesQA Eval")
    _git(a.repo, "config", "core.autocrlf", "false")
    _write(a.repo, BASE)
    _git(a.repo, "add", "-A")
    _git(a.repo, "commit", "-q", "-m", "base app (clean)")

    truth_out = {}
    for c in CASES:
        cid = c["id"]
        files = dict(BASE)
        for path, old, new in c["edits"]:
            if old == "":
                files[path] = new
            else:
                if old not in files[path]:
                    raise SystemExit(f"[{cid}] không tìm thấy đoạn cần thay trong {path}")
                files[path] = files[path].replace(old, new, 1)

        branch = f"case/{cid}"
        _git(a.repo, "checkout", "-q", "-b", branch, "main")
        _write(a.repo, files)
        _git(a.repo, "add", "-A")
        _git(a.repo, "commit", "-q", "-m", f"inject: {cid}")
        _git(a.repo, "checkout", "-q", "main")

        truth_out[cid] = {
            "branch": branch,
            "truth": [
                {"file": path, "line": _line_of(files[path], marker, path, cid),
                 "categories": cats, "note": note}
                for path, marker, cats, note in c["truth"]
            ],
        }

    os.makedirs(os.path.dirname(a.truth) or ".", exist_ok=True)
    with open(a.truth, "w", encoding="utf-8") as f:
        json.dump({"repo": a.repo, "cases": truth_out}, f, ensure_ascii=False, indent=1)

    n_truth = sum(len(v["truth"]) for v in truth_out.values())
    print(f"OK: {len(CASES)} cases, {n_truth} ground-truth findings -> {a.truth}")
    print(f"Repo: {a.repo} (nhánh main + {len(CASES)} nhánh case/*)")


if __name__ == "__main__":
    main()
