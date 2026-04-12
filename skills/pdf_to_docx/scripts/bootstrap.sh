#!/bin/bash
# pdf_to_docx skill용 WSL venv 부트스트랩 (idempotent)
# - 캐시: $HOME/.cache/lecture-pipeline/pdf_to_docx
# - 출력: venv bin 디렉토리 절대경로 (stdout 마지막 라인)
# - 로그: stderr
set -e

VENV="$HOME/.cache/lecture-pipeline/pdf_to_docx"
STAMP="$VENV/.ready"
REQ="$(cd "$(dirname "$0")" && pwd)/requirements.txt"

# 시스템 의존성 체크 — poppler는 apt 필요 (venv로 못 들어감)
if ! command -v pdftoppm >/dev/null 2>&1; then
    echo "ERROR: poppler-utils 없음." >&2
    echo "WSL에서 1회 실행: sudo apt-get install -y poppler-utils" >&2
    exit 1
fi

# Fast path: venv 준비됨 + requirements 변경 없음
if [ -f "$STAMP" ] && [ ! "$REQ" -nt "$STAMP" ]; then
    echo "$VENV/bin"
    exit 0
fi

# uv가 없으면 설치 (~/.local/bin/uv)
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
