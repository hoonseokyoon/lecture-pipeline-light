"""image_generation composite skill.

입력: `.json` FigureSpec 파일 (hint / context / style / format / mode / seed).
산출: fig.png 또는 fig.source.tex + fig.source.py (script mode) + meta.json.

Auto mode: hint 를 Gemini Flash 로 분류 → script (matplotlib/TikZ via Codex)
또는 model (Gemini 3.1 image) 경로 선택.
"""

from pathlib import Path

from codex_runner import CodexRunError

from _lib.generate import run_image_generation


_ACCEPTED_EXTS = (".json",)


def normalize(paths: list[Path]) -> dict[str, Path]:
    files = [p for p in paths if p.suffix.lower() in _ACCEPTED_EXTS]
    if not files:
        raise CodexRunError("image_generation: .json spec 파일 필요")
    return {f"spec_{i}.json": p for i, p in enumerate(files)}


expected_outputs = ["fig.png"]  # meta.json, source 파일은 extras 로 함께 저장됨
run = run_image_generation
