"""lit_search composite skill."""

from pathlib import Path

from codex_runner import CodexRunError

from _lib.search import run_lit_search


_ACCEPTED_EXTS = (".json", ".md")


def normalize(paths: list[Path]) -> dict[str, Path]:
    files = [p for p in paths if p.suffix.lower() in _ACCEPTED_EXTS]
    if not files:
        raise CodexRunError("lit_search: query.json 또는 requirements.md 필요")
    # composite 이라 normalize 결과는 쓰이지 않음.
    return {f.name: f for f in files}


expected_outputs = ["candidates.json"]

run = run_lit_search
