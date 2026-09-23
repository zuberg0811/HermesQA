#!/usr/bin/env bash
# Chạy trong sandbox (--network none). Đọc /src (read-only), ghi JSON vào /out
# Nếu /out/targets.txt tồn tại: chỉ quét các file trong đó (đường dẫn tương đối với /src).
# Worker chỉ giữ finding thuộc file thay đổi, nên quét cả repo là lãng phí.
set -u
OUT=/out
mkdir -p "$OUT"

TARGETS_FILE="$OUT/targets.txt"
PY_TARGETS=()
DOCKER_TARGETS=()
ALL_TARGETS=()
if [ -s "$TARGETS_FILE" ]; then
  while IFS= read -r rel; do
    rel="${rel%$'\r'}"
    [ -z "$rel" ] && continue
    abs="/src/$rel"
    [ -f "$abs" ] || continue
    ALL_TARGETS+=("$abs")
    case "$rel" in
      *.py) PY_TARGETS+=("$abs") ;;
    esac
    case "$(basename "$rel")" in
      Dockerfile*) DOCKER_TARGETS+=("$abs") ;;
    esac
  done < "$TARGETS_FILE"
fi
if [ ${#ALL_TARGETS[@]} -eq 0 ]; then    # không có danh sách -> quét cả cây như trước
  ALL_TARGETS=(/src)
  PY_TARGETS=(/src)
  mapfile -t DOCKER_TARGETS < <(find /src -name 'Dockerfile*' -not -path '*/node_modules/*')
fi

# Chọn rule theo ngôn ngữ có mặt trong diff. Nạp cả 2150 rule mất ~350s, chỉ nạp
# python+generic mất ~117s cho kết quả tương đương với file Python.
rule_dirs_for() {
  case "$(basename "$1")" in Dockerfile*) echo dockerfile; return ;; esac
  case "$1" in
    *.py) echo python ;;
    *.js|*.jsx|*.mjs|*.cjs) echo javascript ;;
    *.ts|*.tsx) echo typescript ;;
    *.go) echo go ;;   *.java) echo java ;;   *.rb) echo ruby ;;
    *.php) echo php ;; *.cs) echo csharp ;;   *.rs) echo rust ;;
    *.c|*.h|*.cpp|*.cc) echo c ;;             *.kt) echo kotlin ;;
    *.swift) echo swift ;; *.scala) echo scala ;; *.sol) echo solidity ;;
    *.tf) echo terraform ;; *.sh|*.bash) echo bash ;; *.html) echo html ;;
    *.json) echo json ;;   *.yaml|*.yml) echo yaml ;;
    *.ex|*.exs) echo elixir ;; *.clj) echo clojure ;; *.ml) echo ocaml ;;
  esac
}

SEMGREP_ARGS=()
if [ -d /opt/semgrep-rules ]; then
  langs=$(for t in "${ALL_TARGETS[@]}"; do rule_dirs_for "$t"; done | sort -u)
  for l in $langs generic; do
    [ -d "/opt/semgrep-rules/$l" ] && SEMGREP_ARGS+=(--config "/opt/semgrep-rules/$l")
  done
  [ ${#SEMGREP_ARGS[@]} -eq 0 ] && SEMGREP_ARGS=(--config /opt/semgrep-rules)
else
  SEMGREP_ARGS=(--config auto)   # chỉ dùng được khi có network
fi

echo "[runner] semgrep (${#ALL_TARGETS[@]} target, rules: ${SEMGREP_ARGS[*]})"
semgrep scan "${SEMGREP_ARGS[@]}" --json --quiet --metrics=off -o "$OUT/semgrep.json" "${ALL_TARGETS[@]}" 2>/dev/null || echo '{"results":[]}' > "$OUT/semgrep.json"

echo "[runner] bandit"
if [ ${#PY_TARGETS[@]} -gt 0 ]; then
  bandit -r -f json -o "$OUT/bandit.json" -q "${PY_TARGETS[@]}" 2>/dev/null || true
fi
[ -s "$OUT/bandit.json" ] || echo '{"results":[]}' > "$OUT/bandit.json"

echo "[runner] gitleaks"
gitleaks detect --source /src --no-git --report-format json --report-path "$OUT/gitleaks.json" --exit-code 0 >/dev/null 2>&1 || echo '[]' > "$OUT/gitleaks.json"

echo "[runner] hadolint"
: > "$OUT/hadolint.json"
for f in ${DOCKER_TARGETS[@]+"${DOCKER_TARGETS[@]}"}; do
  [ -f "$f" ] || continue
  hadolint -f json "$f" >> "$OUT/hadolint.json" 2>/dev/null || true
done

echo "[runner] ruff"
if [ ${#PY_TARGETS[@]} -gt 0 ]; then
  ruff check "${PY_TARGETS[@]}" --output-format json --exit-zero > "$OUT/ruff.json" 2>/dev/null || echo '[]' > "$OUT/ruff.json"
else
  echo '[]' > "$OUT/ruff.json"
fi
echo "[runner] done"
