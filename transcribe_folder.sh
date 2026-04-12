#!/bin/bash
# 폴더 음성 일괄 전사 + 교정
# 사용법:
#   bash transcribe_folder.sh /path/to/folder
#   bash transcribe_folder.sh /path/to/folder --nb-workers 1 --codex-workers 2
#   bash transcribe_folder.sh /path/to/folder --no-correct
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python "$SCRIPT_DIR/transcribe_folder.py" "$@"
