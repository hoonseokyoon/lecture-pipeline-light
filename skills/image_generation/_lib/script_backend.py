"""Script backend — Codex 로 matplotlib/TikZ 코드 생성 + 실행.

`codex_runner.run_codex_task` 를 그대로 사용한다:
- prompt 는 문자열 인자로 직접 전달
- inputs 에 task_hint.md 를 담아 동적 내용 공급
- expected_outputs 로 수집할 파일 지정
- semaphore · cancel · timeout · retry 전부 codex_runner 가 처리

PNG: outputs/fig.source.py (matplotlib 코드) + outputs/fig.png
TeX: outputs/fig.source.tex (Beamer frame TikZ) + (bonus) outputs/fig.png
"""

from __future__ import annotations

import time
from typing import Callable

from codex_runner import CodexRunError, run_codex_task

from _lib.schema import FigureSpec

LogCb = Callable[[str], None]


def _emit(log, msg):
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


_PROMPT_MATPLOTLIB = """\
matplotlib 으로 학술 figure 한 장을 생성한다.

작업 명세는 `inputs/task_hint.md` 파일에 있음. 그 파일을 읽고 지시에 따를 것.

규칙:
1. matplotlib 코드를 `outputs/fig.source.py` 로 저장. 실행 가능한 완결 스크립트.
2. 해당 스크립트를 실행해 `outputs/fig.png` 생성. (Python 직접 실행 또는 `python outputs/fig.source.py`.)
3. Journal 스타일: sans-serif, top·right spine 제거, grid 옅게, tight_layout.
4. `np.random.seed(<task_hint.md 의 Seed 값>)` 을 코드 상단에 명시.
5. `plt.savefig('outputs/fig.png', dpi=150, bbox_inches='tight')`. figsize 는 (7, 4.5) 또는 적절히.
6. 라벨·범례는 영어. 제목은 생략 (슬라이드 제목이 별도).
7. 최종 검증: `outputs/fig.source.py` 와 `outputs/fig.png` 가 모두 non-empty 로 존재.

외부 네트워크·설치 금지. matplotlib/numpy 만 사용.
"""


_PROMPT_TIKZ = """\
TikZ 로 학술 figure Beamer frame 코드를 작성한다.

작업 명세는 `inputs/task_hint.md` 파일에 있음. 그 파일을 읽고 지시에 따를 것.

규칙:
1. **Frame only** — `\\begin{tikzpicture} ... \\end{tikzpicture}` 블록만. `\\documentclass`/`\\begin{document}` 금지.
2. 파일명: `outputs/fig.source.tex`.
3. 필요한 `\\usetikzlibrary{...}` 가 있으면 파일 상단에 LaTeX 주석으로 명시:
   `% requires: \\usetikzlibrary{shapes.geometric, arrows.meta}`
4. 색상은 기본 xcolor (red/blue/black/gray + `!NN` 명도).
5. 좌표는 상대, frame 전체가 (-4,-3) ~ (4,3) 근처.
6. 라벨은 영어.
7. 가능하면 `pdflatex` 로 standalone 컴파일을 `outputs/_tmp/` 에서 시도해 `outputs/fig.png` 로 rasterize (bonus, 없거나 실패하면 skip).

외부 네트워크 금지. 파일은 `outputs/fig.source.tex` (+ optional `outputs/fig.png`) 만.
"""


def _compose_task_hint(spec: FigureSpec) -> str:
    parts = [
        "# Figure Generation Task",
        "",
        f"**Hint**: {spec.hint}",
        "",
    ]
    if spec.context:
        parts.extend(["**Context**:", "", spec.context, ""])
    if spec.style:
        parts.extend([f"**Style**: {spec.style}", ""])
    parts.extend([f"**Seed**: {spec.seed}", ""])
    return "\n".join(parts)


def run_script_backend(
    spec: FigureSpec,
    cfg: dict,
    log_cb: LogCb | None = None,
) -> tuple[dict[str, bytes], dict]:
    """Codex 로 matplotlib/TikZ 작성·실행. `run_codex_task` 가 저수준 전부 처리."""
    timeout = int(cfg.get("script_timeout", 180))
    max_retries = int(cfg.get("max_retries_script", 2))
    model = cfg.get("codex_model", "gpt-5.4")
    reasoning = cfg.get("codex_reasoning_effort", "medium")

    if spec.format == "png":
        prompt = _PROMPT_MATPLOTLIB
        expected = ["fig.source.py", "fig.png"]
    elif spec.format == "tex":
        prompt = _PROMPT_TIKZ
        # tex: fig.png 는 bonus 라 strict 로 걸면 빠짐 → loose mode (None)
        expected = None
    else:
        raise CodexRunError(f"알 수 없는 format: {spec.format}")

    inputs = {"task_hint.md": _compose_task_hint(spec).encode("utf-8")}

    t0 = time.monotonic()
    last_exc: Exception | None = None
    outputs: dict[str, bytes] = {}
    for attempt in range(max_retries + 1):
        try:
            outputs = run_codex_task(
                prompt=prompt,
                inputs=inputs,
                expected_outputs=expected,
                allow_extra_outputs=True,  # tex 의 bonus fig.png, 기타 artifacts 허용
                model=model,
                reasoning_effort=reasoning,
                timeout=timeout,
            )
            break
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                _emit(
                    log_cb,
                    f"[script] 재시도 {attempt+1}/{max_retries} — {str(exc)[:150]}",
                )
                continue
            raise CodexRunError(f"script backend 실패: {exc}") from exc

    # 산출물 검증 (run_codex_task 가 strict 모드일 땐 이미 보장되지만 tex 는 loose)
    if spec.format == "tex" and "fig.source.tex" not in outputs:
        raise CodexRunError("fig.source.tex 산출 누락")

    dt = time.monotonic() - t0
    meta = {
        "backend": f"codex_{spec.format}",
        "model": model,
        "prompt_snapshot": prompt[:400],
        "retries": attempt,
        "duration_s": round(dt, 2),
    }
    _emit(log_cb, f"[script] OK — {list(outputs.keys())} ({dt:.1f}s)")
    return outputs, meta
