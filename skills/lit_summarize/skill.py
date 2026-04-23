"""lit_summarize composite skill."""

from pathlib import Path

from codex_runner import CodexRunError

from _lib.summarize import run_lit_summarize


def normalize(paths: list[Path]) -> dict[str, Path]:
    files = [p for p in paths if p.suffix.lower() in (".md", ".json")]
    if not files:
        raise CodexRunError(
            "lit_summarize: .md (full text) 또는 .json (candidate metadata) 필요"
        )
    return {f.name: f for f in files}


expected_outputs = ["summary.json", "summary.md"]

run = run_lit_summarize
