"""doc_to_md composite skill.

PDF → Mistral OCR API → 페이지별 Markdown 연결 → doc.md (+ 이미지 assets).
수식(LaTeX), 표(HTML), 멀티컬럼 레이아웃 보존이 Mistral OCR 측에서 됨.
"""

from pathlib import Path

from codex_runner import CodexRunError

from _lib.mistral_ocr import run_doc_to_md


_ACCEPTED_EXTS = (".pdf",)


def normalize(paths: list[Path]) -> dict[str, Path]:
    """PDF 파일 유효성 검증. composite skill 이라 run() 이 직접
    input_paths 를 쓰므로 반환 dict 는 쓰이지 않음."""
    files = [p for p in paths if p.suffix.lower() in _ACCEPTED_EXTS]
    if not files:
        raise CodexRunError("doc_to_md: PDF 파일 필요")
    return {f"input_{i}{p.suffix.lower()}": p for i, p in enumerate(files)}


expected_outputs = ["doc.md"]

run = run_doc_to_md
