"""md_to_handout composite skill.

Markdown (doc_to_md 산출물) + sibling `<stem>-assets/` figure → Gemini 3 Pro 로
`LectureOutline` JSON 생성. 밀도 규칙 (핸드아웃 < 대본 < 원문) 은 prompt.txt +
Pydantic schema 에서 강제.
"""

from pathlib import Path

from codex_runner import CodexRunError

from _lib.outline import run_md_to_handout


_ACCEPTED_EXTS = (".md",)


def normalize(paths: list[Path]) -> dict[str, Path]:
    """Markdown 파일 유효성 검증. composite skill 이라 run() 이 직접
    input_paths 를 쓰므로 반환 dict 는 contract 유지용."""
    files = [p for p in paths if p.suffix.lower() in _ACCEPTED_EXTS]
    if not files:
        raise CodexRunError("md_to_handout: Markdown(.md) 파일 필요")
    return {f"input_{i}.md": p for i, p in enumerate(files)}


expected_outputs = ["outline.json"]

run = run_md_to_handout
