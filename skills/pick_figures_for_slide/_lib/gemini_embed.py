"""Gemini 클라이언트 (embedding + Flash 캡션/매칭).

- `gemini-embedding-2`: multimodal interleaved (image + caption) → 단일 fused vector
- `gemini-flash-latest`: 캡셔닝 (이미지 → 텍스트), 최종 매칭 (후보 리스트 → 선택)
"""

from __future__ import annotations

import copy
import logging
import os
import threading
import time
from collections import deque
from typing import Callable

from google import genai
from google.genai import types
from pydantic import BaseModel

from codex_runner import CodexRunError

logger = logging.getLogger("lecture_pipeline.pick_figures_for_slide.gemini")

LogCb = Callable[[str], None]


def _emit(log, msg):
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


_client: genai.Client | None = None
_lock = threading.Lock()


def _get_client() -> genai.Client:
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            key = (
                os.environ.get("GEMINI_API_KEY_ON_DEMAND")
                or os.environ.get("GEMINI_API_KEY")
            )
            if not key:
                raise CodexRunError(
                    "GEMINI_API_KEY_ON_DEMAND / GEMINI_API_KEY 미설정."
                )
            try:
                _client = genai.Client(
                    api_key=key,
                    http_options=types.HttpOptions(timeout=120_000),
                )
            except Exception:
                _client = genai.Client(api_key=key)
    return _client


class _RateLimiter:
    def __init__(self, rpm: int = 0):
        self.rpm = rpm
        self._ts: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.rpm <= 0:
            return
        with self._lock:
            now = time.monotonic()
            win = now - 60.0
            while self._ts and self._ts[0] < win:
                self._ts.popleft()
            if len(self._ts) >= self.rpm:
                sleep = 60.0 - (now - self._ts[0]) + 0.05
                if sleep > 0:
                    time.sleep(sleep)
                now = time.monotonic()
                win = now - 60.0
                while self._ts and self._ts[0] < win:
                    self._ts.popleft()
            self._ts.append(now)


_limiter = _RateLimiter(rpm=0)


def set_rpm(rpm: int) -> None:
    _limiter.rpm = max(0, int(rpm))


# ── Gemini schema sanitizer (md_to_handout 과 같은 패턴) ──
_GEMINI_UNSUPPORTED_KEYS = {
    "additionalProperties", "$schema", "$id", "$ref", "$defs",
    "definitions", "oneOf", "allOf", "not",
    "patternProperties", "propertyNames", "maxItems", "minItems",
}


def _inline_refs(schema: dict) -> dict:
    defs = schema.get("$defs") or schema.get("definitions") or {}

    def _resolve(obj):
        if isinstance(obj, dict):
            ref = obj.get("$ref")
            if isinstance(ref, str):
                for prefix in ("#/$defs/", "#/definitions/"):
                    if ref.startswith(prefix):
                        name = ref[len(prefix):]
                        target = defs.get(name)
                        if target is not None:
                            return _resolve(copy.deepcopy(target))
                        break
            return {k: _resolve(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_resolve(x) for x in obj]
        return obj

    return _resolve(schema)


def _sanitize_schema(s):
    if isinstance(s, dict):
        return {
            k: _sanitize_schema(v)
            for k, v in s.items()
            if k not in _GEMINI_UNSUPPORTED_KEYS
        }
    if isinstance(s, list):
        return [_sanitize_schema(x) for x in s]
    return s


def pydantic_to_gemini_schema(model: type[BaseModel]) -> dict:
    raw = model.model_json_schema()
    return _sanitize_schema(_inline_refs(raw))


# ── Embedding API ──


def embed_text(
    *,
    text: str,
    task_type: str = "RETRIEVAL_QUERY",
    model: str = "gemini-embedding-2",
    output_dim: int = 1536,
) -> list[float]:
    """Pure text → vector."""
    client = _get_client()
    _limiter.wait()
    resp = client.models.embed_content(
        model=model,
        contents=text,
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=output_dim,
        ),
    )
    return list(resp.embeddings[0].values)


def embed_multimodal(
    *,
    image_bytes: bytes,
    mime_type: str,
    caption: str,
    task_type: str = "RETRIEVAL_DOCUMENT",
    model: str = "gemini-embedding-2",
    output_dim: int = 1536,
) -> list[float]:
    """Interleaved [image, caption] → 단일 fused vector.

    gemini-embedding-2 의 핵심 기능 — 별도 벡터 mean-pool 없이 native fusion.
    """
    client = _get_client()
    _limiter.wait()
    resp = client.models.embed_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            caption,
        ],
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=output_dim,
        ),
    )
    return list(resp.embeddings[0].values)


# ── Flash caption (vision → 텍스트) ──


_CAPTION_SYSTEM = (
    "You are a figure captioning assistant for an academic ML textbook. "
    "Given an image (figure/plot/diagram), write a concise English caption "
    "(≤30 words) describing: what is plotted, axes/labels if visible, and the "
    "conceptual role (e.g., 'K-means clustering result on 2D data', "
    "'dendrogram of hierarchical clustering'). No preamble. Caption only."
)


def caption_image(
    *,
    image_bytes: bytes,
    mime_type: str,
    context_hint: str = "",
    model: str = "gemini-flash-latest",
    timeout: int = 60,
) -> str:
    """Gemini Flash vision 으로 figure 캡션 생성."""
    client = _get_client()
    _limiter.wait()

    content_parts = [
        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
    ]
    if context_hint:
        content_parts.append(
            types.Part.from_text(text=f"Context: {context_hint[:300]}")
        )
    content_parts.append(
        types.Part.from_text(text="Produce the caption now.")
    )

    cfg_kwargs = dict(
        system_instruction=_CAPTION_SYSTEM,
        max_output_tokens=200,
    )
    try:
        cfg = types.GenerateContentConfig(
            **cfg_kwargs,
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        )
    except Exception:
        cfg = types.GenerateContentConfig(**cfg_kwargs)

    resp = client.models.generate_content(
        model=model,
        contents=content_parts,
        config=cfg,
    )
    return (resp.text or "").strip()


# ── Flash final match (Tier 1 최종 선택) ──


_MATCH_SYSTEM = """너는 학술 강의 핸드아웃의 figure 선택자다. 주어진 슬라이드의
figure_hint 와 후보 assets 리스트 (파일명 + 캡션) 를 보고, 가장 적합한 파일을
최대 max_refs 개 골라 JSON 으로 답하라.

- matches: 선택한 파일명 리스트 (0개 가능). 반드시 후보 목록에 있는 파일명만.
- confidence: 0~1. 가장 적합한 후보와의 fit 정도.
  - 0.85+: hint 와 강하게 일치하는 캡션이 후보에 있음
  - 0.65~0.85: 적당히 일치, 쓸만함
  - 0.65 미만: 적합한 것 없음 (matches 비워도 됨)
- reasoning: 왜 골랐는지 / 왜 아무것도 못 골랐는지 한 줄."""


def match_final(
    *,
    response_model: type[BaseModel],
    slide_title: str,
    slide_bullets: list[str],
    figure_hint: str,
    candidates: list[dict],  # [{filename, caption, score}]
    max_refs: int,
    model: str = "gemini-flash-latest",
) -> BaseModel:
    client = _get_client()
    _limiter.wait()

    bullets_block = "\n".join(f"  - {b}" for b in slide_bullets[:6])
    cands_block = "\n".join(
        f"  [{c['score']:.2f}] {c['filename']}: {c['caption'][:120]}"
        for c in candidates
    )
    prompt = f"""Slide title: {slide_title}
Bullets:
{bullets_block}

Figure hint: {figure_hint}

Candidates (top-{len(candidates)} by cosine similarity):
{cands_block}

max_refs: {max_refs}
"""

    schema = pydantic_to_gemini_schema(response_model)
    cfg_kwargs = dict(
        system_instruction=_MATCH_SYSTEM,
        response_mime_type="application/json",
        response_schema=schema,
        max_output_tokens=2000,
    )
    try:
        cfg = types.GenerateContentConfig(
            **cfg_kwargs,
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        )
    except Exception:
        cfg = types.GenerateContentConfig(**cfg_kwargs)

    resp = client.models.generate_content(
        model=model,
        contents=[types.Part.from_text(text=prompt)],
        config=cfg,
    )
    text = (resp.text or "").strip()
    if not text:
        raise CodexRunError("match_final: 응답 비었음")
    return response_model.model_validate_json(text)
