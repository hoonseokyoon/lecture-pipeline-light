"""exam_problem_detect composite skill.

PDF 또는 PNG/JPG 이미지 입력에서 문제 단위 bounding box 탐지.
zero_shot_rec의 call_llm/gemini_backend/image_utils 재사용 (복사본).
"""

from pathlib import Path

from codex_runner import CodexRunError

from _lib.pipeline import run_exam_detect


_ACCEPTED_EXTS = (".pdf", ".png", ".jpg", ".jpeg")


def normalize(paths: list[Path]) -> dict[str, Path]:
    """PDF 또는 이미지 파일(들)을 workdir에 매핑. composite skill이라 run()이
    input_paths를 직접 쓰므로 이 dict는 실제로 사용되지 않지만, 유효성 검증은
    여기서 수행."""
    files = [p for p in paths if p.suffix.lower() in _ACCEPTED_EXTS]
    if not files:
        raise CodexRunError(
            "exam_problem_detect: PDF/PNG/JPG 파일 필요"
        )
    # normalize 반환값은 composite skill에서 쓰이지 않지만 형식상 필요
    return {f"input_{i}{p.suffix.lower()}": p for i, p in enumerate(files)}


expected_outputs = ["problems.json", "clusters.json", "bridge_boxes.json"]

# composite — codex_runner.run_skill이 skill.run으로 위임 (codex_runner.py:1011-1017)
run = run_exam_detect
