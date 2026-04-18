#!/usr/bin/env python
"""Codex CLI 기반 재사용 가능한 작업 실행기.

prompt + input files (+ optional scripts) -> output files 구조.

사용 예:

    from pathlib import Path
    from codex_runner import run_codex_task, CodexRunError

    outputs = run_codex_task(
        prompt="inputs/raw.txt의 오타를 고쳐서 outputs/clean.txt로 저장해.",
        inputs={"raw.txt": raw_text},
        expected_outputs=["clean.txt"],
    )
    clean_text = outputs["clean.txt"]

사전 준비:
- Codex CLI 네이티브 설치 (예: `npm i -g @openai/codex`).
  `codex.cmd`/`codex.exe` 또는 `codex` 바이너리가 PATH 에 있거나, 환경변수
  `LECTURE_CODEX_PATH` 로 절대경로 지정. `codex auth login` 실행 필요.
- `git` CLI 설치 (Codex `--full-auto` 는 작업 디렉토리가 git repo 일 것을 요구).
- Python 3.10+ (venv_manager 가 각 스킬의 requirements.txt 를 자동 처리).
- Claude fallback 을 사용하려면 WSL Ubuntu + Claude CLI 설치 필요.

구조:
- `CodexRunner` 클래스가 mutable 상태(semaphore/stagger/active_procs/cancel events/
  skill cache/venv manager)를 캡슐화. 모듈 레벨에는 `_default = CodexRunner()`
  싱글톤이 있고, 기존 함수(`run_codex_task`, `run_skill`, `load_skill`, ...)는
  전부 이 기본 인스턴스에 위임하는 얇은 래퍼다. 테스트 격리나 다중 러너가
  필요하면 직접 `CodexRunner()` 인스턴스를 만들어 사용.
- ContextVar 3개(`codex_priority`, `claude_only_mode`, `current_cancel_event`)는
  프로세스 전역 개념이라 모듈 레벨에 둔다.
- Codex 호출은 네이티브 (WSL 경유 X). Claude fallback 만 WSL 유지.
  스킬별 Python 의존성은 `venv_manager.VenvManager` 가 하네스 측에서 venv 관리.
"""

import contextvars
import heapq
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from venv_manager import VenvManager

# 네임스페이스 로거. `lecture_pipeline` 이하에 핸들러를 부착하면 (예: GUI의
# TkQueueHandler, RotatingFileHandler) 이 모듈의 로그가 일괄 라우팅된다.
logger = logging.getLogger("lecture_pipeline.codex_runner")


DEFAULT_MODEL = "gpt-5.4"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_SERVICE_TIER = "fast"
DEFAULT_TIMEOUT = 600
DEFAULT_WSL_DISTRO = "Ubuntu"
DEFAULT_MAX_CONCURRENT = 12
DEFAULT_STAGGER_INTERVAL = 1.0  # Codex 호출 간 최소 간격(초). 0이면 비활성.

# ── Claude CLI fallback ──
DEFAULT_CLAUDE_MODEL = "claude-opus-4-6"

_EFFORT_MAP: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
}


class CodexRunError(Exception):
    """Codex 작업 실행 실패."""


# ── ContextVars (프로세스 전역) ──
# 현재 스레드의 Codex 작업 우선순위. 낮을수록 높은 우선순위.
# GUI에서 task_id를 설정하며, ContextThreadPoolExecutor를 통해 자식 스레드로 전파.
codex_priority: contextvars.ContextVar[int] = contextvars.ContextVar(
    "codex_priority", default=1000,
)

# Claude Only 모드. True이면 Codex를 skip하고 Claude CLI로만 실행.
claude_only_mode: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "claude_only_mode", default=False,
)

# 현재 task 의 cancel 시그널. `run_codex_task` 가 Claude fallback 진입 전에
# 자동 조회. composite skill 이 fan-out 하는 자식 스레드까지
# ContextThreadPoolExecutor 로 자동 전파되므로 스킬 작성자는 cancel 을 다룰
# 필요 없음. 명시적으로 `run_codex_task(cancel_event=...)` 를 넘기면 그쪽이 우선.
current_cancel_event: contextvars.ContextVar["threading.Event | None"] = (
    contextvars.ContextVar("current_cancel_event", default=None)
)


class PrioritySemaphore:
    """우선순위 기반 세마포어.

    release 시 대기열에서 priority 값이 가장 낮은(= 우선순위 높은) 스레드를
    깨운다. 동일 우선순위 내에서는 FIFO.

    priority는 ``codex_priority`` contextvar에서 자동으로 읽는다.
    """

    def __init__(self, value: int):
        self._capacity = value
        self._available = value
        self._lock = threading.Lock()
        self._counter = 0  # tie-breaker (FIFO within same priority)
        self._waiters: list[tuple[int, int, threading.Event]] = []  # min-heap

    def acquire(self, *, timeout: float | None = None) -> bool:
        priority = codex_priority.get()
        with self._lock:
            if self._available > 0:
                self._available -= 1
                return True
            event = threading.Event()
            self._counter += 1
            heapq.heappush(self._waiters, (priority, self._counter, event))

        got = event.wait(timeout=timeout)
        if got:
            return True
        # timeout — waiter 제거
        with self._lock:
            self._waiters = [
                entry for entry in self._waiters if entry[2] is not event
            ]
            heapq.heapify(self._waiters)
        return False

    def release(self) -> None:
        with self._lock:
            if self._waiters:
                _, _, event = heapq.heappop(self._waiters)
                event.set()
            else:
                self._available += 1

    def stats(self) -> tuple[int, int, int]:
        """(in_flight, capacity, waiters) 스냅샷. GUI status bar 용."""
        with self._lock:
            return (
                self._capacity - self._available,
                self._capacity,
                len(self._waiters),
            )

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


class ContextThreadPoolExecutor(ThreadPoolExecutor):
    """contextvars 컨텍스트를 워커 스레드로 전파하는 ThreadPoolExecutor.

    Python 3.12+ 에서는 표준 TPE도 컨텍스트를 복사하지만, 이 클래스는
    이전 버전에서도 동작을 보장한다.
    """

    def submit(self, fn, /, *args, **kwargs):
        ctx = contextvars.copy_context()
        return super().submit(ctx.run, fn, *args, **kwargs)


SKILL_META_FILES = ("prompt.txt", "skill.py", "config.json", "__pycache__", "*.pyc")

Normalizer = Callable[[list[Path]], dict[str, Path]]


# ---------------------------------------------------------------------------
# 순수 helper 함수 (상태 없음 — 모듈 레벨 유지)
# ---------------------------------------------------------------------------


def _win_to_wsl(path) -> str:
    path = str(path).replace("\\", "/")
    if len(path) >= 2 and path[1] == ":":
        return f"/mnt/{path[0].lower()}{path[2:]}"
    return path


def _write_input(dest: Path, value) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        dest.write_bytes(value)
    elif isinstance(value, str):
        dest.write_text(value, encoding="utf-8")
    elif isinstance(value, Path):
        if not value.exists():
            raise CodexRunError(f"입력 파일 없음: {value}")
        if value.is_dir():
            raise CodexRunError(f"입력이 디렉토리임 (파일 필요): {value}")
        shutil.copy2(value, dest)
    else:
        raise CodexRunError(
            f"지원하지 않는 입력 타입: {type(value).__name__} (name={dest.name})"
        )


def _build_prompt(user_prompt: str, has_scripts: bool,
                  expected_outputs: list[str] | None) -> str:
    lines = [
        "# Task",
        "",
        user_prompt.strip(),
        "",
        "# Environment",
        "",
        "- Input files are in `inputs/`. Treat them as read-only.",
        "- Write all result files to `outputs/`.",
    ]
    if has_scripts:
        lines.append(
            "- Helper scripts are in `scripts/`. "
            "Invoke them with `bash`/`python` as appropriate; "
            "do not assume they are executable."
        )
    if expected_outputs is not None:
        lines.append("")
        lines.append("# Required outputs")
        lines.append("")
        lines.append("You MUST produce at least these files:")
        for name in expected_outputs:
            lines.append(f"- `outputs/{name}`")
        lines.append("")
        lines.append("Additional files in `outputs/` are allowed but not required.")
    return "\n".join(lines) + "\n"


def _collect_outputs(outputs_dir: Path) -> dict[str, bytes]:
    """outputs/ 아래 모든 파일을 {상대경로: bytes}로 수집. 바이너리 안전."""
    result: dict[str, bytes] = {}
    for path in sorted(outputs_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(outputs_dir).as_posix()
        result[rel] = path.read_bytes()
    return result


# ---------------------------------------------------------------------------
# Normalizer factories (상태 없음)
# ---------------------------------------------------------------------------


def identity() -> Normalizer:
    """basename을 그대로 사용. 같은 이름 충돌 시 에러."""
    def fn(paths: list[Path]) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for p in paths:
            if p.name in out:
                raise CodexRunError(
                    f"이름 충돌: {p.name} ({out[p.name]} vs {p})"
                )
            out[p.name] = p
        return out
    return fn


def single(name: str) -> Normalizer:
    """입력 1개만 허용. 주어진 `name`으로 리네임."""
    def fn(paths: list[Path]) -> dict[str, Path]:
        if len(paths) != 1:
            raise CodexRunError(
                f"single normalizer는 입력 1개만 허용 (받음: {len(paths)})"
            )
        return {name: paths[0]}
    return fn


def enumerated(prefix: str = "input", ext: str | None = None,
               digits: int = 3) -> Normalizer:
    """정렬 후 `{prefix}_001{ext}, {prefix}_002{ext}, ...`로 번호 매김.

    `ext`가 None이면 각 파일의 원래 확장자를 유지.
    """
    def fn(paths: list[Path]) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for i, p in enumerate(sorted(paths, key=lambda x: x.name), start=1):
            e = ext if ext is not None else p.suffix
            name = f"{prefix}_{i:0{digits}d}{e}"
            out[name] = p
        return out
    return fn


def by_extension(mapping: dict[str, str]) -> Normalizer:
    """확장자별로 고정 이름 할당. 미등록 확장자나 중복 할당 시 에러."""
    def fn(paths: list[Path]) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for p in paths:
            if p.suffix not in mapping:
                raise CodexRunError(
                    f"매핑에 없는 확장자: {p.suffix} (파일: {p})"
                )
            target = mapping[p.suffix]
            if target in out:
                raise CodexRunError(
                    f"역할 충돌: {target} ({out[target]} vs {p})"
                )
            out[target] = p
        return out
    return fn


def by_regex(rules: list[tuple[str, str]]) -> Normalizer:
    """파일명 정규식 매칭으로 역할 이름 할당.

    `rules`는 `(pattern, target_name)` 리스트이며 순서대로 첫 매칭을 사용.
    매칭 실패 또는 target_name 중복 시 에러.
    """
    compiled = [(re.compile(pat), name) for pat, name in rules]

    def fn(paths: list[Path]) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for p in paths:
            target: str | None = None
            for pat, name in compiled:
                if pat.search(p.name):
                    target = name
                    break
            if target is None:
                raise CodexRunError(f"어떤 규칙에도 매칭 안됨: {p.name}")
            if target in out:
                raise CodexRunError(
                    f"역할 충돌: {target} ({out[target]} vs {p})"
                )
            out[target] = p
        return out
    return fn


@dataclass
class Skill:
    """skill 폴더에서 로드한 설정 묶음."""
    prompt: str
    dir: Path
    normalize: Normalizer
    expected_outputs: list[str] | None = None
    config: dict = field(default_factory=dict)
    # Composite skill: skill.py가 run() 함수를 노출하면 여기에 저장됨.
    # run_skill은 skill.run이 None이 아니면 Codex 경로 대신 이 함수를 호출.
    run: Callable | None = None


def _skill_signature(skill_dir: Path) -> tuple:
    """캐시 무효화 기준. skill 내용에 영향을 주는 파일들의 (경로, mtime, size).

    - 상단의 `prompt.txt`, `skill.py`, `config.json`
    - `_lib/**/*.py` (composite skill 의 핵심 코드)
    그 외 보조 스크립트는 Codex workdir 의 `scripts/` 로 복사될 때 매번 새로
    수집되므로 `Skill` 객체 자체에는 영향 없음 → 캐시 키에서 제외.
    """
    parts: list[tuple] = []
    for name in ("prompt.txt", "skill.py", "config.json"):
        p = skill_dir / name
        try:
            st = p.stat()
        except OSError:
            continue
        parts.append((name, st.st_mtime_ns, st.st_size))
    lib_dir = skill_dir / "_lib"
    if lib_dir.is_dir():
        for p in sorted(lib_dir.rglob("*.py")):
            try:
                st = p.stat()
            except OSError:
                continue
            parts.append(
                (p.relative_to(skill_dir).as_posix(), st.st_mtime_ns, st.st_size)
            )
    return tuple(parts)


# ---------------------------------------------------------------------------
# Retry-on-shrinkage helpers (상태 없음)
# ---------------------------------------------------------------------------


def _total_input_bytes(paths: list[Path]) -> int:
    total = 0
    for p in paths:
        if not p.exists():
            raise CodexRunError(f"입력 파일 없음: {p}")
        total += p.stat().st_size
    return total


def _total_output_bytes(outputs: dict[str, bytes]) -> int:
    return sum(len(v) for v in outputs.values())


def _match_retry_rule(ratio: float, rules: list[dict]) -> int:
    """첫 매칭 규칙의 extra_attempts 반환. 못 찾으면 0.

    규칙 형태: `{"max_ratio": float, "extra_attempts": int}`
    `ratio <= max_ratio`이면 매칭. 규칙은 순서대로 체크, 첫 매칭 승.
    키 누락이나 타입 불일치 규칙은 조용히 스킵.
    """
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        mr = rule.get("max_ratio")
        ea = rule.get("extra_attempts")
        if not isinstance(mr, (int, float)):
            continue
        if not isinstance(ea, int):
            continue
        if ratio <= mr:
            return ea
    return 0


def _emit(log_callback: Callable[[str], None] | None, msg: str) -> None:
    """재시도/진행 메시지를 logger와 호출자 콜백 양쪽으로 방출."""
    logger.info(msg)
    if log_callback is None:
        return
    try:
        log_callback(msg)
    except Exception:
        pass  # 깨진 콜백이 재시도 본류를 망치지 않게


# ---------------------------------------------------------------------------
# CodexRunner — mutable 상태 캡슐화
# ---------------------------------------------------------------------------


class CodexRunner:
    """Codex/Claude 작업 실행기. mutable 상태는 인스턴스 스코프에 있다.

    일반적으로는 모듈 레벨 `_default` 인스턴스를 통해 간접적으로 쓰지만
    (``from codex_runner import run_codex_task`` 등), 테스트 격리나 다중 러너
    시나리오에서는 직접 인스턴스화 가능.

    상태:
    - `semaphore`: 전역 Codex subprocess 동시 실행 제한.
    - `stagger_interval` + `_next_allowed_start`: 호출 간 최소 간격 (burst 방지).
    - `_active_procs`: 실행 중 Popen 집합 (terminate용).
    - `_cancel_events`: 등록된 per-task cancel Event 집합.
    - `_skill_cache`: load_skill 결과 캐시.
    """

    def __init__(
        self,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        stagger_interval: float = DEFAULT_STAGGER_INTERVAL,
    ):
        self.semaphore = PrioritySemaphore(max_concurrent)
        self._semaphore_lock = threading.Lock()

        self.stagger_interval = max(0.0, stagger_interval)
        self._stagger_lock = threading.Lock()
        self._next_allowed_start: float = 0.0

        self._active_procs: "set[subprocess.Popen]" = set()
        self._proc_lock = threading.Lock()

        self._cancel_events: "set[threading.Event]" = set()
        self._cancel_lock = threading.Lock()

        self._skill_cache: "dict[str, tuple[tuple, Skill]]" = {}
        self._skill_cache_lock = threading.Lock()

        # Codex 바이너리 (네이티브). 없으면 None — run_codex_task 호출 시 raise.
        self._codex_binary: Path | None = self._resolve_codex_binary()
        self._venv_manager = VenvManager()

        # 호스트 사전 조건 체크 — 경고만, 실제 실행 시점에 에러.
        self._check_host_prereqs()

    # ── 바이너리 resolver & 환경 점검 ──

    @staticmethod
    def _resolve_codex_binary() -> Path | None:
        """Codex 바이너리 경로를 찾는다.

        우선순위:
        1. `LECTURE_CODEX_PATH` 환경변수 (절대경로)
        2. Windows: `codex.cmd` → `codex.exe`
        3. POSIX: `codex`
        """
        override = os.environ.get("LECTURE_CODEX_PATH")
        if override:
            p = Path(override)
            if p.is_file():
                return p
            logger.warning("LECTURE_CODEX_PATH 존재하지 않음: %s", override)

        candidates = (
            ("codex.cmd", "codex.exe") if sys.platform == "win32" else ("codex",)
        )
        for name in candidates:
            found = shutil.which(name)
            if found:
                return Path(found)
        return None

    def _check_host_prereqs(self) -> None:
        """git, codex 존재 여부 경고. 실패해도 인스턴스 생성은 성공."""
        if shutil.which("git") is None:
            logger.warning(
                "git CLI 를 찾을 수 없음 — Codex --full-auto 는 git 필요"
            )
        if self._codex_binary is None:
            logger.warning(
                "Codex CLI 를 찾을 수 없음 — `npm i -g @openai/codex` 설치 또는 "
                "LECTURE_CODEX_PATH 환경변수 설정 필요"
            )
        else:
            logger.debug("Codex 바이너리: %s", self._codex_binary)

    # ── 설정 ──

    def set_max_concurrent(self, n: int) -> None:
        """Codex 동시 실행 한계를 재설정. 진행 중인 작업에는 영향 없음."""
        if n < 1:
            raise ValueError("max_concurrent는 1 이상이어야 함")
        with self._semaphore_lock:
            self.semaphore = PrioritySemaphore(n)

    def set_stagger_interval(self, seconds: float) -> None:
        """Codex 호출 간 최소 간격(초) 변경."""
        self.stagger_interval = max(0.0, seconds)

    def stats(self) -> dict[str, int]:
        """런타임 가동 스냅샷 (GUI status bar 용).

        - ``active_procs``: 현재 살아있는 subprocess 수 (Codex + Goose 공유).
        - ``in_flight`` / ``capacity`` / ``waiters``: semaphore 점유/한도/대기 수.
          ``active_procs`` 와 ``in_flight`` 는 스태거 구간 등으로 순간적으로
          다를 수 있지만 대체로 같은 방향이다.
        """
        with self._semaphore_lock:
            sem = self.semaphore
        in_flight, capacity, waiters = sem.stats()
        with self._proc_lock:
            active = len(self._active_procs)
        return {
            "active_procs": active,
            "in_flight": in_flight,
            "capacity": capacity,
            "waiters": waiters,
        }

    def _stagger_wait(self) -> None:
        """다음 허용 시점까지 대기한 뒤 자신의 슬롯을 예약."""
        if self.stagger_interval <= 0:
            return
        with self._stagger_lock:
            now = time.monotonic()
            wait_until = max(now, self._next_allowed_start)
            self._next_allowed_start = wait_until + self.stagger_interval
        sleep_for = wait_until - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)

    # ── subprocess 추적 / cancel ──

    def _register_proc(self, p: subprocess.Popen) -> None:
        with self._proc_lock:
            self._active_procs.add(p)

    def _unregister_proc(self, p: subprocess.Popen) -> None:
        with self._proc_lock:
            self._active_procs.discard(p)

    def register_cancel_event(self, event: threading.Event) -> None:
        """`terminate_all_active()` 호출 시 함께 set 될 Event 를 등록."""
        with self._cancel_lock:
            self._cancel_events.add(event)

    def unregister_cancel_event(self, event: threading.Event) -> None:
        with self._cancel_lock:
            self._cancel_events.discard(event)

    def terminate_all_active(self) -> int:
        """실행 중인 모든 Codex subprocess 를 강제 종료.

        Windows: ``taskkill /F /T`` 로 프로세스 트리 전체 kill (wsl.exe + 자식).
        그 외: ``Popen.terminate()``.

        등록된 per-task cancel Event 들도 모두 set 해서 Claude fallback 진입을
        차단한다 (각 task 가 자기 Event 만 참조하므로 race 없음).

        Returns:
            kill 시도한 프로세스 수.
        """
        with self._cancel_lock:
            events = list(self._cancel_events)
        for ev in events:
            ev.set()
        with self._proc_lock:
            procs = list(self._active_procs)
        n = 0
        for p in procs:
            if p.poll() is not None:
                continue  # already dead
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                        capture_output=True,
                        timeout=5,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                else:
                    p.terminate()
                n += 1
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        return n

    def _run_tracked(
        self,
        cmd: list[str],
        timeout: int,
        kind: str,
        cwd: str | None = None,
        env: dict | None = None,
    ) -> tuple[int, str, str]:
        """Tracked Popen + communicate 헬퍼.

        - encoding="utf-8", errors="replace" 강제 (cp949 이슈 방지)
        - 실행 중 active_procs 에 등록 → terminate_all_active 로 kill 가능
        - 타임아웃 시 프로세스 kill 후 CodexRunError raise
        - env: 미지정 시 현재 프로세스 환경 상속 (Popen 기본 동작).

        Returns:
            (returncode, stdout, stderr)
        """
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise CodexRunError(
                f"{kind}: 실행 파일을 찾을 수 없음 ({cmd[0]!r})"
            ) from exc

        self._register_proc(proc)
        try:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.communicate()
                except Exception:
                    pass
                raise CodexRunError(f"{kind} 타임아웃 ({timeout}s)")
        finally:
            self._unregister_proc(proc)

        return proc.returncode, stdout or "", stderr or ""

    # ── Claude CLI fallback ──

    def _run_claude_task(
        self,
        workdir: Path,
        outputs_dir: Path,
        output_schema: dict | None,
        claude_model: str,
        reasoning_effort: str,
        timeout: int,
        wsl_distro: str = DEFAULT_WSL_DISTRO,
    ) -> None:
        """Claude CLI(WSL)로 이미 세팅된 workdir의 작업을 실행한다.

        Codex fallback / Claude Only 용. workdir에 inputs/, outputs/, prompt.txt 가
        이미 존재하는 상태에서 WSL 내의 Claude CLI를 호출해 동일한 작업을 수행시킨다.

        Codex와 동일하게 WSL sandbox에서 실행하므로, Claude가 tmpdir 밖의 원본
        파일을 직접 수정할 위험이 없다.

        Raises:
            CodexRunError: Claude 실행 실패 또는 타임아웃.
        """
        effort = _EFFORT_MAP.get(reasoning_effort, "high")
        wsl_dir = _win_to_wsl(workdir)

        # 프롬프트 구성
        task_instruction = "Read prompt.txt and execute the task described there."
        req_txt = workdir / "scripts" / "requirements.txt"
        if req_txt.exists():
            task_instruction = (
                "First, run `pip install -r scripts/requirements.txt` to install "
                "dependencies. Then read prompt.txt and execute the task described there."
            )

        # WSL 내에서 실행할 인자를 개별 요소로 구성 → shell escaping 문제 회피
        # wsl --cd + -- 로 직접 인자 전달 (bash -c 문자열 조립 X)
        wsl_cmd: list[str] = [
            "wsl", "-d", wsl_distro,
            "--cd", wsl_dir,
            "--",
            "claude",
            "-p", task_instruction,
            "--model", claude_model,
            "--effort", effort,
            "--dangerously-skip-permissions",
            "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep",
            "--output-format", "json",
            "--no-session-persistence",
            "--max-turns", "50",
        ]
        if output_schema is not None:
            schema_json = json.dumps(output_schema, ensure_ascii=False)
            wsl_cmd.extend(["--json-schema", schema_json])

        self._stagger_wait()
        with self.semaphore:
            rc, stdout, stderr = self._run_tracked(
                cmd=wsl_cmd,
                timeout=timeout,
                kind="Claude fallback",
            )

        if rc != 0:
            tail = (stderr or stdout or "")[-500:]
            raise CodexRunError(f"Claude fallback 실패 (exit={rc}):\n{tail}")

        # stdout JSON → _last_message.txt (기존 single-output fallback 경로 호환)
        if stdout.strip():
            try:
                result_json = json.loads(stdout)
                last_msg = result_json.get("result", "")
                if last_msg:
                    (workdir / "_last_message.txt").write_text(
                        last_msg, encoding="utf-8",
                    )
            except (json.JSONDecodeError, AttributeError):
                (workdir / "_last_message.txt").write_text(
                    stdout, encoding="utf-8",
                )

    # ── Codex 본 실행 ──

    def run_codex_task(
        self,
        prompt: str,
        inputs: dict[str, str | bytes | Path],
        *,
        scripts_dir: str | Path | None = None,
        scripts_ignore: tuple[str, ...] = (),
        expected_outputs: list[str] | None = None,
        allow_extra_outputs: bool = False,
        output_schema: dict | None = None,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        service_tier: str = DEFAULT_SERVICE_TIER,
        timeout: int = DEFAULT_TIMEOUT,
        wsl_distro: str = DEFAULT_WSL_DISTRO,
        claude_fallback: bool = True,
        claude_only: bool = False,
        claude_model: str = DEFAULT_CLAUDE_MODEL,
        cancel_event: threading.Event | None = None,
        venv_bin: Path | None = None,
        network_access: bool = False,
    ) -> dict[str, bytes]:
        """Codex CLI로 prompt + inputs -> outputs 작업을 실행한다.

        Args:
            prompt: Codex에 전달할 작업 지시(자연어). 파일 경로가 아닌 문자열.
            inputs: {파일명: 내용 또는 Path}.
                - `str`/`bytes`  -> 해당 내용을 `inputs/<파일명>`에 기록
                - `Path`          -> 해당 파일을 `inputs/<파일명>`으로 복사
                파일명은 workdir 기준 상대경로여야 하며 `..`는 금지.
            scripts_dir: 보조 스크립트 디렉토리(옵션). 하위 트리 전체가
                workdir의 `scripts/`로 복사되어 Codex가 도구로 사용할 수 있다.
                또한 `scripts/bootstrap.sh`가 있으면 Codex 호출 직전에 실행되고,
                stdout 마지막 라인을 venv bin 경로로 간주해 PATH에 prepend한다.
            expected_outputs: 엄격 모드에서 반드시 있어야 하는 출력 파일명 리스트.
                `None`이면 느슨 모드 (outputs/에 생긴 모든 파일을 그대로 반환).
                빈 리스트는 허용되지 않는다 (느슨 모드를 원하면 `None` 사용).
            allow_extra_outputs: 엄격 모드일 때 `expected_outputs` 외 파일도
                함께 반환할지. True 면 와일드카드 출력(예: crops/*.png)을 쓰는
                composite skill 에 적합. False(기본)는 expected 만 반환해 노이즈
                제거.
            output_schema: JSON schema(dict). None이 아니면 workdir에 `_schema.json`
                파일로 저장하고 `codex exec --output-schema _schema.json` 플래그를
                추가해 LLM의 최종 응답을 schema에 맞게 강제.
            model: Codex 모델명. `-c model=...`로 전달.
            reasoning_effort: `low` | `medium` | `high`.
            service_tier: `"default"` | `"fast"`. fast는 1.5x 속도, 2x 크레딧.
            timeout: Codex 서브프로세스 타임아웃(초).
            wsl_distro: WSL 배포판 이름.
            cancel_event: 명시적 cancel Event. None 이면 `current_cancel_event`
                ContextVar 에서 읽음.

        Returns:
            {출력 파일 상대경로(posix): 파일 바이트}. 바이너리 안전.
            엄격 모드에서는 `expected_outputs`에 명시한 파일만 담아 반환한다.

        Raises:
            CodexRunError: 입력 검증 실패, bootstrap/Codex 실행/타임아웃 실패,
                또는 엄격 모드에서 기대 출력 누락.
        """
        if not prompt or not prompt.strip():
            raise CodexRunError("prompt가 비어있음")
        if not inputs:
            raise CodexRunError("inputs가 비어있음")
        if expected_outputs is not None and len(expected_outputs) == 0:
            raise CodexRunError(
                "expected_outputs가 빈 리스트임. 느슨 모드를 원하면 None을 전달."
            )

        for name in inputs:
            p = Path(name)
            if p.is_absolute() or ".." in p.parts:
                raise CodexRunError(
                    f"입력 이름은 workdir 내 상대경로여야 함: {name!r}"
                )
        if expected_outputs is not None:
            for name in expected_outputs:
                p = Path(name)
                if p.is_absolute() or ".." in p.parts:
                    raise CodexRunError(
                        f"expected_outputs 이름은 상대경로여야 함: {name!r}"
                    )

        strict = expected_outputs is not None

        with tempfile.TemporaryDirectory(prefix="codex_task_") as tmpdir:
            workdir = Path(tmpdir)
            inputs_dir = workdir / "inputs"
            outputs_dir = workdir / "outputs"
            inputs_dir.mkdir()
            outputs_dir.mkdir()

            for name, value in inputs.items():
                _write_input(inputs_dir / name, value)

            has_scripts = False
            if scripts_dir is not None:
                src = Path(scripts_dir)
                if not src.is_dir():
                    raise CodexRunError(f"scripts_dir가 디렉토리가 아님: {src}")
                ignore = (
                    shutil.ignore_patterns(*scripts_ignore) if scripts_ignore else None
                )
                shutil.copytree(src, workdir / "scripts", ignore=ignore)
                # 필터 후 스크립트 폴더가 비어 있으면 정리해 프롬프트 노이즈를 막는다.
                if any((workdir / "scripts").iterdir()):
                    has_scripts = True
                else:
                    shutil.rmtree(workdir / "scripts")

            full_prompt = _build_prompt(prompt, has_scripts, expected_outputs)
            (workdir / "prompt.txt").write_text(full_prompt, encoding="utf-8")

            # output_schema가 있으면 workdir에 저장해 --output-schema로 전달.
            if output_schema is not None:
                (workdir / "_schema.json").write_text(
                    json.dumps(output_schema, ensure_ascii=False),
                    encoding="utf-8",
                )

            # Codex의 마지막 메시지를 파일로 받아두면 "inline 답변만 하고 file
            # write를 안 한" 실패 모드에서 fallback으로 사용 가능.
            last_msg_file = "_last_message.txt"

            # Codex 호출용 env: venv_bin 이 주어졌으면 PATH 에 prepend 해 에이전트가
            # 실행하는 python 이 skill 전용 venv 를 쓰도록. 현재 프로세스 env 상속.
            codex_env = os.environ.copy()
            if venv_bin is not None:
                codex_env["PATH"] = (
                    str(venv_bin) + os.pathsep + codex_env.get("PATH", "")
                )

            # claude_only: 파라미터 명시 > ContextVar (composite skill 전파용)
            effective_claude_only = claude_only or claude_only_mode.get()
            stdout = ""
            stderr = ""
            if effective_claude_only:
                # Codex skip — Claude CLI로 직접 실행 (WSL 유지)
                self._run_claude_task(
                    workdir=workdir,
                    outputs_dir=outputs_dir,
                    output_schema=output_schema,
                    claude_model=claude_model,
                    reasoning_effort=reasoning_effort,
                    timeout=timeout,
                    wsl_distro=wsl_distro,
                )
            else:
                # 네이티브 Codex 경로. WSL 경유 없음.
                if self._codex_binary is None:
                    raise CodexRunError(
                        "Codex CLI 미설치. `npm i -g @openai/codex` 설치 또는 "
                        "LECTURE_CODEX_PATH 환경변수로 절대경로 지정."
                    )

                # Codex --full-auto 는 작업 디렉토리가 git repo 일 것을 요구.
                try:
                    subprocess.run(
                        ["git", "init", "-q"],
                        cwd=str(workdir),
                        check=True,
                        capture_output=True,
                        creationflags=getattr(
                            subprocess, "CREATE_NO_WINDOW", 0
                        ),
                    )
                except FileNotFoundError as exc:
                    raise CodexRunError(
                        "git CLI 를 찾을 수 없음 — Codex --full-auto 는 git 필요"
                    ) from exc
                except subprocess.CalledProcessError as exc:
                    tail = (exc.stderr or exc.stdout or b"")[-500:]
                    raise CodexRunError(
                        f"git init 실패 (exit={exc.returncode}):\n"
                        f"{tail.decode('utf-8', errors='replace')}"
                    ) from exc

                argv: list[str] = [str(self._codex_binary), "exec"]
                argv.extend(["-c", f"model={model}"])
                argv.extend(["-c", f"model_reasoning_effort={reasoning_effort}"])
                if service_tier and service_tier != "default":
                    argv.extend(["-c", f"service_tier={service_tier}"])
                argv.append("--full-auto")  # 샌드박스 유지
                if network_access:
                    # workspace-write 샌드박스의 기본은 네트워크 차단. 스킬이
                    # 외부 API (예: Gemini) 를 호출해야 하면 이 플래그를 켜야 함.
                    argv.extend([
                        "-c", "sandbox_workspace_write.network_access=true",
                    ])
                argv.extend(["-o", last_msg_file])
                if output_schema is not None:
                    argv.extend(["--output-schema", "_schema.json"])
                argv.append(
                    "Read prompt.txt and execute the task described there."
                )

                self._stagger_wait()
                codex_error: CodexRunError | None = None
                with self.semaphore:
                    rc, stdout, stderr = self._run_tracked(
                        cmd=argv,
                        timeout=timeout,
                        kind="Codex 실행",
                        cwd=str(workdir),
                        env=codex_env,
                    )

                if rc != 0:
                    tail = (stderr or stdout or "")[-500:]
                    codex_error = CodexRunError(
                        f"Codex 실행 실패 (exit={rc}):\n{tail}"
                    )

                if codex_error is not None:
                    # 명시 인자 > ContextVar. 둘 다 없으면 cancel 추적 비활성.
                    effective_cancel = (
                        cancel_event if cancel_event is not None
                        else current_cancel_event.get()
                    )
                    cancelled = (
                        effective_cancel is not None and effective_cancel.is_set()
                    )
                    if not claude_fallback:
                        logger.warning("Codex 실패, fallback 비활성 → 전파")
                        raise codex_error
                    if cancelled:
                        logger.info("사용자 취소 감지 — Claude fallback skip")
                        raise codex_error
                    logger.warning(
                        "Codex 실패 → Claude fallback 시도 (%s)",
                        str(codex_error).splitlines()[0][:120],
                    )
                    # Claude CLI fallback — 동일 workdir 재사용
                    try:
                        self._run_claude_task(
                            workdir=workdir,
                            outputs_dir=outputs_dir,
                            output_schema=output_schema,
                            claude_model=claude_model,
                            reasoning_effort=reasoning_effort,
                            timeout=timeout,
                            wsl_distro=wsl_distro,
                        )
                    except CodexRunError as claude_exc:
                        raise CodexRunError(
                            f"Codex 실패 후 Claude fallback도 실패.\n"
                            f"--- Codex ---\n{codex_error}\n"
                            f"--- Claude ---\n{claude_exc}"
                        ) from claude_exc

            collected = _collect_outputs(outputs_dir)

            # Fallback: Codex가 outputs/에 아무것도 안 썼지만 마지막 메시지가 있으면
            # 그 내용을 기대 출력 파일로 취급. 단일 expected output + 마지막 메시지
            # 비어있지 않을 때만 동작. 이는 "inline 답변만 하고 file write 건너뜀"
            # 실패 모드의 복구를 위함.
            last_msg_path = workdir / last_msg_file
            if strict and expected_outputs and len(expected_outputs) == 1 and last_msg_path.exists():
                only_name = expected_outputs[0]
                if only_name not in collected:
                    last_msg_bytes = last_msg_path.read_bytes()
                    if last_msg_bytes.strip():
                        collected[only_name] = last_msg_bytes

            if strict:
                missing = [n for n in expected_outputs if n not in collected]
                if missing:
                    extras = sorted(set(collected) - set(expected_outputs))
                    msg = f"기대 출력 누락: {missing}"
                    if extras:
                        msg += f" / 생성된 파일: {extras}"
                    # 진단: Codex stdout/stderr + last_message tail을 에러에 포함
                    diag_parts = []
                    if stdout:
                        diag_parts.append(f"stdout tail:\n{stdout[-600:]}")
                    if stderr:
                        diag_parts.append(f"stderr tail:\n{stderr[-600:]}")
                    if last_msg_path.exists():
                        try:
                            lm = last_msg_path.read_text(encoding="utf-8", errors="replace")
                            diag_parts.append(f"last_message ({len(lm)} chars):\n{lm[-600:]}")
                        except Exception:
                            pass
                    if diag_parts:
                        msg += "\n---\n" + "\n---\n".join(diag_parts)
                    raise CodexRunError(msg)
                if allow_extra_outputs:
                    return collected
                return {n: collected[n] for n in expected_outputs}

            if not collected:
                raise CodexRunError("outputs/에 아무 파일도 없음")
            return collected

    # ── Skill loader (캐시 포함) ──

    def clear_skill_cache(self) -> None:
        """캐시를 전부 비운다 (테스트/디버그 용)."""
        with self._skill_cache_lock:
            self._skill_cache.clear()

    def load_skill(self, skill_dir: str | Path) -> Skill:
        """skill 폴더에서 `prompt.txt` (+ 선택적으로 `skill.py`, `config.json`)를 로드한다.

        skill 폴더 구조:
            <skill_dir>/
            ├── prompt.txt        (필수) 작업 지시
            ├── skill.py          (옵션) normalize, expected_outputs 선언
            ├── config.json       (옵션) model, reasoning_effort, timeout
            └── <기타 파일들>      (옵션) 보조 스크립트 — workdir의 scripts/로 복사

        `skill.py`가 노출할 수 있는 심볼:
        - `normalize`: `Normalizer` 함수 (기본: `identity()`)
        - `expected_outputs`: 기대 출력 리스트 또는 None (기본: None = 느슨 모드)
        - `run`: composite skill인 경우 `(skill_dir, input_paths, log_callback)` 받아
          `dict[str, bytes]` 반환하는 함수. 있으면 Codex 대신 이 함수 실행.

        `config.json`에 들어갈 수 있는 키:
        - `model`, `reasoning_effort`, `timeout`, `output_dir`, `batch`,
          `retry_if_shrunk`, 그리고 각 skill이 정의한 커스텀 키

        skill 폴더 내부에 `_lib/` 같은 서브패키지가 있으면 `skill.py`가 이들로부터
        import할 수 있도록 skill_dir을 임시로 `sys.path`에 prepend한다.

        결과는 파일 mtime 기반으로 캐시된다. 원본 파일 수정 시 자동으로 다시 로드.
        """
        skill_dir = Path(skill_dir).resolve()
        if not skill_dir.is_dir():
            raise CodexRunError(f"skill 디렉토리 없음: {skill_dir}")

        key = str(skill_dir)
        sig = _skill_signature(skill_dir)
        with self._skill_cache_lock:
            cached = self._skill_cache.get(key)
            if cached is not None and cached[0] == sig:
                return cached[1]
            # cache miss — 실제 로드는 락 안에서. 동시에 다른 skill 이 로드되어도
            # `sys.path` 조작이 겹치지 않도록 직렬화. skill 로드는 밀리초 수준이라
            # 직렬화 비용 무시 가능.
            skill = self._load_skill_uncached(skill_dir)
            self._skill_cache[key] = (sig, skill)
            return skill

    def _load_skill_uncached(self, skill_dir: Path) -> Skill:
        """캐시를 우회한 실제 로드. `load_skill` 만 호출해야 한다."""
        prompt_file = skill_dir / "prompt.txt"
        if not prompt_file.exists():
            raise CodexRunError(f"prompt.txt 없음: {prompt_file}")
        prompt = prompt_file.read_text(encoding="utf-8")

        config: dict = {}
        config_file = skill_dir / "config.json"
        if config_file.exists():
            try:
                config = json.loads(config_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise CodexRunError(
                    f"config.json 파싱 실패: {config_file}: {exc}"
                ) from exc
            if not isinstance(config, dict):
                raise CodexRunError(f"config.json은 object여야 함: {config_file}")

        normalize_fn: Normalizer = identity()
        expected: list[str] | None = None
        run_fn: Callable | None = None

        skill_py = skill_dir / "skill.py"
        if skill_py.exists():
            mod_name = f"_loaded_skill_{skill_dir.name}"
            spec = importlib.util.spec_from_file_location(mod_name, skill_py)
            if spec is None or spec.loader is None:
                raise CodexRunError(f"skill.py 로드 실패: {skill_py}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            # skill.py가 `from _lib.foo import bar` 같은 상대 import를 쓸 수 있도록
            # skill_dir을 임시로 sys.path에 prepend. 로드 끝나면 원복.
            sys_path_added = str(skill_dir) not in sys.path
            if sys_path_added:
                sys.path.insert(0, str(skill_dir))
            # exec 이전 sys.modules 키 snapshot — skill.py 가 만든 `_lib.*`
            # 엔트리를 로드 후 정리해서, 서로 다른 skill 이 각각의 `_lib/`
            # 서브패키지를 동일 이름으로 두더라도 상호 간섭 없이 로드되도록.
            pre_modules = set(sys.modules.keys())
            try:
                spec.loader.exec_module(mod)
            except Exception as exc:
                raise CodexRunError(
                    f"skill.py 실행 실패: {skill_py}: {exc}"
                ) from exc
            finally:
                if sys_path_added:
                    try:
                        sys.path.remove(str(skill_dir))
                    except ValueError:
                        pass
                # 이 skill 이 새로 등록한 `_lib*` 모듈만 제거. 다른 skill 의
                # 모듈(pre_modules 에 있던 것)과 skill.py 래퍼(`_loaded_skill_*`)는
                # 그대로 둔다.
                for key in list(sys.modules.keys()):
                    if key in pre_modules:
                        continue
                    if key == "_lib" or key.startswith("_lib."):
                        del sys.modules[key]

            if hasattr(mod, "normalize"):
                normalize_fn = mod.normalize
            if hasattr(mod, "expected_outputs"):
                expected = mod.expected_outputs
            if hasattr(mod, "run") and callable(mod.run):
                run_fn = mod.run

        return Skill(
            prompt=prompt,
            dir=skill_dir,
            normalize=normalize_fn,
            expected_outputs=expected,
            config=config,
            run=run_fn,
        )

    # ── High-level skill 실행 ──

    def run_skill(
        self,
        skill_dir: str | Path,
        inputs: list[str | Path],
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        service_tier: str | None = None,
        timeout: int | None = None,
        wsl_distro: str = DEFAULT_WSL_DISTRO,
        claude_fallback: bool | None = None,
        claude_only: bool | None = None,
        claude_model: str | None = None,
        log_callback: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, bytes]:
        """skill을 로드해 주어진 입력 파일들에 대해 Codex 작업을 실행한다.

        우선순위: 호출자 인자 > `config.json` > 라이브러리 기본값.

        log_callback: 진행 상황을 문자열로 받는 콜백 (옵션). 스레드 안전해야
            함 — 콜백은 워커 스레드에서 호출될 수 있음. 예외는 내부적으로
            swallow되어 재시도 본류에 영향 없음.

        cancel_event: 명시적으로 넘기면 ContextVar `current_cancel_event` 에
            세팅되어 composite skill 의 fan-out 호출까지 전파. None 이면 호출자
            컨텍스트의 ContextVar 값을 그대로 사용.

        Retry-on-shrinkage:
            `config.json`의 `retry_if_shrunk` 키로 설정. 출력/입력 비율이
            임계 미만이면 extra_attempts 만큼 추가 실행 후 가장 큰 출력 채택.
        """
        skill_dir = Path(skill_dir).resolve()
        skill = self.load_skill(skill_dir)

        # 명시적으로 cancel_event가 넘어오면 ContextVar에 반영. composite skill의
        # 내부 fan-out 과 run_codex_task가 ContextVar로 자동 참조하므로 스킬
        # 작성자는 cancel_event를 시그니처에 추가할 필요가 없다.
        if cancel_event is not None:
            current_cancel_event.set(cancel_event)

        # Composite skill: skill.py가 run() 함수를 노출하면 Codex 경로 건너뛰고 그쪽 위임.
        # normalize/scripts_dir/expected_outputs/retry_if_shrunk는 composite가 자체 관리.
        if skill.run is not None:
            input_paths_composite = [Path(p) for p in inputs]
            return skill.run(
                skill_dir=skill_dir,
                input_paths=input_paths_composite,
                log_callback=log_callback,
            )

        cfg = skill.config
        final_model = model if model is not None else cfg.get("model", DEFAULT_MODEL)
        final_effort = (
            reasoning_effort if reasoning_effort is not None
            else cfg.get("reasoning_effort", DEFAULT_REASONING_EFFORT)
        )
        final_tier = (
            service_tier if service_tier is not None
            else cfg.get("service_tier", DEFAULT_SERVICE_TIER)
        )
        final_timeout = (
            timeout if timeout is not None
            else cfg.get("timeout", DEFAULT_TIMEOUT)
        )
        final_claude_fallback = (
            claude_fallback if claude_fallback is not None
            else cfg.get("claude_fallback", True)
        )
        final_claude_only = (
            claude_only if claude_only is not None
            else cfg.get("claude_only", False)
        )
        final_claude_model = (
            claude_model if claude_model is not None
            else cfg.get("claude_model", DEFAULT_CLAUDE_MODEL)
        )

        input_paths = [Path(p) for p in inputs]
        inputs_dict = skill.normalize(input_paths)

        non_meta = [
            p for p in skill_dir.iterdir()
            if p.name not in ("prompt.txt", "skill.py", "config.json", "__pycache__")
            and not p.name.endswith(".pyc")
        ]
        scripts_dir = skill_dir if non_meta else None

        # 스킬이 scripts/requirements.txt 를 선언했으면 VenvManager 가 venv 를
        # 보장하고 bin/Scripts 경로를 반환. 없으면 None (venv 불필요).
        venv_bin = self._venv_manager.ensure(skill_dir)

        # 백엔드 선택: "codex" (기본) / "goose"
        backend = str(cfg.get("backend", "codex")).lower()

        def _single_attempt() -> dict[str, bytes]:
            if backend == "goose":
                # 지연 import — circular 방지 (goose_runner 가 codex_runner 를 import)
                from goose_runner import run_goose_task
                return run_goose_task(
                    prompt=skill.prompt,
                    inputs=inputs_dict,
                    scripts_dir=scripts_dir,
                    scripts_ignore=SKILL_META_FILES,
                    expected_outputs=skill.expected_outputs,
                    provider=cfg.get("goose_provider"),
                    model=cfg.get("goose_model"),
                    max_turns=int(cfg.get("goose_max_turns", 30)),
                    max_tool_repetitions=int(
                        cfg.get("goose_max_tool_repetitions", 3)
                    ),
                    timeout=final_timeout,
                    cancel_event=cancel_event,
                    venv_bin=venv_bin,
                )
            # 기본: codex 경로 (Claude fallback 포함)
            return self.run_codex_task(
                prompt=skill.prompt,
                inputs=inputs_dict,
                scripts_dir=scripts_dir,
                scripts_ignore=SKILL_META_FILES,
                expected_outputs=skill.expected_outputs,
                model=final_model,
                reasoning_effort=final_effort,
                service_tier=final_tier,
                timeout=final_timeout,
                wsl_distro=wsl_distro,
                claude_fallback=final_claude_fallback,
                claude_only=final_claude_only,
                claude_model=final_claude_model,
                cancel_event=cancel_event,
                venv_bin=venv_bin,
            )

        retry_raw = cfg.get("retry_if_shrunk")
        retry_rules: list[dict] = retry_raw if isinstance(retry_raw, list) else []

        if not retry_rules:
            # 기존 동작과 완전 동일 — 추가 로그/stat 호출 없음.
            return _single_attempt()

        # 재시도 활성화된 경로.
        input_bytes = _total_input_bytes(input_paths)

        _emit(log_callback, "attempt 1 — 실행 중...")
        out1 = _single_attempt()  # 예외 발생 시 propagate (재시도 X)
        size1 = _total_output_bytes(out1)
        ratio1 = size1 / input_bytes if input_bytes > 0 else 1.0

        extra = _match_retry_rule(ratio1, retry_rules)
        total_attempts = 1 + extra

        _emit(
            log_callback,
            f"attempt 1 결과: {size1 / 1024:.1f} kB / {input_bytes / 1024:.1f} kB "
            f"(ratio {ratio1:.2f})"
            + (f" — 임계 도달 → {extra}회 추가 시도" if extra > 0 else ""),
        )

        if extra == 0:
            return out1

        attempts: list[tuple[int, dict[str, bytes], int]] = [(1, out1, size1)]

        for i in range(2, 2 + extra):
            _emit(log_callback, f"attempt {i}/{total_attempts} — 실행 중...")
            try:
                out_i = _single_attempt()
            except CodexRunError as exc:
                _emit(log_callback, f"attempt {i} 실패: {exc} — 건너뜀")
                continue
            size_i = _total_output_bytes(out_i)
            ratio_i = size_i / input_bytes if input_bytes > 0 else 1.0
            _emit(
                log_callback,
                f"attempt {i} 결과: {size_i / 1024:.1f} kB (ratio {ratio_i:.2f})",
            )
            attempts.append((i, out_i, size_i))

        best = max(attempts, key=lambda t: t[2])
        best_ratio = best[2] / input_bytes if input_bytes > 0 else 1.0
        _emit(
            log_callback,
            f"최종 선택: attempt {best[0]} ({best[2] / 1024:.1f} kB, ratio {best_ratio:.2f})",
        )
        return best[1]


# ---------------------------------------------------------------------------
# 모듈 레벨 기본 인스턴스 + back-compat 래퍼
# ---------------------------------------------------------------------------


_default = CodexRunner()


def set_max_concurrent(n: int) -> None:
    _default.set_max_concurrent(n)


def set_stagger_interval(seconds: float) -> None:
    _default.set_stagger_interval(seconds)


def register_cancel_event(event: threading.Event) -> None:
    _default.register_cancel_event(event)


def unregister_cancel_event(event: threading.Event) -> None:
    _default.unregister_cancel_event(event)


def terminate_all_active() -> int:
    return _default.terminate_all_active()


def load_skill(skill_dir: str | Path) -> Skill:
    return _default.load_skill(skill_dir)


def clear_skill_cache() -> None:
    _default.clear_skill_cache()


def run_codex_task(*args, **kwargs) -> dict[str, bytes]:
    return _default.run_codex_task(*args, **kwargs)


def run_skill(*args, **kwargs) -> dict[str, bytes]:
    return _default.run_skill(*args, **kwargs)


def stats() -> dict[str, int]:
    return _default.stats()
