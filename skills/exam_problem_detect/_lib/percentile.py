"""Cluster member boxes로부터 loose/medium/tight 후보 3개 산출.

설계:
- loose: 박스 바깥 방향으로 여유 (상하좌우 각 25%/75%) → 큰 박스
- medium: 50% percentile (median) → 중간
- tight: 박스 안쪽 방향으로 조임 (상하좌우 각 75%/25%) → 작은 박스

percentile 기반이라 outlier에 robust. 입력이 1~2개면 degenerate하지만 그대로 반환
(clustering 단계가 많은 detection을 모아줄 때 의미 있음).
"""

from __future__ import annotations


Box = tuple[float, float, float, float]


def _q(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = int(len(sorted_values) * p)
    idx = min(idx, len(sorted_values) - 1)
    return sorted_values[idx]


def compute_candidates(boxes: list[Box]) -> dict[str, Box]:
    """4 edge 각각의 percentile 기반 3 candidates 반환.

    Returns:
        {"loose": box, "medium": box, "tight": box}
    """
    if not boxes:
        raise ValueError("compute_candidates: boxes가 비어있음")

    x1s = sorted(b[0] for b in boxes)
    y1s = sorted(b[1] for b in boxes)
    x2s = sorted(b[2] for b in boxes)
    y2s = sorted(b[3] for b in boxes)

    loose: Box = (
        _q(x1s, 0.25),
        _q(y1s, 0.25),
        _q(x2s, 0.75),
        _q(y2s, 0.75),
    )
    medium: Box = (
        _q(x1s, 0.50),
        _q(y1s, 0.50),
        _q(x2s, 0.50),
        _q(y2s, 0.50),
    )
    tight: Box = (
        _q(x1s, 0.75),
        _q(y1s, 0.75),
        _q(x2s, 0.25),
        _q(y2s, 0.25),
    )

    # tight 박스가 degenerate(x1>=x2 또는 y1>=y2)일 수 있음 → medium으로 fallback
    if tight[0] >= tight[2] or tight[1] >= tight[3]:
        tight = medium

    return {"loose": loose, "medium": medium, "tight": tight}
