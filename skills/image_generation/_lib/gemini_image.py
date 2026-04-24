"""Gemini 3.1 Flash Image (Nano Banana) + Gemini flash classifier.

두 용도의 client 는 같은 싱글톤 공유 (동일 API 키·동일 endpoint).
`GEMINI_API_KEY_ON_DEMAND` 우선. 스키마 sanitize 는 md_to_handout 과 같은 패턴.
"""

from __future__ import annotations

import base64
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

from _lib.schema import RouteDecision

logger = logging.getLogger("lecture_pipeline.image_generation.gemini")

LogCb = Callable[[str], None]


def _emit(log: LogCb | None, msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


# ── client singleton ──
_client: genai.Client | None = None
_client_lock = threading.Lock()


def _get_client() -> genai.Client:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            api_key = (
                os.environ.get("GEMINI_API_KEY_ON_DEMAND")
                or os.environ.get("GEMINI_API_KEY")
            )
            if not api_key:
                raise CodexRunError(
                    "GEMINI_API_KEY_ON_DEMAND / GEMINI_API_KEY 미설정. .env 확인."
                )
            try:
                _client = genai.Client(
                    api_key=api_key,
                    http_options=types.HttpOptions(timeout=180_000),
                )
            except Exception:
                _client = genai.Client(api_key=api_key)
    return _client


# ── rate limiter ──
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


# ── schema sanitize (Gemini Schema proto 호환) ──
_GEMINI_UNSUPPORTED_KEYS = {
    "additionalProperties", "$schema", "$id", "$ref", "$defs",
    "definitions", "oneOf", "allOf", "not",
    "patternProperties", "propertyNames",
    "maxItems", "minItems",  # nested array-of-objects 에서 Gemini 가 400
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


def _pydantic_to_gemini_schema(model: type[BaseModel]) -> dict:
    raw = model.model_json_schema()
    return _sanitize_schema(_inline_refs(raw))


# ── Image generation (model mode) ──


def generate_image(
    *,
    prompt: str,
    model: str = "gemini-3.1-flash-image-preview",
    timeout: int = 120,
    log_cb: LogCb | None = None,
) -> bytes:
    """Gemini image 모델로 PNG bytes 생성.

    Gemini 3.1 Flash Image (aka Nano Banana) 는 `response_modalities=["Image"]`
    를 지정하면 candidates[0].content.parts[].inline_data.data 로 이미지 반환.
    """
    client = _get_client()
    _limiter.wait()
    _emit(log_cb, f"[image_gen] Gemini {model} 호출")
    t0 = time.monotonic()

    cfg = types.GenerateContentConfig(
        response_modalities=["Image"],
    )

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[types.Part.from_text(text=prompt)],
                config=cfg,
            )
            break
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            if any(tok in msg for tok in ("429", "503", "UNAVAILABLE", "RESOURCE_EXHAUSTED")):
                wait_s = 2.0 * (attempt + 1)
                _emit(log_cb, f"[image_gen] 재시도 {attempt+1}/2 ({wait_s}s 대기) — {msg[:100]}")
                time.sleep(wait_s)
                continue
            raise CodexRunError(f"Gemini image gen 실패: {msg}") from exc
    else:
        raise CodexRunError(f"Gemini image gen 재시도 소진: {last_exc}") from last_exc

    dt = time.monotonic() - t0
    _emit(log_cb, f"[image_gen] 응답 수신 {dt:.1f}s")

    # 응답에서 이미지 바이트 추출
    for cand in (resp.candidates or []):
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is None:
                continue
            data = getattr(inline, "data", None)
            if not data:
                continue
            if isinstance(data, str):
                data = base64.b64decode(data)
            return bytes(data)

    text = getattr(resp, "text", "") or ""
    raise CodexRunError(
        f"Gemini image gen 응답에 inline_data 없음. text='{text[:200]}'"
    )


# ── Router classifier (Gemini Flash Lite) ──

_ROUTER_SYSTEM = """너는 학술 figure 생성 라우터다. 입력으로 들어오는 figure
설명을 보고 두 값 중 하나로 분류:

- "script": matplotlib/TikZ 코드로 그릴 수 있는 도식.
  예: 그래프·플롯·차트·수학 도식·결정 경계·덴드로그램·순서도·네트워크 도식.

- "model": 코드로 그리기 어려운 사실적/일러스트/예술적 이미지.
  예: 실제 사진, 세포·분자 일러스트, 풍경, 스타일화된 장면, 인물.

학술 ML/통계/수학 맥락이면 거의 항상 "script". 애매하면 "script" 선택.
JSON schema 에 맞게 응답."""


def classify_route(
    *,
    hint: str,
    context: str = "",
    model: str = "gemini-flash-latest",
    log_cb: LogCb | None = None,
) -> RouteDecision:
    """Gemini Flash 로 script vs model 라우팅 결정."""
    client = _get_client()
    _limiter.wait()
    _emit(log_cb, f"[router] Gemini {model} 분류 호출")

    prompt_text = f"figure hint: {hint}\ncontext: {context[:500]}"
    schema = _pydantic_to_gemini_schema(RouteDecision)

    # 분류는 단순 task → thinking_level=low 로 thinking token 최소화.
    # SDK 버전/모델에 따라 thinking_config 미지원일 수 있어 try/except fallback.
    cfg_kwargs = dict(
        system_instruction=_ROUTER_SYSTEM,
        response_mime_type="application/json",
        response_schema=schema,
        max_output_tokens=2000,  # thinking tokens 여유 포함
    )
    try:
        cfg = types.GenerateContentConfig(
            **cfg_kwargs,
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        )
    except Exception:
        cfg = types.GenerateContentConfig(**cfg_kwargs)

    try:
        resp = client.models.generate_content(
            model=model,
            contents=[types.Part.from_text(text=prompt_text)],
            config=cfg,
        )
    except Exception as exc:
        raise CodexRunError(f"router 호출 실패: {exc}") from exc

    text = (resp.text or "").strip()
    if not text:
        # 기본 fallback — 학술 맥락 bias
        _emit(log_cb, "[router] 응답 비었음 — 'script' 기본값")
        return RouteDecision(route="script", reason="empty response fallback")

    try:
        decision = RouteDecision.model_validate_json(text)
    except Exception as exc:
        _emit(log_cb, f"[router] 파싱 실패 — 'script' 기본값: {exc}")
        return RouteDecision(route="script", reason=f"parse error fallback: {exc}")

    _emit(log_cb, f"[router] → {decision.route} ({decision.reason[:80]})")
    return decision
