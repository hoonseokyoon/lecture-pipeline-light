"""Gemini (google.genai) 호스트 측 백엔드 — profile / (optional) orchestrator 용.

detect/annotate 는 scripts/ 쪽에서 별도 Gemini 호출을 하므로 이 모듈은
composite orchestrator 만 사용한다. zero_shot_rec / exam_problem_detect 의
gemini_backend 와 동일 계약.
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


# ── client singleton ──
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
                    "GEMINI_API_KEY 미설정. .env 파일 확인."
                )
            # 네트워크 hang 방지. google-genai 의 HttpOptions.timeout 은
            # 밀리초 단위. SDK 버전에 따라 필드명이 달라질 수 있으므로 실패 시
            # 조용히 fallback (timeout 없는 client).
            try:
                _client = genai.Client(
                    api_key=api_key,
                    http_options=types.HttpOptions(timeout=120_000),
                )
            except Exception:
                _client = genai.Client(api_key=api_key)
    return _client


# ── rate limiter (per-process, thread-safe) ──
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


_MIME_MAP = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "txt": "text/plain",
    "json": "application/json",
    "md": "text/markdown",
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


_GEMINI_UNSUPPORTED_KEYS = {
    "additionalProperties", "$schema", "$id", "$ref",
    "definitions", "oneOf", "allOf", "not",
    "patternProperties", "propertyNames",
}


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


def _extract_json(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl >= 0:
            t = t[nl + 1:]
        end = t.rfind("```")
        if end >= 0:
            t = t[:end]
        t = t.strip()
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


def run_gemini_task(
    prompt: str,
    inputs: dict[str, str | bytes | Path],
    *,
    output_schema: dict | None = None,
    model: str = "gemini-2.5-pro",
    timeout: int = 120,
) -> dict[str, bytes]:
    if not prompt or not prompt.strip():
        raise CodexRunError("prompt 가 비어있음")
    if not inputs:
        raise CodexRunError("inputs 가 비어있음")

    client = _get_client()

    parts = []
    for name, val in inputs.items():
        data = _to_bytes(val)
        mime = _infer_mime(name)
        parts.append(types.Part.from_bytes(data=data, mime_type=mime))
    parts.append(types.Part.from_text(text=prompt))

    cfg = types.GenerateContentConfig()
    if output_schema is not None:
        cfg.response_mime_type = "application/json"
        cfg.response_schema = _sanitize_schema(output_schema)

    max_retries = 2
    last_exc: Exception | None = None
    resp = None
    for attempt in range(max_retries + 1):
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
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
    if resp is None:
        raise CodexRunError(
            f"Gemini 호출 실패 ({model}, {max_retries + 1}회 시도): {last_exc}"
        ) from last_exc

    text = (resp.text or "").strip()
    if not text:
        raise CodexRunError("Gemini 응답이 비어있음")

    if output_schema is not None:
        extracted = _extract_json(text)
        try:
            json.loads(extracted)
        except json.JSONDecodeError as exc:
            raise CodexRunError(
                f"Gemini JSON 파싱 실패: {exc}\n"
                f"원본 tail: {text[-200:]}"
            ) from exc
        text = extracted

    return {"result.json": text.encode("utf-8")}
