"""Multi-box 샘플링 중단 조건 (pluggable).

현재 기본: 최근 N round 연속으로 n_clusters 불변이면 stop. 또는 max_rounds 도달.
장래에는 확률론적 estimator(Chao1 등)로 교체 가능한 인터페이스.
"""

from __future__ import annotations

from typing import Callable


# history item 형태: {"call_idx": int, "n_clusters": int, "n_boxes_added": int, ...}
HistoryItem = dict
StopFn = Callable[[list[HistoryItem], dict], tuple[bool, str]]


def default_stopping(
    history: list[HistoryItem],
    cfg: dict,
) -> tuple[bool, str]:
    """cluster 수가 연속 round 동안 증가하지 않으면 stop.

    Args:
        history: round별 상태 기록.
        cfg: 'max_rounds', 'min_rounds', 'stable_rounds' 키 사용.

    Returns:
        (stop?, reason)
    """
    max_rounds = int(cfg.get("max_rounds", 12))
    min_rounds = int(cfg.get("min_rounds", 3))
    stable_threshold = int(cfg.get("stable_rounds", 2))

    if len(history) >= max_rounds:
        return True, f"max_rounds={max_rounds} 도달"
    if len(history) < min_rounds:
        return False, f"min_rounds={min_rounds} 미도달"
    # 마지막 (stable_threshold + 1)개 round의 n_clusters가 모두 같은지 확인
    window = history[-(stable_threshold + 1):]
    if len(window) < stable_threshold + 1:
        return False, "stable window 미확보"
    counts = [h["n_clusters"] for h in window]
    if len(set(counts)) == 1:
        return True, f"n_clusters={counts[-1]}로 {stable_threshold} round 안정화"
    return False, f"n_clusters 변동 중: {counts}"
