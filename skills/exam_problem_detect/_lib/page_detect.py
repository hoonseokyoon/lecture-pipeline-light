"""페이지당 multi-box LLM 호출 + 누적 샘플링 + incremental clustering.

Flow:
1. 초기 N회 (min_rounds) multi-box 호출 → 박스 수집 → clustering
2. stopping.default_stopping가 stop 반환할 때까지 추가 호출
3. (clusters, bridge_boxes, history) 반환
"""

from __future__ import annotations

import json
from typing import Any, Callable

from PIL import Image

from codex_runner import CodexRunError

from _lib import image_utils as iu
from _lib.clustering import (
    BridgeBox,
    Cluster,
    incremental_add,
    reset_cluster_id_counter,
)
from _lib.llm import call_llm
from _lib.prompts import build_multibox_prompt
from _lib.schemas import PAGE_MULTIBOX_SCHEMA
from _lib.stopping import default_stopping


LogCb = Callable[[str], None]
Box = tuple[float, float, float, float]


def _multibox_call(
    img: Image.Image,
    cfg: dict,
) -> tuple[bool, list[Box]]:
    """단일 multi-box LLM 호출. (has_problems, boxes) 반환.

    boxes는 clip + 유효성 필터 통과한 것만.
    """
    prompt = build_multibox_prompt()
    png = iu.save_png_bytes(img)
    outputs = call_llm(
        cfg=cfg,
        prompt=prompt,
        inputs={"image.png": png},
        output_schema=PAGE_MULTIBOX_SCHEMA,
        reasoning_effort=cfg.get("stage0_reasoning_effort", "medium"),
    )
    data = json.loads(outputs["result.json"].decode("utf-8"))
    has_problems = bool(data.get("has_problems", False))
    raw_problems = data.get("problems") or []

    boxes: list[Box] = []
    for p in raw_problems:
        try:
            b = iu.clip_box_norm((
                float(p["x1"]), float(p["y1"]),
                float(p["x2"]), float(p["y2"]),
            ))
        except (KeyError, ValueError, TypeError):
            continue
        # 0-영역 박스 제거
        if b[2] - b[0] < 1.0 or b[3] - b[1] < 1.0:
            continue
        boxes.append(b)
    return has_problems, boxes


def detect_page_sampling(
    img: Image.Image,
    cfg: dict,
    log: LogCb,
    page_num: int,
) -> tuple[list[Cluster], list[BridgeBox], list[dict]]:
    """페이지 하나에 대해 multi-box 반복 호출 → 누적 clustering.

    Returns:
        (clusters, bridge_boxes, history)
    """
    reset_cluster_id_counter(page_num * 10000 + 1)  # 페이지별 id 공간 분리

    iou_threshold = float(cfg.get("iou_threshold", 0.5))
    min_rounds = int(cfg.get("min_rounds", 3))

    clusters: list[Cluster] = []
    bridge_boxes: list[BridgeBox] = []
    history: list[dict] = []
    has_problems_ever = False

    round_idx = 0
    while True:
        try:
            has_p, boxes = _multibox_call(img, cfg)
        except (CodexRunError, json.JSONDecodeError, KeyError) as exc:
            log(f"[page {page_num}] round {round_idx + 1} 실패, 건너뜀: {exc}")
            boxes = []
            has_p = False

        if has_p:
            has_problems_ever = True

        # 박스 누적
        n_new = n_merged = n_bridge = 0
        for b in boxes:
            tag = incremental_add(
                b, clusters, bridge_boxes,
                iou_threshold=iou_threshold, call_idx=round_idx,
            )
            if tag == "new":
                n_new += 1
            elif tag == "merged":
                n_merged += 1
            else:
                n_bridge += 1

        history.append({
            "call_idx": round_idx,
            "n_clusters": len(clusters),
            "n_boxes_this_round": len(boxes),
            "n_new": n_new,
            "n_merged": n_merged,
            "n_bridge": n_bridge,
        })
        log(
            f"[page {page_num}] round {round_idx + 1}: "
            f"boxes={len(boxes)} (new={n_new}, merged={n_merged}, "
            f"bridge={n_bridge}) → clusters={len(clusters)}"
        )

        # 첫 round에서 "문제 없음"이 나오고 min_rounds 이상 돌고도 cluster 0이면 조기 종료
        if round_idx + 1 >= min_rounds and not clusters:
            if not has_problems_ever:
                log(f"[page {page_num}] 문제 없음 판정 → 조기 종료")
                break

        # stopping 체크
        stop, reason = default_stopping(history, cfg)
        if stop:
            log(f"[page {page_num}] 종료: {reason}")
            break

        round_idx += 1

    return clusters, bridge_boxes, history
