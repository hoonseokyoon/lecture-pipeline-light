#!/bin/bash
# glossary skill용 WSL venv 부트스트랩 (idempotent)
# 캐시: $HOME/.cache/lecture-pipeline/glossary
# 출력: venv bin 절대경로 (stdout 마지막 라인)
set -e

VENV="$HOME/.cache/lecture-pipeline/glossary"
STAMP="$VENV/.ready"
REQ="$(cd "$(dirname "$0")" && pwd)/requirements.txt"

# Fast path: 이미 준비됨 + requirements 변경 없음
if [ -f "$STAMP" ] && [ ! "$REQ" -nt "$STAMP" ]; then
    echo "$VENV/bin"
    exit 0
fi

# uv 설치 확인
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    echo "[bootstrap] uv 설치 중..." >&2
    curl -LsSf https://astral.sh/uv/install.sh | sh >&2
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "[bootstrap] venv 준비: $VENV" >&2
uv venv --quiet "$VENV"
echo "[bootstrap] 패키지 설치..." >&2
uv pip install --quiet --python "$VENV/bin/python" -r "$REQ"
touch "$STAMP"
echo "[bootstrap] 완료" >&2

echo "$VENV/bin"
