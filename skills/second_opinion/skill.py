"""second_opinion — Codex 로 Claude 산출물을 재검토.

일반 스킬 (composite 아님): harness 가 prompt + inputs 로 Codex 를 자동 실행.
backend=codex, batch=false 이므로 리뷰 문서 1개씩 호출.
"""

from codex_runner import CodexRunError, single


# inputs/input.md 규약 — harness 가 첫 파일을 이 이름으로 매핑
normalize = single("input.md")
expected_outputs = ["critique.md"]
