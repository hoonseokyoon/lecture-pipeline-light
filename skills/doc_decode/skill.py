"""doc_decode composite skill.

PDF → 페이지별 Codex agent orchestration → structured markdown + assets.
각 페이지에 대해 Codex agent 가 detect/annotate 도구만으로 objects.json 을 채우고,
최종적으로 deterministic assembler 가 doc.md 를 만든다.
"""

from pathlib import Path

from codex_runner import CodexRunError

from _lib.pipeline import run_doc_decode


_ACCEPTED_EXTS = (".pdf",)


def normalize(paths: list[Path]) -> dict[str, Path]:
    """PDF 파일 목록을 workdir 에 매핑. composite skill 이라 run() 이 직접
    input_paths 를 쓰므로 이 dict 는 사실상 쓰이지 않지만 유효성 검증은 한다.
    """
    files = [p for p in paths if p.suffix.lower() in _ACCEPTED_EXTS]
    if not files:
        raise CodexRunError("doc_decode: PDF 파일 필요")
    return {f"input_{i}{p.suffix.lower()}": p for i, p in enumerate(files)}


expected_outputs = ["doc.md", "structure.json"]

# composite — codex_runner.run_skill 이 skill.run 으로 위임
run = run_doc_decode
