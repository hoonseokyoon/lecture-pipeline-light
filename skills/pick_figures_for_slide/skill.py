"""pick_figures_for_slide composite skill.

outline.json (md_to_handout 산출물) 을 입력 받아 slide.figure_refs 를 채운다.
2-tier: (1) 기존 assets 에서 의미 매칭 (Gemini embedding + Flash 최종 선택),
(2) 신규 생성 (image_generation 서브스킬 subprocess 호출).
"""

from pathlib import Path

from codex_runner import CodexRunError

from _lib.pick import run_pick_figures


_ACCEPTED_EXTS = (".json",)


def normalize(paths: list[Path]) -> dict[str, Path]:
    files = [p for p in paths if p.suffix.lower() in _ACCEPTED_EXTS]
    if not files:
        raise CodexRunError("pick_figures_for_slide: outline.json 파일 필요")
    # outline 이름 추정 — 여럿이면 첫 번째
    return {f"outline_{i}.json": p for i, p in enumerate(files)}


expected_outputs = ["outline.with-figures.json"]
run = run_pick_figures
