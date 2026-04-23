"""Claude Code CLI 를 Head Agent 런타임으로 래핑.

- `claude --print --resume <sid>` subprocess 호출
- RFI 당 세션 1개 (`.litproj/current_session.txt`)
- 응답은 journal 에도 기록됨 → Streamlit observer 가 자동으로 렌더
- 백그라운드 실행 지원 (sync / async)
- cwd = 프로젝트 루트 (AGENTS.md 자동 로드됨)

외부 요구:
- `claude` CLI 가 PATH 에 있거나 `LIT_CLAUDE_PATH` 환경변수로 지정
- `claude auth login` 으로 이미 로그인된 상태
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from gui_lit import ipc

logger = logging.getLogger("lecture_pipeline.gui_lit.head_agent")

DEFAULT_TIMEOUT = 1800  # 30분
DEFAULT_MODEL = "claude-opus-4-7"


class HeadAgentError(Exception):
    """Head agent 호출 실패."""


def _popen_kwargs_for_group() -> dict:
    """subprocess 트리 종료를 위한 플랫폼별 Popen kwargs.

    - POSIX: `start_new_session=True` → setsid, 독립 process group.
      killpg 로 트리 종료 가능.
    - Windows: CREATE_NEW_PROCESS_GROUP 불필요 — taskkill /T /PID 가
      자식 트리를 알아서 추적. 빈 dict.
    """
    if sys.platform == "win32":
        return {}
    return {"start_new_session": True}


def _resolve_claude_cli() -> str:
    override = os.environ.get("LIT_CLAUDE_PATH", "").strip()
    if override:
        if not Path(override).exists():
            raise HeadAgentError(
                f"LIT_CLAUDE_PATH 가 가리키는 파일 없음: {override}"
            )
        return override
    # Windows 에서 `.cmd` shim 도 감지되도록 shutil.which.
    found = shutil.which("claude")
    if found:
        return found
    # Windows 특유: `claude.cmd` 만 PATH 에 있는 경우
    found_cmd = shutil.which("claude.cmd")
    if found_cmd:
        return found_cmd
    raise HeadAgentError(
        "claude CLI 를 찾을 수 없음. `claude auth login` 후 PATH 등록하거나 "
        "LIT_CLAUDE_PATH 환경변수 지정."
    )


@dataclass
class AgentRun:
    """Head agent 단일 호출 결과."""
    session_id: str | None
    response_text: str
    raw_json: dict
    returncode: int
    stderr: str
    duration_s: float


@dataclass
class AsyncRunState:
    """비동기 실행 상태 — Streamlit 에서 polling."""
    thread: threading.Thread
    done: threading.Event = field(default_factory=threading.Event)
    result: AgentRun | None = None
    error: Exception | None = None

    def is_done(self) -> bool:
        return self.done.is_set()


class HeadAgent:
    """Head agent 실행 인스턴스. 프로젝트 루트마다 하나."""

    def __init__(
        self,
        project_root: Path,
        *,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.project_root = Path(project_root).resolve()
        self.model = model
        self.timeout = timeout
        self._active_proc: subprocess.Popen | None = None
        self._active_lock = threading.Lock()

    # ── 공개 API ──

    def _read_current_rfi_id(self) -> str | None:
        """REQUEST_FOR_INFORMATION.md front matter 에서 id 만 추출."""
        rfi_path = self.project_root / "REQUEST_FOR_INFORMATION.md"
        if not rfi_path.exists():
            return None
        try:
            text = rfi_path.read_text(encoding="utf-8")
        except OSError:
            return None
        import re
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
        if not m:
            return None
        for line in m.group(1).split("\n"):
            k, _, v = line.partition(":")
            if k.strip() == "id":
                val = v.strip().strip('"').strip("'")
                return val or None
        return None

    def send(
        self,
        message: str,
        *,
        on_stream_event: Callable[[dict], None] | None = None,
    ) -> AgentRun:
        """동기: 메시지 보내고 응답 받을 때까지 블록.

        - message 는 inbox 에도 드롭 (감사)
        - claude 에는 "inbox 를 확인하고 응답하라" nudge 로 호출
        - 세션 ID 가 있으면 --resume, 없으면 신규 세션
        """
        # 1) inbox drop (journal 에도 기록됨)
        ipc.drop_inbox_message(self.project_root, message, author="user")

        # 2) halt flag 존재 시 거부
        if ipc.is_halted(self.project_root):
            raise HeadAgentError(
                "halt flag 가 설정되어 있습니다. 해제 후 재시도 "
                "(`.litproj/halt` 삭제)."
            )

        # 3) 프롬프트 구성 — 에이전트가 먼저 모드를 판단하도록 중립적으로.
        #    AGENTS.md 의 "대화 모드 vs 작업 모드" 섹션이 행동을 분기시킴.
        prompt = (
            "사용자로부터 새 메시지가 `.litproj/inbox/` 에 도착했습니다. "
            "AGENTS.md 를 먼저 확인하고, 특히 **대화 모드 vs 작업 모드** 판단을 "
            "수행한 뒤 그에 맞게 응답하세요.\n\n"
            "- 단순 질문·의견 요청·피드백이면 가볍게 직접 답 (파일·git 최소 접근).\n"
            "- 명시적 작업 지시(슬래시 또는 자연어)라면 전체 프로토콜 수행.\n"
            "- 애매하면 먼저 사용자에게 되물어라.\n\n"
            f"이번 사용자 메시지:\n```\n{message[:2000]}\n```"
        )

        prev_sid = ipc.read_current_session(self.project_root)
        result = self._run(prompt, on_stream_event=on_stream_event)

        # 세션 히스토리 기록 (UI 에서 resume 선택 가능하도록)
        try:
            if result.session_id:
                rfi_id = self._read_current_rfi_id()
                if not prev_sid or prev_sid != result.session_id:
                    ipc.record_session_event(
                        self.project_root,
                        result.session_id,
                        "created",
                        rfi_id=rfi_id,
                        title=message,
                    )
                ipc.record_session_event(
                    self.project_root,
                    result.session_id,
                    "used",
                    rfi_id=rfi_id,
                )
        except Exception as exc:  # 이게 본 응답을 깨뜨리지 않게
            logger.warning("세션 history 기록 실패: %s", exc)

        return result

    def send_async(self, message: str) -> AsyncRunState:
        """비동기 실행 — 백그라운드 스레드에서 send 수행.

        Streamlit 에서 `state.is_done()` 으로 폴링하고, done=True 면
        `state.result` 혹은 `state.error` 확인.
        """
        state = AsyncRunState(thread=threading.Thread(daemon=True))

        def _run():
            try:
                state.result = self.send(message)
            except Exception as exc:
                state.error = exc
                logger.exception("head agent async 실패: %s", exc)
            finally:
                state.done.set()

        state.thread = threading.Thread(target=_run, daemon=True)
        state.thread.start()
        return state

    def cancel_active(self) -> bool:
        """진행 중인 subprocess 트리 강제 종료.

        claude CLI 는 내부에서 bash/git/기타 tool 을 spawn 하므로 단순
        terminate 만으로는 자식이 orphan 으로 남는다. 플랫폼별로 프로세스
        그룹 전체를 죽인다.
        """
        with self._active_lock:
            proc = self._active_proc
            if proc is None or proc.poll() is not None:
                return False
            pid = proc.pid
            try:
                if sys.platform == "win32":
                    # /T = tree, /F = force
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True,
                        timeout=10,
                    )
                else:
                    import os as _os
                    import signal as _signal
                    try:
                        # process group kill (setsid 로 시작한 경우)
                        _os.killpg(_os.getpgid(pid), _signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        proc.terminate()
            except (OSError, subprocess.SubprocessError) as exc:
                logger.warning("cancel_active kill 실패 — fallback terminate: %s", exc)
                try:
                    proc.terminate()
                except OSError:
                    pass
            # 종료 확인
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
            # journal 에 기록 — UI 에서 "취소됨" 가시화
            try:
                ipc.append_journal(
                    self.project_root,
                    {
                        "actor": "user",
                        "kind": "cancelled",
                        "pid": pid,
                    },
                )
            except Exception:  # 절대 여기서 터지면 안됨
                pass
            return True

    # ── 내부 ──

    def _build_cmd(
        self, prompt: str, *, stream: bool,
    ) -> tuple[list[str], str | None]:
        cli = _resolve_claude_cli()
        sid = ipc.read_current_session(self.project_root)
        cmd = [cli, "--print"]
        if stream:
            cmd.extend(["--output-format", "stream-json", "--verbose"])
        else:
            cmd.extend(["--output-format", "json"])
        cmd.extend(["--model", self.model])
        if sid:
            cmd.extend(["--resume", sid])
        cmd.append(prompt)
        return cmd, sid

    # ── Streaming 경로 ──

    def _emit_stream_event(
        self, event: dict, *, response_text_buf: list[str],
    ) -> None:
        """stream-json 라인 1개 → journal 이벤트 emit."""
        etype = event.get("type")

        if etype == "system" and event.get("subtype") == "init":
            sid = event.get("session_id")
            if sid:
                ipc.append_journal(
                    self.project_root,
                    {
                        "actor": "head",
                        "kind": "session_start",
                        "session": sid,
                        "model": event.get("model"),
                    },
                )
            return

        if etype == "assistant":
            msg = event.get("message") or {}
            for block in msg.get("content") or []:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                    if text:
                        response_text_buf.append(text)
                        # 부분 텍스트는 중간 이벤트로만 기록 (최종 agent_response 는 별도)
                        ipc.append_journal(
                            self.project_root,
                            {
                                "actor": "head",
                                "kind": "assistant_text",
                                "content": text[:600],
                            },
                        )
                elif btype == "tool_use":
                    name = block.get("name") or "?"
                    tool_input = block.get("input") or {}
                    # 입력이 너무 크면 키만
                    try:
                        input_json = json.dumps(tool_input, ensure_ascii=False)
                        if len(input_json) > 400:
                            input_summary = {
                                "_truncated": True,
                                "keys": list(tool_input.keys())
                                if isinstance(tool_input, dict) else None,
                                "preview": input_json[:300],
                            }
                        else:
                            input_summary = tool_input
                    except (TypeError, ValueError):
                        input_summary = {"_unserializable": True}
                    ipc.append_journal(
                        self.project_root,
                        {
                            "actor": "head",
                            "kind": "tool_use",
                            "tool": name,
                            "tool_id": block.get("id"),
                            "input": input_summary,
                        },
                    )
            return

        if etype == "user":
            # tool_result 를 포함하는 synthetic user 메시지
            msg = event.get("message") or {}
            for block in msg.get("content") or []:
                if block.get("type") != "tool_result":
                    continue
                content = block.get("content")
                if isinstance(content, list):
                    pieces = []
                    for c in content:
                        if isinstance(c, dict):
                            pieces.append(c.get("text") or "")
                    content_str = " ".join(pieces)
                else:
                    content_str = str(content or "")
                ipc.append_journal(
                    self.project_root,
                    {
                        "actor": "head",
                        "kind": "tool_result",
                        "tool_id": block.get("tool_use_id"),
                        "is_error": bool(block.get("is_error")),
                        "content": content_str[:600],
                    },
                )
            return

        # system/result/기타 type 은 외부 루프에서 처리

    def _run_streaming(self, prompt: str) -> AgentRun:
        import time

        cmd, prev_sid = self._build_cmd(prompt, stream=True)
        logger.info("head agent stream (resume=%s)", prev_sid)

        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # line-buffered
                **_popen_kwargs_for_group(),
            )
        except FileNotFoundError as exc:
            raise HeadAgentError(f"claude 실행 실패: {exc}") from exc

        with self._active_lock:
            self._active_proc = proc

        final_result: dict | None = None
        final_sid: str | None = prev_sid
        response_text_buf: list[str] = []
        raw_lines: list[str] = []
        timed_out = False

        try:
            while True:
                if time.monotonic() - t0 > self.timeout:
                    proc.kill()
                    timed_out = True
                    break

                line = proc.stdout.readline() if proc.stdout else ""
                if not line:
                    if proc.poll() is not None:
                        break
                    # 일시적 빈 readline — 짧게 쉬고 재시도
                    time.sleep(0.05)
                    continue

                line = line.rstrip()
                if not line:
                    continue
                raw_lines.append(line)

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("stream line 파싱 실패: %r", line[:200])
                    continue

                etype = event.get("type")
                if etype == "result":
                    final_result = event
                    sid = event.get("session_id")
                    if sid:
                        final_sid = sid
                    # result 이후 stream 종료
                    continue

                try:
                    self._emit_stream_event(
                        event, response_text_buf=response_text_buf,
                    )
                except Exception as exc:
                    logger.exception("stream event emit 실패: %s", exc)

            # subprocess 정리
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        finally:
            with self._active_lock:
                self._active_proc = None

        duration = time.monotonic() - t0
        stderr = proc.stderr.read() if proc.stderr else ""

        if timed_out:
            raise HeadAgentError(
                f"claude 호출 타임아웃 ({self.timeout}s)"
            )

        if proc.returncode != 0:
            tail_src = (stderr + "\n" + "\n".join(raw_lines))[-600:]
            raise HeadAgentError(
                f"claude 실패 (exit={proc.returncode}):\n{tail_src}"
            )

        # result 이벤트가 없으면 응답 추론 (fallback)
        response_text = ""
        if final_result is not None:
            response_text = final_result.get("result") or ""
        if not response_text and response_text_buf:
            response_text = "".join(response_text_buf)

        if final_sid:
            ipc.write_current_session(self.project_root, final_sid)

        ipc.append_journal(
            self.project_root,
            {
                "actor": "head",
                "kind": "agent_response",
                "content": response_text[:20000],
                "session": final_sid,
                "duration_s": round(duration, 2),
                "model": self.model,
            },
        )

        return AgentRun(
            session_id=final_sid,
            response_text=response_text,
            raw_json=final_result or {"type": "result", "_note": "final event missing"},
            returncode=proc.returncode,
            stderr=stderr,
            duration_s=duration,
        )

    # ── 비-스트리밍 경로 (fallback) ──

    def _run_json_once(self, prompt: str) -> AgentRun:
        import time

        cmd, prev_sid = self._build_cmd(prompt, stream=False)
        logger.info("head agent invoke (json, resume=%s)", prev_sid)

        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **_popen_kwargs_for_group(),
            )
        except FileNotFoundError as exc:
            raise HeadAgentError(f"claude 실행 실패: {exc}") from exc

        with self._active_lock:
            self._active_proc = proc

        try:
            stdout, stderr = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            raise HeadAgentError(
                f"claude 호출 타임아웃 ({self.timeout}s)"
            )
        finally:
            with self._active_lock:
                self._active_proc = None

        duration = time.monotonic() - t0

        if proc.returncode != 0:
            tail = (stderr + stdout)[-600:]
            raise HeadAgentError(
                f"claude 실패 (exit={proc.returncode}):\n{tail}"
            )

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise HeadAgentError(
                f"claude JSON 파싱 실패: {exc}\n출력 앞부분: {stdout[:400]}"
            ) from exc

        new_sid = data.get("session_id")
        if new_sid:
            ipc.write_current_session(self.project_root, new_sid)

        response_text = (
            data.get("result") or data.get("response") or data.get("text") or ""
        )

        ipc.append_journal(
            self.project_root,
            {
                "actor": "head",
                "kind": "agent_response",
                "content": response_text[:20000],
                "session": new_sid,
                "duration_s": round(duration, 2),
                "model": self.model,
            },
        )

        return AgentRun(
            session_id=new_sid,
            response_text=response_text,
            raw_json=data,
            returncode=proc.returncode,
            stderr=stderr,
            duration_s=duration,
        )

    def _run(
        self,
        prompt: str,
        *,
        on_stream_event: Callable[[dict], None] | None = None,
    ) -> AgentRun:
        """기본 streaming. 실패 시 json 모드로 1회 fallback."""
        use_stream = os.environ.get("LIT_HEAD_STREAM", "1") != "0"
        if not use_stream:
            return self._run_json_once(prompt)
        try:
            return self._run_streaming(prompt)
        except HeadAgentError as exc:
            # stream-json 이 예상과 다른 Claude CLI 버전일 수 있음 → json 모드로 재시도
            msg = str(exc).lower()
            looks_like_format_issue = (
                "output-format" in msg
                or "verbose" in msg
                or "JSON 파싱" in str(exc)
                or "result event missing" in str(exc).lower()
            )
            if looks_like_format_issue:
                logger.warning(
                    "stream-json 실패 — json fallback 재시도: %s", exc,
                )
                return self._run_json_once(prompt)
            raise


def check_cli_available() -> tuple[bool, str]:
    """환경 점검 — UI 에서 사전 경고용."""
    try:
        cli = _resolve_claude_cli()
    except HeadAgentError as exc:
        return False, str(exc)
    try:
        proc = subprocess.run(
            [cli, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return False, f"claude --version 실패: {exc}"
    if proc.returncode != 0:
        return False, f"claude --version exit={proc.returncode}: {proc.stderr[:200]}"
    return True, proc.stdout.strip() or "ok"


if __name__ == "__main__":
    # 스모크 테스트 — CLI 가용성만 확인 (실제 호출은 하지 않음)
    ok, msg = check_cli_available()
    print("claude CLI:", "OK" if ok else "FAIL", msg)
    sys.exit(0 if ok else 1)
