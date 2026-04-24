"""render_beamer composite skill.

outline.with-figures.json → Beamer .tex + .pdf.

처리는 전부 `_lib/render.py::run_render_beamer` 가 담당.
"""

from pathlib import Path

from codex_runner import CodexRunError

from _lib.render import run_render_beamer


_ACCEPTED_EXTS = (".json",)


def normalize(paths: list[Path]) -> dict[str, Path]:
    files = [p for p in paths if p.suffix.lower() in _ACCEPTED_EXTS]
    if not files:
        raise CodexRunError(
            "render_beamer: outline.with-figures.json 파일 필요"
        )
    return {f"outline_{i}.json": p for i, p in enumerate(files)}


expected_outputs = ["doc.tex", "doc.pdf"]
run = run_render_beamer
