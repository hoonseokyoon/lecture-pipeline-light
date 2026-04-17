"""zero_shot_rec composite skill.

LLM-only 반복 refinement로 자연어 지시어에 해당하는 객체에 bounding box.
run_rec가 Codex/Claude 경로를 stage별로 run_codex_task로 호출한다.
"""

from pathlib import Path

from codex_runner import CodexRunError

from _lib.pipeline import run_rec


def normalize(paths: list[Path]) -> dict[str, Path]:
    """이미지 1개만 workdir으로 매핑. conditioning 파일은 run()이 직접 읽음."""
    images = [
        p for p in paths
        if p.suffix.lower() in (".png", ".jpg", ".jpeg")
    ]
    if len(images) != 1:
        raise CodexRunError(
            f"zero_shot_rec: PNG/JPG 이미지 1개 필요 (found {len(images)})"
        )
    p = images[0]
    return {f"image{p.suffix.lower()}": p}


expected_outputs = ["result.json", "annotated.png"]

# composite skill — codex_runner.run_skill이 이쪽으로 위임 (codex_runner.py:1011-1017)
run = run_rec
