#!/usr/bin/env python
"""Goose (Block) CLI 기반 에이전트 러너 — Codex 의 subprocess 대칭.

Goose: https://github.com/aaif-goose/goose  (Apache-2.0, Rust)
Headless 모드: ``goose run -t <prompt> --no-session --quiet``

관계:
- Codex 와 **배타적 백엔드**. 스킬 config.json 의 ``backend`` 키로 선택
  ("codex" 기본, "goose" 로 전환). 합성 기능 없음 — 한 task 는 한쪽만 돌림.
- 프로세스 트래킹 / cancel 레지스트리 / 전역 semaphore / stagger 는
  ``codex_runner._default`` 와 **공유**. "현재 중단" 버튼은 codex/goose 모두 kill.
- workdir / inputs / outputs / expected_outputs 계약은 동일. skill 메타가
  영향받지 않도록 설계.

사전 준비:
- `goose` 바이너리 PATH 에 있거나 `LECTURE_GOOSE_PATH` 환경변수 지정
- `goose configure` 로 공급자/모델 설정, 또는 아래 env var 로 런타임 override:
  - `GOOSE_PROVIDER` (openai / anthropic / gemini-cli / claude-code / ollama / ...)
  - `GOOSE_MODEL`
  - 각 공급자별 API 키 (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, ...)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import codex_runner
from codex_runner import (
    CodexRunError,
    _build_prompt,
    _collect_outputs,
    _write_input,
)

logger = logging.getLogger("lecture_pipeline.goose_runner")

# ── 기본값 ──
# None 이면 goose 가 env / config.yaml 에서 읽음.
DEFAULT_GOOSE_PROVIDER: str | None = None
DEFAULT_GOOSE_MODEL: str | None = None
DEFAULT_MAX_TURNS = 30
DEFAULT_MAX_TOOL_REPETITIONS = 3
DEFAULT_TIMEOUT = 600


class GooseRunner:
    """Goose CLI 호출 러너.

    상태는 최소 — 바이너리 경로만 캐시. 동시성/프로세스 관리는
    ``codex_runner._default`` 의 인프라를 빌려쓴다.
    """

    def __init__(self):
        self._binary: Path | None = self._resolve_binary()
        if self._binary is None:
            logger.warning(
                "Goose CLI 를 찾을 수 없음 — "
                "scoop install extras/goose (Windows) 또는 "
                "https://block.github.io/goose 참조 후 "
                "LECTURE_GOOSE_PATH 환경변수로 절대경로 지정."
            )
        else:
            logger.debug("Goose 바이너리: %s", self._binary)

    # ── 바이너리 resolver ──

    @staticmethod
    def _resolve_binary() -> Path | None:
        """Goose 바이너리 경로 탐색.

        Windows 에서는 `.exe` 를 **강력히 선호**. `.cmd` 배치 shim 은 `-t` 로
        전달되는 multi-line 프롬프트를 손상시켜 goose 가 빈 프롬프트로 인식
        (배너 출력 후 "please send the task" 응답) 하는 증상이 있다.
        """
        override = os.environ.get("LECTURE_GOOSE_PATH")
        if override:
            p = Path(override)
            if p.is_file():
                return p
            logger.warning("LECTURE_GOOSE_PATH 존재하지 않음: %s", override)

        if sys.platform == "win32":
            # .exe 우선
            exe = shutil.which("goose.exe")
            if exe:
                return Path(exe)
            # .cmd 폴백 — 단, multi-line prompt 손실 경고
            cmd = shutil.which("goose.cmd")
            if cmd:
                logger.warning(
                    "goose.cmd(배치 shim)를 사용합니다. multi-line 프롬프트가 "
                    "깨질 수 있으니 `goose.exe` 직접 경로를 LECTURE_GOOSE_PATH "
                    "또는 PATH 에 두는 것을 권장."
                )
                return Path(cmd)
            # 일반 설치 위치 probe
            for candidate in (
                Path.home() / "scoop" / "apps" / "goose-cli" / "current" / "goose.exe",
                Path.home() / "tools" / "goose-cli" / "goose-package" / "goose.exe",
            ):
                if candidate.is_file():
                    return candidate
            return None

        found = shutil.which("goose")
        return Path(found) if found else None

    # ── 본 실행 ──

    def run_goose_task(
        self,
        prompt: str,
        inputs: dict[str, str | bytes | Path],
        *,
        scripts_dir: str | Path | None = None,
        scripts_ignore: tuple[str, ...] = (),
        expected_outputs: list[str] | None = None,
        provider: str | None = DEFAULT_GOOSE_PROVIDER,
        model: str | None = DEFAULT_GOOSE_MODEL,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_tool_repetitions: int = DEFAULT_MAX_TOOL_REPETITIONS,
        timeout: int = DEFAULT_TIMEOUT,
        cancel_event: threading.Event | None = None,
        venv_bin: Path | None = None,
    ) -> dict[str, bytes]:
        """Goose 로 prompt + inputs -> outputs 작업 실행.

        Codex 러너와 동일한 계약:
        - ``inputs`` 파일을 workdir/inputs/ 에 스테이징
        - ``scripts_dir`` 이 있으면 workdir/scripts/ 로 복사
        - 에이전트는 workdir/outputs/ 에 결과 파일 작성
        - strict 모드(`expected_outputs` 지정)면 누락 시 에러

        Args:
            provider: None 이면 GOOSE_PROVIDER env / config.yaml 사용.
            model: None 이면 GOOSE_MODEL env / config.yaml 사용.
            max_turns: 에이전트가 user 개입 없이 돌 수 있는 최대 턴.
            max_tool_repetitions: 동일 도구의 동일 인자 연속 호출 상한
                (무한 루프 방지).
            cancel_event: 명시적 cancel Event. None 이면
                ``codex_runner.current_cancel_event`` ContextVar 에서 조회.
            venv_bin: 스킬 venv 의 bin/Scripts 경로. PATH 에 prepend.

        Raises:
            CodexRunError: 바이너리 미설치, 입력 검증 실패, Goose 실행 실패,
                또는 strict 모드에서 기대 출력 누락.
        """
        if self._binary is None:
            raise CodexRunError(
                "Goose CLI 미설치. "
                "Windows: `scoop install extras/goose` 또는 "
                "release 에서 `goose-x86_64-pc-windows-msvc.zip` 다운로드. "
                "또는 LECTURE_GOOSE_PATH 환경변수로 goose.exe 절대경로 지정."
            )

        if not prompt or not prompt.strip():
            raise CodexRunError("prompt 가 비어있음")
        if not inputs:
            raise CodexRunError("inputs 가 비어있음")
        if expected_outputs is not None and len(expected_outputs) == 0:
            raise CodexRunError(
                "expected_outputs 가 빈 리스트임. 느슨 모드는 None 전달."
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

        with tempfile.TemporaryDirectory(prefix="goose_task_") as tmpdir:
            workdir = Path(tmpdir)
            inputs_dir = workdir / "inputs"
            outputs_dir = workdir / "outputs"
            inputs_dir.mkdir()
            outputs_dir.mkdir()

            for name, value in inputs.items():
                _write_input(inputs_dir / name, value)

            # 보조 스크립트 스테이징 (codex 와 동일 패턴)
            has_scripts = False
            if scripts_dir is not None:
                src = Path(scripts_dir)
                if not src.is_dir():
                    raise CodexRunError(f"scripts_dir 가 디렉토리가 아님: {src}")
                ignore = (
                    shutil.ignore_patterns(*scripts_ignore)
                    if scripts_ignore else None
                )
                shutil.copytree(src, workdir / "scripts", ignore=ignore)
                if any((workdir / "scripts").iterdir()):
                    has_scripts = True
                else:
                    shutil.rmtree(workdir / "scripts")

            # Codex 와 동일한 환경 마크다운 프롬프트
            full_prompt = _build_prompt(prompt, has_scripts, expected_outputs)

            # env: 현재 프로세스 상속 + GOOSE_PROVIDER/MODEL 주입 + venv PATH prepend.
            # 주의: goose CLI 는 `--provider`/`--model` 플래그만으로는 초기화가
            # 안 되고(`error: No provider configured` 발생) `GOOSE_PROVIDER` env
            # 가 있어야 동작. 그래서 env 에 직접 주입한다.
            env = os.environ.copy()
            if provider:
                env["GOOSE_PROVIDER"] = provider
            if model:
                env["GOOSE_MODEL"] = model
            if venv_bin is not None:
                env["PATH"] = (
                    str(venv_bin) + os.pathsep + env.get("PATH", "")
                )

            argv: list[str] = [
                str(self._binary), "run",
                "-t", full_prompt,
                "--no-session",      # 자동화 용 — 세션 파일 남기지 않음
                "--quiet",           # 진행 출력 억제 (최종 응답만 stdout)
                "--max-turns", str(max_turns),
                "--max-tool-repetitions", str(max_tool_repetitions),
            ]

            # codex_runner 의 전역 인프라 재사용 —
            # stagger + semaphore + active_procs 트래킹 + cancel 레지스트리
            runtime = codex_runner._default
            runtime._stagger_wait()
            with runtime.semaphore:
                rc, stdout, stderr = runtime._run_tracked(
                    cmd=argv,
                    timeout=timeout,
                    kind="Goose 실행",
                    cwd=str(workdir),
                    env=env,
                )

            # 사용자 취소 체크: 명시 인자 > ContextVar
            effective_cancel = (
                cancel_event if cancel_event is not None
                else codex_runner.current_cancel_event.get()
            )
            if effective_cancel is not None and effective_cancel.is_set():
                raise CodexRunError("Goose 실행 중 사용자 취소")

            if rc != 0:
                tail = (stderr or stdout or "")[-500:]
                raise CodexRunError(f"Goose 실행 실패 (exit={rc}):\n{tail}")

            collected = _collect_outputs(outputs_dir)

            # Fallback: expected_outputs 가 1개인데 파일이 안 만들어졌으면
            # stdout 의 최종 응답을 해당 파일로 간주. Codex 의 _last_message
            # 경로와 같은 철학.
            if (
                strict and expected_outputs
                and len(expected_outputs) == 1
                and expected_outputs[0] not in collected
                and stdout.strip()
            ):
                collected[expected_outputs[0]] = stdout.encode("utf-8")

            if strict:
                missing = [n for n in expected_outputs if n not in collected]
                if missing:
                    extras = sorted(set(collected) - set(expected_outputs))
                    msg = f"기대 출력 누락: {missing}"
                    if extras:
                        msg += f" / 생성된 파일: {extras}"
                    diag_parts = []
                    if stdout:
                        diag_parts.append(f"stdout tail:\n{stdout[-600:]}")
                    if stderr:
                        diag_parts.append(f"stderr tail:\n{stderr[-600:]}")
                    if diag_parts:
                        msg += "\n---\n" + "\n---\n".join(diag_parts)
                    raise CodexRunError(msg)
                return {n: collected[n] for n in expected_outputs}

            if not collected:
                raise CodexRunError("outputs/에 아무 파일도 없음")
            return collected


# ── 모듈 레벨 싱글톤 + 래퍼 ──
_default = GooseRunner()


def run_goose_task(*args, **kwargs) -> dict[str, bytes]:
    return _default.run_goose_task(*args, **kwargs)
