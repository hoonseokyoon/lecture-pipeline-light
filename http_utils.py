"""HTTP 공통 유틸: per-endpoint rate limit + exponential backoff.

외부 API 호출 시 사용. Thread-safe — lit_fetch 병렬 다운로드에서도 안전.

사용 예:
    from http_utils import RateLimiter, get_with_retry

    ss_limiter = RateLimiter(rps=1.0, name="semantic_scholar")
    resp = get_with_retry(
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params={...}, rate_limiter=ss_limiter, log_name="SS-search",
    )

정책:
- 429, 502/503/504, ConnectionError, Timeout → exponential backoff 재시도
- 기타 4xx → 재시도 없이 그대로 반환 (호출자가 해석)
- Retry-After 헤더가 있으면 그것을 우선
- Jitter 로 thundering herd 방지
"""

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger("lecture_pipeline.http_utils")

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_INITIAL_BACKOFF = 1.0
DEFAULT_MAX_BACKOFF = 30.0
DEFAULT_JITTER = 0.5
DEFAULT_RETRY_STATUSES = (429, 500, 502, 503, 504)


class HttpError(Exception):
    """재시도 exhausted."""


class RateLimiter:
    """연속 호출 사이 최소 간격 보장. thread-safe.

    토큰 버킷 간소화 버전 — burst 허용 필요하면 확장.
    """

    def __init__(self, rps: float, name: str = ""):
        if rps <= 0:
            raise ValueError("rps 는 양수여야 함")
        self.min_interval = 1.0 / rps
        self.last_call = 0.0
        self.name = name or f"<rps={rps}>"
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """호출 직전 블로킹 대기."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_call
            wait = self.min_interval - elapsed
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.monotonic()

    def __repr__(self) -> str:
        return f"RateLimiter({self.name}, {1/self.min_interval:.2f} req/s)"


class CrossProcessRateLimiter:
    """크로스 프로세스 rate limiter — tempdir lockfile + timestamp 파일.

    동일 머신에서 여러 Python 프로세스가 같은 `name` 을 쓰면 **모든 프로세스
    통틀어** min_interval 을 보장. Head Agent 가 skill subprocess 를 연속
    호출해도 외부 API 에는 안전.

    패턴: O_CREAT|O_EXCL 파일락 → 상태 파일 read-modify-write → 락 해제.
    락 획득 실패(stale 감지 포함) 시 best-effort 로 진행 (에러 X).

    doc_decode/scripts/_gemini.py 와 동일 프로토콜.
    """

    _STALE_LOCK_SEC = 10.0

    def __init__(self, rps: float, name: str):
        if rps <= 0:
            raise ValueError("rps 는 양수여야 함")
        self.min_interval = 1.0 / rps
        self.name = name
        self._proc_lock = threading.Lock()
        self._tmp = Path(tempfile.gettempdir())
        # 파일명에 영문·숫자·dash 만. name 이 안전하지 않으면 hash.
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        self._lock_path = self._tmp / f"litrl-{safe}.lock"
        self._state_path = self._tmp / f"litrl-{safe}.ts"

    def _acquire_file_lock(self, timeout: float = 5.0) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return os.open(
                    str(self._lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o600,
                )
            except FileExistsError:
                # stale lock?
                try:
                    age = time.time() - self._lock_path.stat().st_mtime
                    if age > self._STALE_LOCK_SEC:
                        try:
                            self._lock_path.unlink()
                        except FileNotFoundError:
                            pass
                        continue
                except OSError:
                    pass
                time.sleep(0.02)
        return None

    def _release_file_lock(self, fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _read_last_ts(self) -> float:
        try:
            raw = self._state_path.read_text(encoding="utf-8").strip()
            return float(raw)
        except (OSError, ValueError):
            return 0.0

    def _write_last_ts(self, ts: float) -> None:
        try:
            self._state_path.write_text(f"{ts:.6f}", encoding="utf-8")
        except OSError:
            pass

    def acquire(self) -> None:
        """전체 프로세스 통틀어 min_interval 이후까지 블로킹."""
        while True:
            should_wait = False
            wait_secs = 0.0
            with self._proc_lock:
                fd = self._acquire_file_lock(timeout=5.0)
                if fd is None:
                    # 락 획득 실패: best-effort, in-process 만 엄격
                    logger.debug(
                        "[%s] cross-process 락 실패 — in-process only",
                        self.name,
                    )
                    return
                try:
                    now = time.time()  # wall clock (크로스프로세스)
                    last = self._read_last_ts()
                    wait = self.min_interval - (now - last)
                    if wait > 0:
                        should_wait = True
                        wait_secs = min(wait, self.min_interval)
                    else:
                        self._write_last_ts(now)
                        return
                finally:
                    self._release_file_lock(fd)
            # (proc lock 해제된 상태) 다른 프로세스 차단 안 하도록 여기서 대기
            if should_wait:
                time.sleep(wait_secs)
                continue
            return

    def __repr__(self) -> str:
        return (
            f"CrossProcessRateLimiter({self.name}, "
            f"{1/self.min_interval:.2f} req/s, lock={self._lock_path.name})"
        )


def _compute_backoff(
    attempt: int,
    initial: float = DEFAULT_INITIAL_BACKOFF,
    cap: float = DEFAULT_MAX_BACKOFF,
    jitter: float = DEFAULT_JITTER,
) -> float:
    """초 단위 대기 시간. attempt 는 1부터 시작."""
    base = initial * (2 ** (attempt - 1))
    base = min(base, cap)
    if jitter > 0:
        base += random.uniform(0, jitter)
    return base


def _parse_retry_after(resp: requests.Response) -> float | None:
    """Retry-After 헤더 파싱. 숫자(초) 만 지원. HTTP-date 는 무시."""
    val = (resp.headers.get("Retry-After") or "").strip()
    if not val:
        return None
    try:
        n = float(val)
        # 상한 — 미친 값 (예: 하루) 은 무시
        if 0 < n <= 300:
            return n
    except ValueError:
        pass
    return None


def request_with_retry(
    method: str,
    url: str,
    *,
    rate_limiter: RateLimiter | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    initial_backoff: float = DEFAULT_INITIAL_BACKOFF,
    max_backoff: float = DEFAULT_MAX_BACKOFF,
    jitter: float = DEFAULT_JITTER,
    retry_on_status: tuple = DEFAULT_RETRY_STATUSES,
    log_name: str = "",
    **request_kwargs: Any,
) -> requests.Response:
    """일반 재시도 엔진.

    반환: 최종 응답 (성공 OR 재시도 소진 후 마지막 응답).
    예외: ConnectionError/Timeout 이 max_attempts 까지 반복되면 HttpError.
    """
    display = log_name or url.split("?")[0]
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        if rate_limiter is not None:
            rate_limiter.acquire()

        try:
            resp = requests.request(method, url, **request_kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt == max_attempts:
                raise HttpError(
                    f"{display} 연결 실패 — {max_attempts}회 재시도: {exc}"
                ) from exc
            wait = _compute_backoff(attempt, initial_backoff, max_backoff, jitter)
            logger.warning(
                "[%s] 연결 오류 (%s) — %.2fs 후 재시도 %d/%d",
                display, exc.__class__.__name__, wait, attempt, max_attempts,
            )
            time.sleep(wait)
            continue

        if resp.status_code in retry_on_status and attempt < max_attempts:
            retry_after = _parse_retry_after(resp)
            wait = retry_after or _compute_backoff(
                attempt, initial_backoff, max_backoff, jitter,
            )
            logger.warning(
                "[%s] HTTP %d%s — %.2fs 후 재시도 %d/%d",
                display, resp.status_code,
                " (Retry-After)" if retry_after else "",
                wait, attempt, max_attempts,
            )
            time.sleep(wait)
            continue

        # 성공 또는 재시도 불필요 상태코드
        return resp

    # 이론상 unreachable
    raise HttpError(f"{display} 재시도 exhausted (last_exc={last_exc})")


def get_with_retry(url: str, **kwargs: Any) -> requests.Response:
    return request_with_retry("GET", url, **kwargs)


def post_with_retry(url: str, **kwargs: Any) -> requests.Response:
    return request_with_retry("POST", url, **kwargs)
