"""Gemini 호출 래퍼 — detect/annotate 스크립트 전용.

호스트 측 `_lib/gemini_backend.py` 와 API 는 유사하지만, CodexRunError 대신
자체 RuntimeError 를 쓰고 rate limiter 는 단순 sleep 방식.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    # skill workdir 가 아닌 프로젝트 root 의 .env 를 상속받게끔, 환경변수가 이미
    # 주입돼있으면 덮어쓰지 않음.
    load_dotenv(override=False)
except ImportError:
    pass

from google import genai
from google.genai import types


class GeminiError(RuntimeError):
    pass


# ── cross-process rate limiter ──
#
# `DOC_DECODE_GEMINI_RPM` (int) 가 세팅되면, tempdir 에 공유되는 타임스탬프
# 리스트로 분당 호출 수를 제한한다. `O_CREAT|O_EXCL` 파일락으로 read-modify-write
# 직렬화. best-effort — 락 획득 실패 시 그냥 진행한다.

_RL_LOCK_PATH = Path(tempfile.gettempdir()) / "doc_decode_gemini_rl.lock"
_RL_STATE_PATH = Path(tempfile.gettempdir()) / "doc_decode_gemini_rl.json"
_RL_PROC_LOCK = threading.Lock()
_RL_LOCK_STALE_SEC = 10.0


def _rl_rpm() -> int:
    try:
        return max(0, int(os.environ.get("DOC_DECODE_GEMINI_RPM", "0") or "0"))
    except ValueError:
        return 0


def _rl_acquire_lock(timeout: float = 10.0) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return os.open(
                str(_RL_LOCK_PATH),
                os.O_CREAT | os.O_EXCL | os.O_RDWR,
                0o600,
            )
        except FileExistsError:
            try:
                if time.time() - _RL_LOCK_PATH.stat().st_mtime > _RL_LOCK_STALE_SEC:
                    try:
                        _RL_LOCK_PATH.unlink()
                    except FileNotFoundError:
                        pass
                    continue
            except OSError:
                pass
            time.sleep(0.02)
    return None


def _rl_release_lock(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        _RL_LOCK_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _wait_for_rpm_slot() -> None:
    rpm = _rl_rpm()
    if rpm <= 0:
        return
    while True:
        sleep_for = 0.0
        with _RL_PROC_LOCK:
            fd = _rl_acquire_lock()
            if fd is None:
                return  # best-effort: 락 실패 시 그냥 호출
            try:
                timestamps: list[float] = []
                if _RL_STATE_PATH.exists():
                    try:
                        raw = json.loads(_RL_STATE_PATH.read_text(encoding="utf-8"))
                        if isinstance(raw, list):
                            timestamps = [t for t in raw if isinstance(t, (int, float))]
                    except (OSError, json.JSONDecodeError):
                        pass
                now = time.time()
                cutoff = now - 60.0
                timestamps = [t for t in timestamps if t > cutoff]
                if len(timestamps) < rpm:
                    timestamps.append(now)
                    try:
                        _RL_STATE_PATH.write_text(
                            json.dumps(timestamps), encoding="utf-8",
                        )
                    except OSError:
                        pass
                    return
                sleep_for = max(0.0, 60.0 - (now - timestamps[0]) + 0.05)
            finally:
                _rl_release_lock(fd)
        if sleep_for > 0:
            time.sleep(min(sleep_for, 2.0))


_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if _client is not None:
        return _client
    # doc_decode 는 on-demand 유료 키 우선, 없으면 표준 키로 fallback.
    api_key = (
        os.environ.get("GEMINI_API_KEY_ON_DEMAND")
        or os.environ.get("GEMINI_API_KEY")
    )
    if not api_key:
        raise GeminiError("GEMINI_API_KEY_ON_DEMAND 또는 GEMINI_API_KEY 미설정")
    _client = genai.Client(api_key=api_key)
    return _client


_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


def _mime(name: str) -> str:
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    return _MIME.get(ext, "application/octet-stream")


def _to_bytes(v) -> bytes:
    if isinstance(v, Path):
        return v.read_bytes()
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    if isinstance(v, str):
        return v.encode("utf-8")
    raise GeminiError(f"지원하지 않는 input 타입: {type(v).__name__}")


_UNSUPPORTED = {
    "additionalProperties", "$schema", "$id", "$ref",
    "definitions", "oneOf", "allOf", "not",
    "patternProperties", "propertyNames",
}


def _sanitize(s):
    if isinstance(s, dict):
        return {k: _sanitize(v) for k, v in s.items() if k not in _UNSUPPORTED}
    if isinstance(s, list):
        return [_sanitize(x) for x in s]
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
    esc = False
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
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


def call(
    prompt: str,
    inputs: dict[str, object],
    *,
    output_schema: dict | None = None,
    model: str = "gemini-2.5-pro",
    max_retries: int = 2,
) -> dict:
    """Gemini 1회 호출. output_schema 지정 시 JSON, 아니면 text.

    Returns:
        output_schema 지정 시: dict (parsed JSON)
        없으면: {"text": str}
    """
    client = get_client()

    parts = []
    for name, val in inputs.items():
        data = _to_bytes(val)
        parts.append(types.Part.from_bytes(data=data, mime_type=_mime(name)))
    parts.append(types.Part.from_text(text=prompt))

    cfg = types.GenerateContentConfig()
    if output_schema is not None:
        cfg.response_mime_type = "application/json"
        cfg.response_schema = _sanitize(output_schema)

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        _wait_for_rpm_slot()
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
            raise GeminiError(f"Gemini 호출 실패 ({model}): {exc}") from exc
    else:
        raise GeminiError(f"Gemini 호출 실패 (retries 소진): {last_exc}")

    text = (resp.text or "").strip()
    if not text:
        raise GeminiError("응답이 비어있음")

    if output_schema is None:
        return {"text": text}

    extracted = _extract_json(text)
    try:
        return json.loads(extracted)
    except json.JSONDecodeError as exc:
        raise GeminiError(
            f"JSON 파싱 실패: {exc}\n원본 tail: {text[-200:]}"
        ) from exc
