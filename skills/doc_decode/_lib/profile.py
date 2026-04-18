"""문서 프로파일 — 샘플 페이지 몇 장을 Gemini 에 보여 자연어 메모 생성.

결과는 `profile.md` (plain text) 로 저장해 agent 프롬프트에 그대로 주입.
분기 없이 참고용.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from PIL import Image

from codex_runner import CodexRunError, current_cancel_event

from _lib import image_utils as iu
from _lib.gemini_backend import run_gemini_task, set_rpm
from _lib.prompts import build_profile_prompt
from _lib.schemas import PROFILE_SCHEMA


LogCb = Callable[[str], None]


def pick_sample_indices(page_count: int, n: int = 3) -> list[int]:
    """첫 / 중간 / 마지막 3장. page_count 에 맞춰 조정."""
    if page_count <= 0:
        return []
    if page_count <= n:
        return list(range(page_count))
    if n == 1:
        return [0]
    if n == 2:
        return [0, page_count - 1]
    # 기본: 첫·중·끝
    mid = page_count // 2
    return [0, mid, page_count - 1]


def generate_profile(
    pages: list[Image.Image],
    cfg: dict,
    log: LogCb,
) -> str:
    """샘플 페이지 이미지들로 Gemini 에 질의 → 자유 텍스트 반환.

    실패 시 빈 문자열 반환 (agent 프롬프트에서 "(프로파일 없음)" 처리).
    """
    n_sample = int(cfg.get("profile_sample_pages", 3))
    samples = pick_sample_indices(len(pages), n_sample)
    if not samples:
        return ""

    ev = current_cancel_event.get()
    if ev is not None and ev.is_set():
        raise CodexRunError("doc_decode: 사용자 취소 (profile)")

    log(f"[profile] {len(samples)}개 샘플 페이지로 Gemini 호출: {samples}")

    set_rpm(int(cfg.get("gemini_rpm", 0)))
    inputs: dict[str, bytes] = {}
    for i, idx in enumerate(samples):
        inputs[f"sample_{i:02d}.png"] = iu.save_png_bytes(pages[idx])

    prompt = build_profile_prompt(len(samples))
    try:
        out = run_gemini_task(
            prompt=prompt,
            inputs=inputs,
            output_schema=PROFILE_SCHEMA,
            model=cfg.get("gemini_model", "gemini-2.5-pro"),
        )
    except Exception as exc:
        if ev is not None and ev.is_set():
            raise CodexRunError("doc_decode: 사용자 취소 (profile)") from exc
        log(f"[profile] Gemini 실패 — 빈 프로파일로 진행: {exc}")
        return ""

    try:
        data = json.loads(out["result.json"].decode("utf-8"))
        text = (data.get("summary_ko") or "").strip()
    except Exception as exc:
        log(f"[profile] 파싱 실패: {exc}")
        return ""

    log(f"[profile] {len(text)} 자 수신")
    return text
