"""Stage 2: 단일 edge refinement 워커.

병렬 4-edge 실행을 위한 stateless 함수. LLM이 ROI 내 점 라벨을 반환하면
원본 정규화 좌표로 역변환해 edge 값을 갱신.
"""

from __future__ import annotations

import json
from typing import Any

from PIL import Image

from codex_runner import CodexRunError

from _lib import image_utils as iu
from _lib.llm import call_llm
from _lib.prompts import build_stage2_edge_prompt
from _lib.schemas import build_stage2_edge_schema


CONFIDENCE_THRESHOLD = 0.3


def _edge_current_value(box_norm: tuple[float, float, float, float],
                        edge: str) -> float:
    x1, y1, x2, y2 = box_norm
    if edge == "top":
        return y1
    if edge == "bottom":
        return y2
    if edge == "left":
        return x1
    if edge == "right":
        return x2
    raise ValueError(f"invalid edge: {edge}")


def _dot_axis_value(dot_norm: tuple[float, float],
                    edge: str) -> float:
    x, y = dot_norm
    if edge in ("top", "bottom"):
        return y
    return x


def _clamp_new_edge(new_value: float,
                    box_norm: tuple[float, float, float, float],
                    edge: str,
                    max_move_frac: float = 0.5
                    ) -> float:
    """현재 edge 값 대비 이동량을 변 길이의 max_move_frac로 제한.

    발산 방지용. 변 길이는 수평 edge면 박스 높이, 수직 edge면 박스 폭.
    """
    x1, y1, x2, y2 = box_norm
    current = _edge_current_value(box_norm, edge)
    size = (y2 - y1) if edge in ("top", "bottom") else (x2 - x1)
    max_move = size * max_move_frac
    delta = new_value - current
    if delta > max_move:
        delta = max_move
    elif delta < -max_move:
        delta = -max_move
    return current + delta


def refine_one_edge(
    img: Image.Image,
    box_norm: tuple[float, float, float, float],
    edge: str,
    condition: str,
    *,
    cfg: dict,
    reasoning_effort: str,
    outward: float,
    inward: float,
    lateral: float,
    min_roi_px: int,
    rows: int,
    cols: int,
    log_callback=None,
) -> tuple[float, dict[str, Any]]:
    """단일 edge refinement. 새 edge 정규화 값과 raw LLM 응답 반환.

    실패 또는 confidence<threshold 또는 direction=same이면 현재 edge 값 유지.
    outward/inward/lateral은 ROI 크롭 비대칭 margin — Stage 1 verdict로 결정.
    """
    log = log_callback or (lambda m: None)

    # 1) ROI 크롭 (scale: 업스케일 배율, 1.0=원본 유지)
    roi, crop_box_norm, scale = iu.crop_edge_roi(
        img, box_norm, edge,
        outward=outward, inward=inward, lateral=lateral,
        min_roi_px=min_roi_px,
    )
    roi = roi.copy()  # 원본 훼손 방지

    # 2) 점 매트릭스 (원본 정규화 좌표 기준)
    dots_norm = iu.dot_matrix_norm(crop_box_norm, rows, cols, margin_frac=0.06)
    dots_px = [
        (*iu.orig_norm_to_roi_pixel((x, y), crop_box_norm, roi.size), label)
        for (x, y, label) in dots_norm
    ]

    # 3) 현재 edge 선을 ROI 위에 그리기
    current_box_px_in_roi = iu.box_norm_to_roi_pixel(
        box_norm, crop_box_norm, roi.size,
    )
    if edge == "top":
        edge_px_value = current_box_px_in_roi[1]
    elif edge == "bottom":
        edge_px_value = current_box_px_in_roi[3]
    elif edge == "left":
        edge_px_value = current_box_px_in_roi[0]
    else:  # right
        edge_px_value = current_box_px_in_roi[2]

    # Scale-aware 드로잉 파라미터.
    # - ROI 짧은 변 대비 비례해서 선 두께/점 반지름/폰트 조정
    # - 업스케일 시 반투명(약 78%)으로 하여 가림 최소화
    roi_short = min(roi.size)
    line_width = max(2, round(roi_short / 200))
    dot_radius = max(5, round(roi_short / 80))
    font_size = max(10, dot_radius + 3)
    upscaled = scale > 1.2
    line_alpha = 200 if upscaled else 255
    dot_alpha = 220 if upscaled else 255

    iu.draw_edge_line(
        roi, edge, edge_px_value,
        color="red", width=line_width, alpha=line_alpha,
    )
    iu.draw_dots(
        roi, dots_px,
        radius=dot_radius, font_size=font_size, alpha=dot_alpha,
    )

    # 4) 전체 이미지(컨텍스트)에도 현재 박스를 그려 첨부
    #    edge refinement가 ROI만 보면 주변 문맥을 잃기 때문.
    full_ctx = img.copy()
    fw, fh = full_ctx.size
    full_box_px = iu.norm_to_pixel(box_norm, fw, fh)
    full_line_width = max(2, round(min(fw, fh) / 400))
    iu.draw_box(full_ctx, full_box_px, color="red", width=full_line_width)

    # 5) LLM 호출 — ROI + 전체 샷 둘 다 첨부
    dot_labels = [lbl for (_, _, lbl) in dots_norm]
    prompt = build_stage2_edge_prompt(condition, edge, dot_labels)
    schema = build_stage2_edge_schema(dot_labels)
    roi_bytes = iu.save_png_bytes(roi)
    full_bytes = iu.save_png_bytes(full_ctx)

    try:
        outputs = call_llm(
            cfg=cfg,
            prompt=prompt,
            inputs={"roi.png": roi_bytes, "full.png": full_bytes},
            output_schema=schema,
            reasoning_effort=reasoning_effort,
        )
        raw_bytes = outputs["result.json"]
        resp = json.loads(raw_bytes.decode("utf-8"))
    except (CodexRunError, json.JSONDecodeError, KeyError) as exc:
        log(f"[stage2 {edge}] 실패 — edge 유지: {exc}")
        return _edge_current_value(box_norm, edge), {"error": str(exc)}

    # 5) 응답 해석
    confidence = float(resp.get("confidence", 0.0))
    direction = resp.get("direction", "same")
    nearest_label = resp.get("nearest_point", "")

    if confidence < CONFIDENCE_THRESHOLD:
        log(
            f"[stage2 {edge}] confidence={confidence:.2f} < "
            f"{CONFIDENCE_THRESHOLD} — edge 유지"
        )
        return _edge_current_value(box_norm, edge), resp

    if direction == "same":
        log(f"[stage2 {edge}] direction=same — edge 유지")
        return _edge_current_value(box_norm, edge), resp

    # nearest_point 라벨 → dot의 원본 정규화 좌표 → axis 값
    label_map = {lbl: (x, y) for (x, y, lbl) in dots_norm}
    if nearest_label not in label_map:
        log(
            f"[stage2 {edge}] unknown nearest_point={nearest_label} — edge 유지"
        )
        return _edge_current_value(box_norm, edge), resp

    new_value = _dot_axis_value(label_map[nearest_label], edge)
    new_value = _clamp_new_edge(new_value, box_norm, edge, max_move_frac=0.5)

    log(
        f"[stage2 {edge}] {direction}, region={resp.get('region')}, "
        f"conf={confidence:.2f}, {nearest_label} → {new_value:.1f}"
    )
    return new_value, resp
