"""Gemini (google.genai) 백엔드 — in-process vision+text LLM 호출.

`run_codex_task`와 동일한 (prompt, inputs, output_schema, model, timeout) 인터페이스를
제공해 dispatcher가 라우팅만 바꾸면 되도록 설계. WSL/subprocess/파일시스템 불필요.

- inputs의 bytes/Path/str는 MIME 추론 후 Part.from_bytes로 전달.
- output_schema가 있으면 response_schema + response_mime_type=JSON 강제.
- 쓰로틀링: 내부 RateLimiter. config에서 `gemini_rpm` 로 제어.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path

from google import genai
from google.genai import types

from codex_runner import CodexRunError


# ───────────────────────── 클라이언트 싱글톤 ─────────────────────────

_client: genai.Client | None = None
_client_lock = threading.Lock()


def _get_client() -> genai.Client:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise CodexRunError(
                    "GEMINI_API_KEY 미설정. .env에 추가하거나 "
                    "config의 backend를 'codex'로 전환하세요."
                )
            _client = genai.Client(api_key=api_key)
    return _client


# ───────────────────────── RateLimiter (thread-safe) ─────────────────────────

class _RateLimiter:
    def __init__(self, rpm: int = 0):
        self.rpm = rpm
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.rpm <= 0:
            return
        with self._lock:
            now = time.monotonic()
            window_start = now - 60.0
            while self._timestamps and self._timestamps[0] < window_start:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.rpm:
                oldest = self._timestamps[0]
                wait_s = 60.0 - (now - oldest) + 0.05
                if wait_s > 0:
                    time.sleep(wait_s)
                now = time.monotonic()
                window_start = now - 60.0
                while self._timestamps and self._timestamps[0] < window_start:
                    self._timestamps.popleft()
            self._timestamps.append(now)


_rate_limiter = _RateLimiter(rpm=0)


def set_rpm(rpm: int) -> None:
    """외부에서 RPM 반영 (dispatcher가 config 읽을 때 호출)."""
    _rate_limiter.rpm = max(0, int(rpm))


# ───────────────────────── Input 정규화 ─────────────────────────

_MIME_MAP = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "txt": "text/plain",
    "json": "application/json",
}


def _infer_mime(name: str) -> str:
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    return _MIME_MAP.get(ext, "application/octet-stream")


def _to_bytes(value) -> bytes:
    if isinstance(value, Path):
        return value.read_bytes()
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    raise CodexRunError(f"지원하지 않는 input 타입: {type(value).__name__}")


# Gemini structured output이 거부하는 JSON Schema 키들 (OpenAI strict에서는 필수이지만
# Gemini OpenAPI subset은 이를 이해 못함). 재귀적으로 제거한다.
_GEMINI_UNSUPPORTED_KEYS = {
    "additionalProperties",
    "$schema",
    "$id",
    "$ref",
    "definitions",
    "oneOf",
    "allOf",
    "not",
    "patternProperties",
    "propertyNames",
}


def _sanitize_schema_for_gemini(schema):
    """Gemini 미지원 키를 재귀적으로 제거한 사본 반환."""
    if isinstance(schema, dict):
        return {
            k: _sanitize_schema_for_gemini(v)
            for k, v in schema.items()
            if k not in _GEMINI_UNSUPPORTED_KEYS
        }
    if isinstance(schema, list):
        return [_sanitize_schema_for_gemini(x) for x in schema]
    return schema


def _extract_json(text: str) -> str:
    """Gemma처럼 schema 엄밀 준수가 약한 모델의 응답에서 JSON 블록만 추출.

    - markdown fence ```json ... ``` 제거
    - 첫 '{' ~ 매칭 '}' 까지의 balanced substring 반환
    - 문자열 리터럴 안의 중괄호도 무시 (간단한 state machine)
    """
    t = text.strip()
    # markdown fence 제거
    if t.startswith("```"):
        # 첫 줄(```json or ```) 제거
        nl = t.find("\n")
        if nl >= 0:
            t = t[nl + 1:]
        # 마지막 ``` 제거
        end = t.rfind("```")
        if end >= 0:
            t = t[:end]
        t = t.strip()

    # 첫 '{' 찾아서 balanced 매칭
    start = t.find("{")
    if start < 0:
        return t
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return t[start:i + 1]
    return t[start:]


# ───────────────────────── 메인 함수 ─────────────────────────

def run_gemini_task(
    prompt: str,
    inputs: dict[str, str | bytes | Path],
    *,
    output_schema: dict | None = None,
    model: str = "gemini-2.5-pro",
    timeout: int = 120,  # 현 SDK는 함수 인자로 timeout 미지원 — 명시적 취소 없음
) -> dict[str, bytes]:
    """in-process Gemini 호출. run_codex_task와 호환되는 반환 형태.

    Returns:
        {"result.json": bytes} — schema 준수 JSON text.

    Raises:
        CodexRunError: 입력/API/파싱 오류.
    """
    if not prompt or not prompt.strip():
        raise CodexRunError("prompt가 비어있음")
    if not inputs:
        raise CodexRunError("inputs가 비어있음")

    client = _get_client()

    parts = []
    for name, val in inputs.items():
        data = _to_bytes(val)
        mime = _infer_mime(name)
        parts.append(types.Part.from_bytes(data=data, mime_type=mime))
    parts.append(types.Part.from_text(text=prompt))

    gen_config = types.GenerateContentConfig()
    if output_schema is not None:
        gen_config.response_mime_type = "application/json"
        gen_config.response_schema = _sanitize_schema_for_gemini(output_schema)

    _rate_limiter.wait()
    try:
        resp = client.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=parts)],
            config=gen_config,
        )
    except Exception as exc:
        raise CodexRunError(f"Gemini 호출 실패 ({model}): {exc}") from exc

    text = (resp.text or "").strip()
    if not text:
        raise CodexRunError("Gemini 응답이 비어있음")

    if output_schema is not None:
        # Gemma/일부 모델이 response_schema를 완전 준수 못해 JSON 뒤에 extra text를
        # 붙이는 경우 대응: markdown fence 제거 + 첫 balanced {...} 추출.
        extracted = _extract_json(text)
        try:
            json.loads(extracted)
        except json.JSONDecodeError as exc:
            raise CodexRunError(
                f"Gemini JSON 파싱 실패: {exc}\n"
                f"추출 text (len={len(extracted)}): {extracted[:200]}...\n"
                f"원본 tail: {text[-200:]}"
            ) from exc
        text = extracted

    return {"result.json": text.encode("utf-8")}
