"""Chạy static analysis trong container ephemeral: read-only source, không network, giới hạn tài nguyên."""
import logging
import os
import docker

from app.config import settings

log = logging.getLogger("hermesqa.sandbox")


def run_static_analysis(repo_dir: str, out_dir: str, timeout: int = 600, targets: list[str] | None = None) -> str:
    """targets: đường dẫn tương đối của các file cần quét (thường là file trong diff).
    Bỏ trống -> quét cả repo. Finding ngoài file thay đổi vốn bị lọc bỏ ở tầng trên,
    nên giới hạn phạm vi cho kết quả tương đương mà nhanh hơn nhiều."""
    os.makedirs(out_dir, exist_ok=True)
    targets_file = os.path.join(out_dir, "targets.txt")
    if targets:
        with open(targets_file, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(sorted(set(targets))) + "\n")
    elif os.path.exists(targets_file):
        os.remove(targets_file)
    client = docker.from_env()
    container = client.containers.run(
        settings.runner_image,
        detach=True,
        network_mode="none",                 # code lạ trong PR không thể gọi ra ngoài
        read_only=False,
        mem_limit="2g",
        nano_cpus=2_000_000_000,             # 2 CPU
        pids_limit=256,
        security_opt=["no-new-privileges"],
        cap_drop=["ALL"],
        volumes={
            os.path.abspath(repo_dir): {"bind": "/src", "mode": "ro"},
            os.path.abspath(out_dir): {"bind": "/out", "mode": "rw"},
        },
        user="0",  # tools cần ghi /out; có thể hạ xuống uid thường nếu chown out_dir
    )
    try:
        res = container.wait(timeout=timeout)
        logs = container.logs().decode(errors="replace")
        log.info("runner exit=%s\n%s", res.get("StatusCode"), logs[-2000:])
        return logs
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass
