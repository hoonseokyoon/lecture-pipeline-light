"""lit_fetch composite skill."""

from pathlib import Path

from codex_runner import CodexRunError

from _lib.fetch import run_lit_fetch


def normalize(paths: list[Path]) -> dict[str, Path]:
    files = [p for p in paths if p.suffix.lower() == ".json"]
    if not files:
        raise CodexRunError("lit_fetch: candidates.json 필요")
    return {f.name: f for f in files}


expected_outputs = ["download_report.json"]

run = run_lit_fetch
