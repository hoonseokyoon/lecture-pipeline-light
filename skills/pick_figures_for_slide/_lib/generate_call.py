"""Tier 2: image_generation 스킬을 subprocess 로 호출.

두 스킬 모두 `_lib/` 를 쓰므로 직접 import 하면 이름 충돌. 각 figure 생성을
독립 subprocess 로 띄워 sys.modules 격리. 오버헤드는 ~500ms 이며 Gemini image
호출 (수 초) 또는 Codex 실행 (수십 초) 에 묻힘.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, current_cancel_event

LogCb = Callable[[str], None]

# 레포 루트 (= 이 파일에서 4단계 상위)
#   skills/pick_figures_for_slide/_lib/generate_call.py
#   ↑ _lib            ↑ skill dir       ↑ skills         ↑ repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_IMAGE_GEN_DIR = _REPO_ROOT / "skills" / "image_generation"


def _emit(log, msg):
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


def _raise_if_cancelled():
    ev = current_cancel_event.get()
    if ev is not None and ev.is_set():
        raise CodexRunError("pick_figures_for_slide (generate): 사용자 취소")


# subprocess 내부 실행 스크립트 (stdin 또는 -c 로 전달)
_BRIDGE_SCRIPT = r"""
import json, sys
from pathlib import Path

repo = Path(sys.argv[1])
ig_dir = repo / "skills" / "image_generation"
sys.path.insert(0, str(repo))
sys.path.insert(0, str(ig_dir))

from _lib.generate import generate_figure

kwargs = json.loads(sys.argv[2])
workspace = Path(kwargs.pop("workspace"))
skill_dir = Path(kwargs.pop("skill_dir"))
out_dir = Path(kwargs.pop("out_dir"))

result = generate_figure(
    workspace=workspace,
    skill_dir=skill_dir,
    **kwargs,
)

# 산출물을 out_dir 에 저장
out_dir.mkdir(parents=True, exist_ok=True)
written = []
for name, data in result.outputs.items():
    p = out_dir / name
    p.write_bytes(data)
    written.append(str(p))

# 결과 메타 stdout 으로
print("BRIDGE_RESULT:" + json.dumps({
    "cache_key": result.cache_key,
    "mode_used": result.meta.mode_used,
    "backend": result.meta.backend,
    "cache_hit": result.meta.cache_hit,
    "written": written,
}))
"""


def call_image_generation(
    *,
    hint: str,
    context: str,
    style: str,
    format: str,
    mode: str,
    seed: int,
    workspace: Path,
    out_dir: Path,
    timeout: int = 400,
    log_cb: LogCb | None = None,
) -> dict:
    """image_generation 스킬을 subprocess 로 호출.

    out_dir 에 산출 파일 저장. 반환: {cache_key, mode_used, backend, cache_hit, written}.
    """
    _raise_if_cancelled()

    kwargs = {
        "hint": hint,
        "context": context,
        "style": style,
        "format": format,
        "mode": mode,
        "seed": seed,
        "workspace": str(workspace),
        "skill_dir": str(_IMAGE_GEN_DIR),
        "out_dir": str(out_dir),
    }

    _emit(log_cb, f"[gen-call] subprocess → image_generation ({mode}, {format})")

    cmd = [
        sys.executable,
        "-c", _BRIDGE_SCRIPT,
        str(_REPO_ROOT),
        json.dumps(kwargs),
    ]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise CodexRunError(f"image_generation subprocess 타임아웃 ({timeout}s)")

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise CodexRunError(f"image_generation 실패 (exit={proc.returncode}): {tail}")

    # BRIDGE_RESULT 라인 추출
    result_line = None
    for line in proc.stdout.splitlines():
        if line.startswith("BRIDGE_RESULT:"):
            result_line = line[len("BRIDGE_RESULT:"):]
            break
    if not result_line:
        raise CodexRunError(
            f"image_generation 결과 라인 없음. stdout tail: {proc.stdout[-400:]}"
        )
    try:
        return json.loads(result_line)
    except Exception as exc:
        raise CodexRunError(f"결과 파싱 실패: {exc}\n{result_line[:300]}") from exc
