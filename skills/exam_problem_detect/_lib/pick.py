"""Cluster별 3-후보(loose/medium/tight) 중 LLM이 대표 선출.

zero_shot_rec Stage 0b 패턴 그대로:
- 후보 박스를 번호 매긴 컬러 박스로 이미지에 overlay
- LLM에 "best_id는?" 질의 (PICK_SCHEMA)
- 실패 시 medium fallback
"""

from __future__ import annotations

import json
from typing import Callable

from PIL import Image

from codex_runner import CodexRunError

from _lib import image_utils as iu
from _lib.llm import call_llm
from _lib.prompts import build_pick_prompt
from _lib.schemas import PICK_SCHEMA


LogCb = Callable[[str], None]
Box = tuple[float, float, float, float]


def pick_representative(
    img: Image.Image,
    candidates: dict[str, Box],
    cfg: dict,
    log: LogCb,
) -> tuple[Box, str]:
    """3-후보 중 LLM이 best 선택. 실패 시 medium fallback.

    Args:
        candidates: {"loose": box, "medium": box, "tight": box}

    Returns:
        (chosen_box, variant_name) — variant_name ∈ {"loose", "medium", "tight"}.
    """
    # 번호와 variant 매핑: 1=loose, 2=medium, 3=tight
    variants = ["loose", "medium", "tight"]
    boxes = [candidates[v] for v in variants]
    ids = [1, 2, 3]

    annotated = iu.annotate_stage0_candidates(img, boxes, ids)
    png = iu.save_png_bytes(annotated)

    try:
        outputs = call_llm(
            cfg=cfg,
            prompt=build_pick_prompt(),
            inputs={"image.png": png},
            output_schema=PICK_SCHEMA,
            reasoning_effort=cfg.get("stage0_reasoning_effort", "medium"),
        )
        data = json.loads(outputs["result.json"].decode("utf-8"))
        best_id = int(data["best_id"])
        if best_id not in (1, 2, 3):
            raise ValueError(f"best_id 범위 밖: {best_id}")
        variant = variants[best_id - 1]
        log(f"[pick] best={best_id} ({variant}) — {data.get('reason', '')[:60]}")
        return candidates[variant], variant
    except (CodexRunError, json.JSONDecodeError, KeyError, ValueError) as exc:
        log(f"[pick] 실패, medium fallback: {exc}")
        return candidates["medium"], "medium"
