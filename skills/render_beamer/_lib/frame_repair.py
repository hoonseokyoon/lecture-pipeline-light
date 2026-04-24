"""Codex 를 이용한 frame / preamble repair.

- 별도 sub-skill 디렉토리 없이 `codex_runner.run_codex_task` 직접 호출.
- inputs/ 에 frame.tex + error.log + meta.md 올려놓고 Codex 가 outputs/ 에
  수정된 파일과 fix_note.md 를 작성한다.
- 구조 보존 (frame 은 `\\begin{frame}~\\end{frame}` 유지) 검증은 호출자가 수행.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from codex_runner import run_codex_task

from _lib.schema import Slide

logger = logging.getLogger("lecture_pipeline.render_beamer.repair")

LogCb = Callable[[str], None]


_PROMPT_FRAME = """\
LaTeX Beamer frame 컴파일 에러를 수정한다.

입력:
- inputs/frame.tex : 실패한 frame 의 LaTeX 소스 (`\\begin{frame}~\\end{frame}` 블록)
- inputs/error.log : latexmk 에러 메시지 발췌
- inputs/meta.md   : slide 메타 (slide_number, title, layout, bullets/equations 개수, figure_refs)

작업:
1. error.log 를 읽고 원인 파악 (예: undefined control sequence, missing $, extra }, 수식 밖 _, 잘못된 includegraphics 등).
2. frame.tex 를 **최소 수정** 으로 고쳐 `outputs/frame.fixed.tex` 로 저장.
3. 수정 의도를 한 문장으로 `outputs/fix_note.md` 에 기록.

규칙:
- `\\begin{frame}[fragile]{...}` ~ `\\end{frame}` 구조와 제목 유지.
- 주석 마커 `% ── slide N ──` 삭제 금지 (log 파서가 frame 위치 복원에 사용).
- 의미 보존 우선. 불가피할 때만 bullet/equation 제거 (fix_note 에 명시).
- 새 package 가 필요하면 fix_note 에 `REQUIRES_PACKAGE: <name>` 한 줄 추가.
- 외부 네트워크/설치 금지. 추측성 대규모 리라이트 금지.
"""


_PROMPT_PREAMBLE = """\
LaTeX Beamer preamble 컴파일 에러를 수정한다.

입력:
- inputs/preamble.tex : 현재 preamble (문서 클래스 ~ `\\begin{document}` 직전)
- inputs/error.log    : latexmk 에러 메시지 발췌

작업:
1. 원인 파악 (package 충돌, option 오류, 폰트 문제 등).
2. `outputs/preamble.fixed.tex` 로 수정본 저장.
3. 수정 의도 한 문장 `outputs/fix_note.md`.

규칙:
- `\\documentclass{...}` 유지.
- kotex / fontspec / xeCJK / graphicx / amsmath / tikz / xcolor / hyperref 등 기본 package 는 유지.
- `\\begin{document}` 이후는 절대 손대지 말 것 (그 뒤는 frame 이라 별도 관리).
- 외부 네트워크/설치 금지.
"""


def _format_slide_meta(slide: Slide) -> str:
    return (
        f"# Slide Meta\n\n"
        f"- slide_number: {slide.slide_number}\n"
        f"- title: {slide.title}\n"
        f"- layout: {slide.layout}\n"
        f"- bullets: {len(slide.bullets)}\n"
        f"- equations: {len(slide.equations)}\n"
        f"- figure_refs: {slide.figure_refs}\n"
        f"- figure_hint: {slide.figure_hint}\n"
    )


def repair_frame(
    *,
    frame_source: str,
    error_log: str,
    slide_meta: Slide,
    model: str = "gpt-5.4",
    reasoning_effort: str = "medium",
    timeout: int = 120,
    log_cb: LogCb | None = None,
) -> tuple[str | None, str]:
    """Return (fixed_source or None, fix_note). 실패 시 None."""
    inputs = {
        "frame.tex": frame_source.encode("utf-8"),
        "error.log": error_log.encode("utf-8"),
        "meta.md": _format_slide_meta(slide_meta).encode("utf-8"),
    }
    t0 = time.monotonic()
    try:
        outputs = run_codex_task(
            prompt=_PROMPT_FRAME,
            inputs=inputs,
            expected_outputs=["frame.fixed.tex"],
            allow_extra_outputs=True,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout=timeout,
        )
    except Exception as exc:
        if log_cb:
            log_cb(f"[render] frame repair 실패 (slide {slide_meta.slide_number}): {str(exc)[:150]}")
        return None, ""

    fixed = outputs.get("frame.fixed.tex", b"").decode("utf-8", errors="replace")
    note = outputs.get("fix_note.md", b"").decode("utf-8", errors="replace").strip()

    # 구조 validation: frame 블록이 살아있는지.
    if "\\begin{frame}" not in fixed or "\\end{frame}" not in fixed:
        if log_cb:
            log_cb(
                f"[render] slide {slide_meta.slide_number} 수리 결과 구조 손상 "
                f"(begin/end frame 누락) — 폐기"
            )
        return None, note

    dt = time.monotonic() - t0
    if log_cb:
        log_cb(
            f"[render] slide {slide_meta.slide_number} 수리 OK ({dt:.1f}s)"
            + (f" — {note[:80]}" if note else "")
        )
    return fixed, note


def repair_preamble(
    *,
    preamble_source: str,
    error_log: str,
    model: str = "gpt-5.4",
    reasoning_effort: str = "medium",
    timeout: int = 120,
    log_cb: LogCb | None = None,
) -> tuple[str | None, str]:
    """Return (fixed_preamble or None, fix_note)."""
    inputs = {
        "preamble.tex": preamble_source.encode("utf-8"),
        "error.log": error_log.encode("utf-8"),
    }
    t0 = time.monotonic()
    try:
        outputs = run_codex_task(
            prompt=_PROMPT_PREAMBLE,
            inputs=inputs,
            expected_outputs=["preamble.fixed.tex"],
            allow_extra_outputs=True,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout=timeout,
        )
    except Exception as exc:
        if log_cb:
            log_cb(f"[render] preamble repair 실패: {str(exc)[:150]}")
        return None, ""

    fixed = outputs.get("preamble.fixed.tex", b"").decode("utf-8", errors="replace")
    note = outputs.get("fix_note.md", b"").decode("utf-8", errors="replace").strip()

    if r"\documentclass" not in fixed:
        if log_cb:
            log_cb("[render] preamble 수리 결과 \\documentclass 누락 — 폐기")
        return None, note

    dt = time.monotonic() - t0
    if log_cb:
        log_cb(
            f"[render] preamble 수리 OK ({dt:.1f}s)"
            + (f" — {note[:80]}" if note else "")
        )
    return fixed, note
