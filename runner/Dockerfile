# Sandbox image: chỉ chạy static analysis, KHÔNG có network khi run
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl git ca-certificates && rm -rf /var/lib/apt/lists/*
# setuptools<81 để giữ pkg_resources mà semgrep 1.90 cần (python:3.12-slim không kèm sẵn)
RUN pip install --no-cache-dir "setuptools<81" semgrep==1.90.0 ruff==0.6.9 bandit==1.7.10
RUN curl -sSL -o /usr/local/bin/hadolint https://github.com/hadolint/hadolint/releases/download/v2.12.0/hadolint-Linux-x86_64 \
    && chmod +x /usr/local/bin/hadolint
RUN curl -sSL https://github.com/gitleaks/gitleaks/releases/download/v8.21.2/gitleaks_8.21.2_linux_x64.tar.gz \
    | tar -xz -C /usr/local/bin gitleaks
# Bake sẵn ruleset semgrep vào image: lúc run sandbox KHÔNG có network nên --config auto sẽ không tải được rules
RUN git clone --depth 1 https://github.com/semgrep/semgrep-rules /opt/semgrep-rules \
    && rm -rf /opt/semgrep-rules/.git /opt/semgrep-rules/stats /opt/semgrep-rules/.github \
    # các YAML không phải rule sẽ làm semgrep từ chối cả thư mục config
    && rm -f /opt/semgrep-rules/.pre-commit-config.yaml /opt/semgrep-rules/template.yaml
COPY run_static.sh /run_static.sh
RUN chmod +x /run_static.sh
WORKDIR /src
ENTRYPOINT ["/run_static.sh"]
