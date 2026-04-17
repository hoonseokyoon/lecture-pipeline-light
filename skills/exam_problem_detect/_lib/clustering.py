"""Greedy single-linkage IoU 기반 incremental clustering.

Cluster membership은 sticky (재배정 없음). 새 박스 추가만으로 전체 구조 업데이트.
단일 박스가 2+ cluster에 IoU 임계 이상 매칭 시 bridge bucket로 분리 보관.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

from _lib.image_utils import iou as iou_fn


Box = tuple[float, float, float, float]


@dataclass
class Cluster:
    id: int
    members: list[Box] = field(default_factory=list)
    call_indices: list[int] = field(default_factory=list)


@dataclass
class BridgeBox:
    box: Box
    linked_cluster_ids: list[int]
    call_idx: int


_id_counter = count(1)


def new_cluster_id() -> int:
    return next(_id_counter)


def reset_cluster_id_counter(start: int = 1) -> None:
    """테스트용 또는 페이지별 처리 시작 시 id 초기화."""
    global _id_counter
    _id_counter = count(start)


def incremental_add(
    box: Box,
    clusters: list[Cluster],
    bridge_boxes: list[BridgeBox],
    iou_threshold: float = 0.5,
    call_idx: int = 0,
) -> str:
    """새 박스 하나를 클러스터 구조에 누적.

    Returns:
        'new' | 'merged' | 'bridge' — 처리 결과 태그.
    """
    matched: list[Cluster] = []
    for c in clusters:
        if not c.members:
            continue
        max_iou = max(iou_fn(box, m) for m in c.members)
        if max_iou >= iou_threshold:
            matched.append(c)

    if len(matched) == 0:
        new_c = Cluster(id=new_cluster_id(), members=[box], call_indices=[call_idx])
        clusters.append(new_c)
        return "new"
    if len(matched) == 1:
        matched[0].members.append(box)
        matched[0].call_indices.append(call_idx)
        return "merged"
    # len(matched) >= 2 → bridge
    bridge_boxes.append(
        BridgeBox(
            box=box,
            linked_cluster_ids=[c.id for c in matched],
            call_idx=call_idx,
        )
    )
    return "bridge"
