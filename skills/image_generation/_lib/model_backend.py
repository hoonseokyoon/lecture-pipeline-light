"""Model backend — Gemini 3.1 Flash Image 로 PNG 직접 생성."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError

from _lib.gemini_image import generate_image
from _lib.schema import FigureSpec

LogCb = Callable[[str], None]


def _emit(log, msg):
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


def _compose_image_prompt(spec: FigureSpec) -> str:
    """hint + context + style 을 image-gen prompt 로 조합."""
    lines = [spec.hint]
    if spec.context:
        lines.append(f"context: {spec.context}")
    style = spec.style or "academic figure, clean composition, sans-serif, no watermark"
    lines.append(f"style: {style}")
    lines.append("no text labels unless explicitly part of a diagram")
    return "\n".join(lines)


def run_model_backend(
    spec: FigureSpec,
    cfg: dict,
    log_cb: LogCb | None = None,
) -> tuple[dict[str, bytes], dict]:
    """Gemini 3.1 image 로 PNG 생성.

    반환: (outputs_dict, meta_partial)
    - outputs_dict: {"fig.png": bytes} (format=="tex" 여도 model mode 는 PNG 만 가능 —
      TikZ 를 image 모델이 생성할 수 없음. 호출자가 라우팅에서 막거나 fallback 처리).
    - meta_partial: {backend, model, prompt_snapshot, retries, duration_s}
    """
    if spec.format != "png":
        raise CodexRunError(
            f"model backend 는 format='png' 만 지원 (요청: {spec.format}). "
            f"TikZ/SVG 는 script mode 사용."
        )

    model = cfg.get("gemini_image_model", "gemini-3.1-flash-image-preview")
    timeout = int(cfg.get("model_timeout", 120))

    prompt = _compose_image_prompt(spec)
    t0 = time.monotonic()
    png_bytes = generate_image(
        prompt=prompt,
        model=model,
        timeout=timeout,
        log_cb=log_cb,
    )
    dt = time.monotonic() - t0

    outputs = {"fig.png": png_bytes}
    meta = {
        "backend": "gemini_image",
        "model": model,
        "prompt_snapshot": prompt,
        "retries": 0,
        "duration_s": round(dt, 2),
    }
    _emit(log_cb, f"[model] OK — {len(png_bytes)} bytes")
    return outputs, meta
