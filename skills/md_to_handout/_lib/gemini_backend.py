"""Gemini 3 Pro 백엔드 — md_to_handout 전용.

`skills/doc_decode/_lib/gemini_backend.py` 의 싱글톤·rate-limiter 패턴을 복사하고,
Gemini 3 Pro (= `gemini-pro-latest`) 에 맞춰 다음 3가지를 바꿨다:

1. `thinking_budget` 대신 `thinking_level` ("low|medium|high") — Gemini 3 API.
2. `temperature` 는 1.0 고정 (Google 이 낮추지 말라고 명시).
3. `response_schema=<PydanticModel>` 로 구조화 출력을 강제 → `resp.parsed` 로 타입
   인스턴스를 바로 받음.

Safety 는 `HarmBlockThreshold.OFF` 먼저 시도하고, allowlist 미등록 키에서 permission
에러가 나면 `BLOCK_NONE` 로 재시도. 과학 교재의 의학·생리 키워드에서 false positive
차단 방지.

키 우선순위: `GEMINI_API_KEY_ON_DEMAND` > `GEMINI_API_KEY`.
"""

from __future__ import annotations

import copy
import io
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from google import genai
from google.genai import types
from pydantic import BaseModel

from codex_runner import CodexRunError

logger = logging.getLogger("lecture_pipeline.md_to_handout.gemini")


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
    """싱글톤 Client. `GEMINI_API_KEY_ON_DEMAND` 우선."""
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
                    "GEMINI_API_KEY_ON_DEMAND 또는 GEMINI_API_KEY 미설정. "
                    ".env 파일 확인."
                )
            # HttpOptions.timeout 은 ms. SDK 버전 호환성 위해 실패 시 fallback.
            try:
                _client = genai.Client(
                    api_key=api_key,
                    http_options=types.HttpOptions(timeout=180_000),
                )
            except Exception:
                _client = genai.Client(api_key=api_key)
    return _client


# ── rate limiter (token-bucket, RPM 기반) ──
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
                oldest = self._ts[0]
                sleep = 60.0 - (now - oldest) + 0.05
                if sleep > 0:
                    time.sleep(sleep)
                now = time.monotonic()
                win = now - 60.0
                while self._ts and self._ts[0] < win:
                    self._ts.popleft()
            self._ts.append(now)


_rate_limiter = _RateLimiter(rpm=0)


def set_rpm(rpm: int) -> None:
    _rate_limiter.rpm = max(0, int(rpm))


# ── 이미지 전처리 ──

_SUPPORTED_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _prepare_image_part(
    path: Path, max_dim: int = 1024
) -> types.Part:
    """Pillow 로 long-edge 를 `max_dim` 으로 downscale.

    - alpha 채널 있는 PNG → PNG 유지 (투명도 손상 방지).
    - 그 외 → JPEG q=85 재인코딩 (토큰 비용 절약).
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise CodexRunError(
            "Pillow 미설치. `pip install Pillow` 필요."
        ) from exc

    with Image.open(path) as im:
        im.load()
        has_alpha = (im.mode in ("RGBA", "LA")) or (
            im.mode == "P" and "transparency" in im.info
        )
        w, h = im.size
        longest = max(w, h)
        if longest > max_dim:
            scale = max_dim / float(longest)
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            im = im.resize(new_size, Image.LANCZOS)

        buf = io.BytesIO()
        if has_alpha:
            im.save(buf, format="PNG", optimize=True)
            mime = "image/png"
        else:
            # JPEG 는 RGB 만 받음
            if im.mode != "RGB":
                im = im.convert("RGB")
            im.save(buf, format="JPEG", quality=85, optimize=True)
            mime = "image/jpeg"
        data = buf.getvalue()
    return types.Part.from_bytes(data=data, mime_type=mime)


# ── safety settings ──

_ALL_HARM_CATEGORIES = (
    types.HarmCategory.HARM_CATEGORY_HARASSMENT,
    types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
    types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
    types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
)


def _safety_settings(threshold: types.HarmBlockThreshold) -> list:
    """5개 카테고리 전체에 동일 threshold 를 적용.

    CIVIC_INTEGRITY 는 SDK 버전에 따라 enum 에 없을 수 있으므로 getattr 로 유연.
    """
    cats = list(_ALL_HARM_CATEGORIES)
    civic = getattr(
        types.HarmCategory, "HARM_CATEGORY_CIVIC_INTEGRITY", None
    )
    if civic is not None:
        cats.append(civic)
    return [
        types.SafetySetting(category=c, threshold=threshold)
        for c in cats
    ]


def _is_safety_permission_error(exc: Exception) -> bool:
    """OFF 가 allowlist 필요라 거부된 건지 감지."""
    msg = str(exc).lower()
    markers = (
        "permission", "allowlist", "not allowed",
        "unauthorized", "quota", "harmblockthreshold",
    )
    return any(m in msg for m in markers)


# ── Pydantic → Gemini schema 변환 ──
#
# Pydantic v2 가 생성하는 JSON schema 는 Gemini API 가 모르는 필드들을 포함한다:
#   - $defs / $ref (nested model 재귀 구조)
#   - additionalProperties (ConfigDict(extra="forbid"))
#   - $schema, $id, definitions, oneOf/allOf/not 등
# Gemini API 는 이를 만나면 `400 INVALID_ARGUMENT: Unknown name ...` 로 거절.
# 그래서 (1) $ref 를 inline 하고 (2) 비지원 key 를 전부 제거한 dict 를 넘긴다.

_GEMINI_UNSUPPORTED_KEYS = {
    "additionalProperties", "$schema", "$id", "$ref", "$defs",
    "definitions", "oneOf", "allOf", "not",
    "patternProperties", "propertyNames",
    # Gemini 3 Pro (gemini-pro-latest) 는 array-of-objects 에 `maxItems`/`minItems`
    # 가 붙고 그게 또 상위 array 의 items 안에 중첩된 경우 400 으로 거절한다.
    # (경험적 확인 — 2026-04 gemini-pro-latest = gemini-3-pro-preview).
    # Pydantic 이 model_validate_json 단계에서 어차피 강제하므로 Gemini 에 보내는
    # 스키마에서만 제거. 수량 제한은 prompt.txt 가 자연어로 지시.
    "maxItems", "minItems",
}


def _inline_refs(schema: dict) -> dict:
    """Pydantic v2 가 emit 하는 `$ref` 를 `$defs` 내용으로 치환, `$defs` 제거."""
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
    """Pydantic 모델 → Gemini-호환 JSON schema dict."""
    raw = model.model_json_schema()
    return _sanitize_schema(_inline_refs(raw))


# ── 메인 API ──


def count_tokens_for(
    *,
    model: str,
    parts: list[types.Part],
    system_instruction: str,
) -> int:
    """실제 호출 전 token 수 사전 조회."""
    client = _get_client()
    # count_tokens 는 system_instruction 을 직접 받지 않는 SDK 버전이 있음.
    # system 문자열을 prefix text Part 로 합산해 근사치 (±수십 토큰).
    probe_parts = [types.Part.from_text(text=system_instruction), *parts]
    try:
        resp = client.models.count_tokens(
            model=model,
            contents=[types.Content(role="user", parts=probe_parts)],
        )
        return int(getattr(resp, "total_tokens", 0))
    except Exception as exc:
        logger.warning("count_tokens 실패: %s", exc)
        return 0


def run_gemini_structured(
    *,
    model: str,
    system_instruction: str,
    parts: list[types.Part],
    response_model: type[BaseModel],
    thinking_level: str = "high",
    temperature: float = 1.0,
    seed: int | None = None,
    max_output_tokens: int = 32000,
    media_resolution: str = "media_resolution_high",
    log_cb: LogCb | None = None,
) -> tuple[BaseModel, dict]:
    """Gemini 3 Pro 구조화 생성.

    Returns:
        (parsed_model, usage_dict)
    """
    client = _get_client()

    # Pydantic 모델을 그대로 넘기면 SDK 가 추가하는 메타 필드들이
    # Gemini proto 에 매칭 안 돼 400 이 남. 직접 sanitize 한 dict 로 전달.
    gemini_schema = _pydantic_to_gemini_schema(response_model)

    def _build_config(
        threshold: types.HarmBlockThreshold,
    ) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=gemini_schema,
            temperature=temperature,
            seed=seed,
            max_output_tokens=max_output_tokens,
            thinking_config=types.ThinkingConfig(
                thinking_level=thinking_level
            ),
            media_resolution=media_resolution,
            safety_settings=_safety_settings(threshold),
        )

    # OFF 먼저, permission 에러면 BLOCK_NONE 로 fallback.
    safety_order = [
        types.HarmBlockThreshold.OFF,
        types.HarmBlockThreshold.BLOCK_NONE,
    ]

    max_attempts = 3
    backoff = (1.0, 2.0, 4.0)
    last_exc: Exception | None = None
    resp = None

    for safety_attempt, threshold in enumerate(safety_order):
        cfg = _build_config(threshold)
        for attempt in range(max_attempts):
            _rate_limiter.wait()
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=[types.Content(role="user", parts=parts)],
                    config=cfg,
                )
                break
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                # 429 / 503 / DeadlineExceeded 만 재시도
                if any(
                    tok in msg
                    for tok in (
                        "429", "503", "RESOURCE_EXHAUSTED",
                        "UNAVAILABLE", "DeadlineExceeded", "timeout",
                    )
                ):
                    wait = backoff[attempt] if attempt < len(backoff) else backoff[-1]
                    _emit(
                        log_cb,
                        f"[md_to_handout] Gemini 재시도 {attempt+1}/{max_attempts} "
                        f"({msg[:120]}) — {wait:.1f}s 대기",
                    )
                    time.sleep(wait)
                    continue
                # safety permission 에러면 fallback threshold 로 넘어감
                if (
                    _is_safety_permission_error(exc)
                    and safety_attempt + 1 < len(safety_order)
                ):
                    _emit(
                        log_cb,
                        f"[md_to_handout] safety=OFF 거부 — BLOCK_NONE 로 재시도",
                    )
                    resp = None
                    break
                # 그 외 에러는 즉시 raise
                raise CodexRunError(
                    f"Gemini 호출 실패 ({model}): {msg}"
                ) from exc
        if resp is not None:
            break  # 성공
        # 안쪽 루프가 safety fallback 으로 빠진 경우만 바깥 루프 계속

    if resp is None:
        raise CodexRunError(
            f"Gemini 호출 실패 ({model}, {max_attempts}회 재시도 후): {last_exc}"
        ) from last_exc

    # usage 수집
    usage: dict = {}
    um = getattr(resp, "usage_metadata", None)
    if um is not None:
        usage = {
            "prompt_tokens": getattr(um, "prompt_token_count", None),
            "output_tokens": getattr(um, "candidates_token_count", None),
            "thoughts_tokens": getattr(um, "thoughts_token_count", None),
            "total_tokens": getattr(um, "total_token_count", None),
        }

    # response_schema 가 dict 라 resp.parsed 가 Pydantic 인스턴스를 못 만듦.
    # resp.parsed 는 dict 일 수도, None 일 수도 있음 — 둘 다 커버.
    parsed_raw = getattr(resp, "parsed", None)
    if isinstance(parsed_raw, dict):
        try:
            parsed = response_model.model_validate(parsed_raw)
        except Exception as exc:
            raise CodexRunError(
                f"Gemini 응답 Pydantic 검증 실패: {exc}\n"
                f"raw: {str(parsed_raw)[:300]}"
            ) from exc
    else:
        text = (resp.text or "").strip()
        if not text:
            raise CodexRunError(
                "Gemini 응답이 비어있음 (resp.parsed 와 resp.text 모두 공백)"
            )
        try:
            parsed = response_model.model_validate_json(text)
        except Exception as exc:
            raise CodexRunError(
                f"Gemini JSON 파싱 실패: {exc}\n"
                f"원본 tail: {text[-300:]}"
            ) from exc

    return parsed, usage
