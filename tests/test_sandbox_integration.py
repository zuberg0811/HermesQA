"""Integration test cho sandbox static analysis — chay CONTAINER THAT.

Vi sao can test nay: du an da dinh 4 lan bug "cong cu chay ma khong bao gi"
(semgrep chua tung chay, bandit chua tung duoc goi, self-verify luon that bai
trong im lang, ruff chet vi ghi cache vao /src read-only). Bon lan do deu LOT
QUA 4 unit test san co, vi cac test do khong di qua container.

Nguyen tac: kiem phai di dung duong nguoi dung di. Test nay dung sandbox that,
tren file co loi biet truoc, roi doi tung cong cu phai bao it nhat 1 phat hien.

Chay:  pytest tests/test_sandbox_integration.py -v
Bo qua tu dong neu khong co Docker hoac chua build image.
"""
import os
import shutil
import subprocess
import tempfile

import pytest

from app.config import settings
from app.review.static_tools import collect, tool_status
from app.sandbox import run_static_analysis


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "image", "inspect", settings.runner_image],
                       check=True, capture_output=True, timeout=60)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _docker_ready(),
    reason=f"can Docker va image {settings.runner_image} (build: docker build -t ... runner/)",
)

# Moi file cai loi ma DUNG MOT cong cu cu the phai bat duoc.
FILES = {
    # ruff: F401 import thua, E711 so sanh None, E722 bare except
    "app/bad_lint.py": (
        "import json\n"
        "\n"
        "\n"
        "def check(x):\n"
        "    if x == None:\n"
        "        return False\n"
        "    try:\n"
        "        return int(x) > 0\n"
        "    except:\n"
        "        return False\n"
    ),
    # bandit + semgrep: subprocess shell=True, md5
    "app/bad_sec.py": (
        "import hashlib\n"
        "import subprocess\n"
        "\n"
        "\n"
        "def ping(host):\n"
        "    return subprocess.run('ping -c 1 ' + host, shell=True)\n"
        "\n"
        "\n"
        "def digest(pw):\n"
        "    return hashlib.md5(pw.encode()).hexdigest()\n"
    ),
    # hadolint: base image latest, chay bang root, apt khong xoa cache
    "Dockerfile": (
        "FROM python:latest\n"
        "RUN apt-get update && apt-get install -y curl\n"
        "USER root\n"
        "CMD python app.py\n"
    ),
    # gitleaks: khoa GIA, ghep chuoi luc chay vi hai ly do:
    #  - file nguon cua test khong chua mau hoan chinh nen GitHub push
    #    protection khong chan khi commit;
    #  - KHONG dung AKIAIOSFODNN7EXAMPLE: gitleaks co allowlist cho key vi du
    #    trong tai lieu AWS nen no khong bao gi (da kiem chung bang tay).
    "config.py": (
        'AWS_ACCESS_KEY_ID = "' + "AKIA" + 'QYLPMN5HXQZ3TUVWR"\n'
        'AWS_SECRET_ACCESS_KEY = "bQ7xK2mNvR8tYuIoP4aSdFgHjKlZxCvBnM1qWeRt"\n'
    ),
}


@pytest.fixture(scope="module")
def scan():
    """Chay sandbox mot lan, dung chung ket qua cho ca module."""
    repo = tempfile.mkdtemp(prefix="hqa-it-src-")
    out = tempfile.mkdtemp(prefix="hqa-it-out-")
    try:
        for rel, content in FILES.items():
            full = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(full) or repo, exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        run_static_analysis(repo, out, targets=sorted(FILES))
        findings = collect(out)
        yield findings, tool_status(out, findings)
    finally:
        shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


@pytest.mark.parametrize("tool", ["semgrep", "bandit", "gitleaks", "hadolint", "ruff"])
def test_moi_cong_cu_deu_bao_it_nhat_mot_phat_hien(scan, tool):
    """Day la test da thieu suot 4 lan bug im lang truoc do.

    0 phat hien tu mot cong cu tren file CO loi = cong cu do khong chay.
    """
    findings, status = scan
    n = sum(1 for f in findings if f.source == tool)
    assert status[tool]["state"] != "failed", (
        f"{tool} bao that bai trong failed_tools.txt"
    )
    assert n > 0, (
        f"{tool} bao 0 phat hien tren file co loi -> gan nhu chac chan no khong "
        f"chay. Cac nguon co phat hien: {sorted({f.source for f in findings})}"
    )


def test_khong_cong_cu_nao_that_bai(scan):
    _, status = scan
    failed = [t for t, st in status.items() if st["state"] == "failed"]
    assert not failed, f"cong cu that bai: {failed}"


def test_ruff_phan_loai_bug_khac_style(scan):
    """E711/E722 la loi dung-sai, khong phai style. Truoc day bi gan cung 'style'."""
    findings, _ = scan
    ruff = {f.title: f.category for f in findings if f.source == "ruff"}
    assert ruff, "ruff khong bao gi"
    for code in ("E711", "E722"):
        if code in ruff:
            assert ruff[code] == "bug", f"{code} phai la category 'bug', dang la {ruff[code]!r}"
    if "F401" in ruff:
        assert ruff["F401"] == "style"


def test_moi_finding_deu_tro_vao_file_da_quet(scan):
    """Duong dan phai tuong doi voi repo, khong phai /src/... trong container."""
    findings, _ = scan
    for f in findings:
        assert not f.file.startswith("/"), f"duong dan tuyet doi ro ri: {f.file}"
        assert f.line >= 1
