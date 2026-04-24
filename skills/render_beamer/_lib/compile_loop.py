"""latexmk 호출 + 수리 iteration 오케스트레이션.

흐름:
1. latexmk (없으면 xelatex 2회) 로 컴파일 시도.
2. 성공 시 PDF 반환.
3. 실패 시 .log 파싱 → preamble/frame 에러 분류.
4. preamble error 는 iteration 0 에서 1회만 Codex 로 수리.
5. frame error 는 frame_index 별로 Codex repair. N회 실패하면 placeholder 로 교체.
6. 최대 iteration 도달 시 중단.

모든 aux/out 파일은 임시 디렉토리에서 처리. 최종 .tex 는 text 로 반환.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, current_cancel_event

from _lib.frame_repair import repair_frame, repair_preamble
from _lib.latex_log import parse_latex_log
from _lib.schema import LectureOutline, LatexError, RenderReport, RepairAttempt
from _lib.templates import render_placeholder_frame

LogCb = Callable[[str], None]


def _emit(log: LogCb | None, msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


def _raise_if_cancelled() -> None:
    ev = current_cancel_event.get()
    if ev is not None and ev.is_set():
        raise CodexRunError("render_beamer: 사용자 취소")


# ─── latexmk / xelatex 실행 ──────────────────────────────────


def _candidate_tex_bin_dirs() -> list[Path]:
    """일반적인 TeX 배포판 설치 위치 (Windows 위주).

    GUI 실행 시 PATH 에 등록이 안 되어 있어도 이 디렉토리들을 직접 뒤진다.
    """
    dirs: list[Path] = []
    if sys.platform != "win32":
        # POSIX 는 PATH 에 있으면 됨. 추가 fallback 없음.
        return dirs

    home = Path.home()
    # MiKTeX per-user (가장 흔함)
    dirs.append(home / "AppData/Local/Programs/MiKTeX/miktex/bin/x64")
    dirs.append(home / "AppData/Local/Programs/MiKTeX 2.9/miktex/bin/x64")
    # MiKTeX system-wide
    dirs.append(Path("C:/Program Files/MiKTeX/miktex/bin/x64"))
    dirs.append(Path("C:/Program Files/MiKTeX 2.9/miktex/bin/x64"))
    dirs.append(Path("C:/Program Files (x86)/MiKTeX/miktex/bin"))
    # TeX Live (최근 버전부터 시도)
    for year in ("2026", "2025", "2024", "2023", "2022"):
        dirs.append(Path(f"C:/texlive/{year}/bin/windows"))
        dirs.append(Path(f"C:/texlive/{year}/bin/win32"))
    # scoop 으로 설치한 경우
    scoop = home / "scoop/apps"
    if scoop.is_dir():
        for sub in ("latex", "miktex", "texlive"):
            candidate = scoop / sub / "current"
            if candidate.is_dir():
                for root, _, _ in os.walk(candidate):
                    rp = Path(root)
                    if rp.name.lower() in {"bin", "x64", "windows"}:
                        dirs.append(rp)
    return [d for d in dirs if d.is_dir()]


def _find_tex_binary(name: str) -> str | None:
    """shutil.which fallback + Windows 일반 설치 위치 검색."""
    exts = ["", ".exe"] if sys.platform == "win32" else [""]
    for suffix in exts:
        p = shutil.which(name + suffix)
        if p:
            return p
    # fallback: 일반 디렉토리 검색
    for d in _candidate_tex_bin_dirs():
        for suffix in exts:
            candidate = d / (name + suffix)
            if candidate.exists():
                return str(candidate)
    return None


def _resolve_latexmk() -> str | None:
    """latexmk 바이너리 + Perl 존재 확인.

    latexmk 는 Perl 스크립트라 Perl 이 PATH 에 없으면 실행 자체가 실패
    ("MiKTeX could not find the script engine 'perl'"). 둘 다 있을 때만
    반환하고, 없으면 None → xelatex 직접 호출 분기로 fallback.
    """
    lmk = _find_tex_binary("latexmk")
    if not lmk:
        return None
    perl = shutil.which("perl") or shutil.which("perl.exe")
    if not perl:
        # Git for Windows 기본 설치 위치도 체크
        git_perl = Path("C:/Program Files/Git/usr/bin/perl.exe")
        if git_perl.exists():
            perl = str(git_perl)
    if not perl:
        return None
    return lmk


def _resolve_xelatex() -> str:
    p = _find_tex_binary("xelatex")
    if p:
        return p
    searched = [str(d) for d in _candidate_tex_bin_dirs()]
    raise CodexRunError(
        "xelatex 실행 파일을 찾을 수 없음. TeX Live / MiKTeX 설치 필요. "
        f"검색 위치: PATH, {', '.join(searched) if searched else '(추가 위치 없음)'}"
    )


def _candidate_perl_bin_dirs() -> list[Path]:
    """latexmk 가 필요로 하는 Perl 의 일반적인 설치 위치 (Windows).

    Perl 이 Windows 에 기본 설치되어 있지 않으므로 Git for Windows, Strawberry
    Perl, ActivePerl 순으로 탐색.
    """
    if sys.platform != "win32":
        return []
    dirs: list[Path] = []
    dirs.append(Path("C:/Program Files/Git/usr/bin"))
    dirs.append(Path("C:/Program Files (x86)/Git/usr/bin"))
    dirs.append(Path("C:/Strawberry/perl/bin"))
    dirs.append(Path("C:/Perl64/bin"))
    dirs.append(Path("C:/Perl/bin"))
    return [d for d in dirs if (d / "perl.exe").exists()]


def _sanitized_env() -> dict[str, str]:
    """subprocess 용 env 준비.

    세 가지 처리:
    1. PATH 에 디렉토리가 아닌 항목 (잘못 등록된 .exe 등) 제거
       — MiKTeX 가 dir stat 돌 때 실패하는 것 방지.
    2. TeX 바이너리 설치 디렉토리 prepend
       — xelatex 이 내부적으로 다른 MiKTeX 툴 호출 시 필요.
    3. Perl 설치 디렉토리 prepend (있으면)
       — latexmk 는 Perl 스크립트.
    """
    env = os.environ.copy()
    path = env.get("PATH", "") or env.get("Path", "")
    sep = os.pathsep
    cleaned: list[str] = []
    seen: set[str] = set()

    # 1. 기존 PATH 필터링
    for raw in path.split(sep):
        p = raw.strip()
        if not p:
            continue
        key = p.lower() if sys.platform == "win32" else p
        if key in seen:
            continue
        try:
            if Path(p).is_dir():
                cleaned.append(p)
                seen.add(key)
        except OSError:
            continue

    # 2+3. TeX + Perl bin dir prepend
    prepend: list[str] = []
    for d in _candidate_tex_bin_dirs() + _candidate_perl_bin_dirs():
        s = str(d)
        key = s.lower() if sys.platform == "win32" else s
        if key in seen:
            continue
        prepend.append(s)
        seen.add(key)

    env["PATH"] = sep.join(prepend + cleaned)
    return env


def _run_compile(
    tex_path: Path, timeout: int, log_cb: LogCb | None,
) -> tuple[bytes | None, str]:
    """Return (pdf_bytes or None, log_text)."""
    work_dir = tex_path.parent
    lmk = _resolve_latexmk()
    if lmk:
        cmd = [
            lmk, "-xelatex", "-interaction=nonstopmode",
            "-halt-on-error", "-file-line-error", tex_path.name,
        ]
        engine_desc = "latexmk-xelatex"
    else:
        xelatex = _resolve_xelatex()
        cmd = [
            xelatex, "-interaction=nonstopmode", "-halt-on-error",
            "-file-line-error", tex_path.name,
        ]
        engine_desc = "xelatex"

    stdout_acc: list[str] = []
    passes = 1 if lmk else 2  # latexmk 가 aux 반복 처리
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    subprocess_env = _sanitized_env()

    for pass_idx in range(passes):
        try:
            proc = subprocess.run(
                cmd, cwd=work_dir, timeout=timeout,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                creationflags=creationflags,
                env=subprocess_env,
            )
            stdout_acc.append(f"--- pass {pass_idx+1} / engine={engine_desc} ---")
            stdout_acc.append(proc.stdout or "")
            stdout_acc.append(proc.stderr or "")
        except subprocess.TimeoutExpired:
            stdout_acc.append(f"[TIMEOUT after {timeout}s]")
            return None, "\n".join(stdout_acc)

    # .log 파일이 더 정확.
    log_file = work_dir / (tex_path.stem + ".log")
    if log_file.exists():
        try:
            log_from_file = log_file.read_text(
                encoding="utf-8", errors="replace"
            )
            stdout_acc.append("--- .log ---")
            stdout_acc.append(log_from_file)
        except Exception:
            pass

    log_text = "\n".join(stdout_acc)

    pdf_path = work_dir / (tex_path.stem + ".pdf")
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        return pdf_path.read_bytes(), log_text
    return None, log_text


# ─── tex 조작 (frame / preamble 교체) ────────────────────────


def _extract_runtime_error(log_text: str) -> str:
    """파서가 실패했을 때 사용자에게 보일 raw error 발췌.

    우선순위:
    1. `Sorry, but ...` 블록 (MiKTeX 실행 실패 메시지)
    2. 마지막 non-empty 라인들 (끝 부분)
    """
    if not log_text:
        return ""
    lo = log_text.find("Sorry, but")
    if lo >= 0:
        return log_text[lo : lo + 1500]
    lines = [ln for ln in log_text.splitlines() if ln.strip()]
    tail = lines[-20:]
    return "\n".join(tail)


def _extract_frame(tex: str, slide_number: int) -> str:
    marker = f"% ── slide {slide_number} "
    start = tex.find(marker)
    if start < 0:
        return ""
    end_marker = "\\end{frame}"
    end = tex.find(end_marker, start)
    if end < 0:
        return tex[start:]
    return tex[start : end + len(end_marker)]


def _replace_frame(tex: str, slide_number: int, new_frame: str) -> str:
    old = _extract_frame(tex, slide_number)
    if not old:
        return tex
    return tex.replace(old, new_frame.rstrip() + "\n", 1)


def _extract_preamble(tex: str) -> str:
    idx = tex.find(r"\begin{document}")
    return tex[:idx] if idx >= 0 else tex


def _replace_preamble(tex: str, new_preamble: str) -> str:
    idx = tex.find(r"\begin{document}")
    if idx < 0:
        return new_preamble
    pre = new_preamble if new_preamble.endswith("\n") else new_preamble + "\n"
    return pre + tex[idx:]


# ─── 오케스트레이션 ──────────────────────────────────────────


def compile_with_repair(
    *,
    initial_tex: str,
    outline: LectureOutline,
    tex_path: Path,
    cfg: dict,
    log_cb: LogCb | None = None,
) -> tuple[bytes | None, str, RenderReport]:
    """Return (pdf_bytes or None, final_tex, report)."""
    max_iter = int(cfg.get("max_repair_iterations", 3))
    max_fail = int(cfg.get("max_frame_failures_before_skip", 2))
    timeout = int(cfg.get("latexmk_timeout", 180))
    model = cfg.get("codex_model", "gpt-5.4")
    reasoning = cfg.get("codex_reasoning_effort", "medium")
    repair_timeout = int(cfg.get("repair_timeout", 120))

    engine = "latexmk-xelatex" if _resolve_latexmk() else "xelatex"

    tex_path.write_text(initial_tex, encoding="utf-8")

    frame_fail_count: dict[int, int] = defaultdict(int)
    skipped: list[int] = []
    attempts: list[RepairAttempt] = []
    preamble_tried = False
    t_start = time.monotonic()

    for iteration in range(max_iter + 1):
        _raise_if_cancelled()
        _emit(log_cb, f"[render] compile iter {iteration+1}/{max_iter+1} (engine={engine})")
        pdf_bytes, log_text = _run_compile(tex_path, timeout, log_cb)

        if pdf_bytes is not None:
            status = "partial" if skipped else "success"
            final_tex = tex_path.read_text(encoding="utf-8")
            _emit(
                log_cb,
                f"[render] 컴파일 성공 — {len(pdf_bytes)} bytes, skipped={skipped}",
            )
            return pdf_bytes, final_tex, RenderReport(
                total_frames=len(outline.slides),
                compiled_frames=len(outline.slides) - len(skipped),
                skipped_frames=skipped,
                repair_attempts=attempts,
                final_status=status,
                engine=engine,
                pdf_size_bytes=len(pdf_bytes),
                duration_s=round(time.monotonic() - t_start, 2),
            )

        # 컴파일 실패 → 로그 파싱
        if iteration == max_iter:
            break

        current_tex = tex_path.read_text(encoding="utf-8")
        errors = parse_latex_log(log_text, current_tex)
        n_pre = sum(1 for e in errors if e.kind == "preamble")
        n_frame = sum(1 for e in errors if e.kind == "frame")
        n_unk = sum(1 for e in errors if e.kind == "unknown")
        _emit(
            log_cb,
            f"[render] 컴파일 실패 — errors: preamble={n_pre} frame={n_frame} unknown={n_unk}",
        )

        if not errors:
            # LaTeX 문법 에러가 아니라 MiKTeX 런타임/환경 문제 (PATH, 폰트, 권한
            # 등). log_text 앞부분을 dump 해서 사용자가 원인 볼 수 있게 한다.
            preview = _extract_runtime_error(log_text)
            _emit(
                log_cb,
                "[render] log 파싱 에러 없음 — LaTeX 문법 에러가 아닌 런타임/환경 문제로 보임",
            )
            if preview:
                for line in preview.splitlines()[:15]:
                    _emit(log_cb, f"[render]   | {line}")
            _emit(log_cb, "[render] 수리 중단")
            break

        made_progress = False

        # 1) preamble 에러 먼저 (iteration 0 에 한번만)
        if n_pre > 0 and not preamble_tried:
            preamble_tried = True
            _raise_if_cancelled()
            preamble_src = _extract_preamble(current_tex)
            err_excerpt = "\n\n".join(
                e.context for e in errors if e.kind == "preamble"
            )[:2000]
            t0 = time.monotonic()
            fixed_pre, note = repair_preamble(
                preamble_source=preamble_src,
                error_log=err_excerpt,
                model=model,
                reasoning_effort=reasoning,
                timeout=repair_timeout,
                log_cb=log_cb,
            )
            first_pre_err = next(
                (e for e in errors if e.kind == "preamble"), None
            )
            attempts.append(RepairAttempt(
                frame_index=None, iteration=iteration,
                error_excerpt=(first_pre_err.message[:200] if first_pre_err else ""),
                fixed=fixed_pre is not None,
                duration_s=round(time.monotonic() - t0, 2),
                note=note[:200],
            ))
            if fixed_pre:
                new_tex = _replace_preamble(current_tex, fixed_pre)
                tex_path.write_text(new_tex, encoding="utf-8")
                made_progress = True
                # preamble 고쳤으면 다음 iteration 으로 (frame 시도 전에 재컴파일)
                continue

        # 2) frame 에러 — frame_index 별로 1회
        frame_errs_by_idx: dict[int, LatexError] = {}
        for e in errors:
            if e.kind == "frame" and e.frame_index is not None:
                frame_errs_by_idx.setdefault(e.frame_index, e)

        # unknown 에러도 "가장 가까운 아직 처리 안 된 frame" 에 배정 시도 — 보수적으로 skip.

        current_tex = tex_path.read_text(encoding="utf-8")
        for idx, err in frame_errs_by_idx.items():
            _raise_if_cancelled()
            if idx in skipped:
                continue
            frame_fail_count[idx] += 1

            if frame_fail_count[idx] >= max_fail:
                _emit(
                    log_cb,
                    f"[render] slide {idx}: {max_fail}회 실패 → placeholder 교체",
                )
                placeholder = render_placeholder_frame(
                    idx,
                    outline.slides[idx - 1].title,
                    err.message,
                )
                current_tex = _replace_frame(current_tex, idx, placeholder)
                tex_path.write_text(current_tex, encoding="utf-8")
                skipped.append(idx)
                made_progress = True
                continue

            frame_src = _extract_frame(current_tex, idx)
            if not frame_src:
                _emit(
                    log_cb,
                    f"[render] slide {idx} frame 추출 실패 — skip",
                )
                continue

            t0 = time.monotonic()
            fixed, note = repair_frame(
                frame_source=frame_src,
                error_log=err.context,
                slide_meta=outline.slides[idx - 1],
                model=model,
                reasoning_effort=reasoning,
                timeout=repair_timeout,
                log_cb=log_cb,
            )
            attempts.append(RepairAttempt(
                frame_index=idx, iteration=iteration,
                error_excerpt=err.message[:200],
                fixed=fixed is not None,
                duration_s=round(time.monotonic() - t0, 2),
                note=note[:200],
            ))
            if fixed:
                current_tex = _replace_frame(current_tex, idx, fixed)
                tex_path.write_text(current_tex, encoding="utf-8")
                made_progress = True

        if not made_progress:
            _emit(log_cb, "[render] 이번 iteration 에서 수리 진전 없음 — 중단")
            break

    # 최종 실패
    final_tex = tex_path.read_text(encoding="utf-8")
    _emit(log_cb, f"[render] 최종 실패 — {len(attempts)}회 수리 시도, skipped={skipped}")
    return None, final_tex, RenderReport(
        total_frames=len(outline.slides),
        compiled_frames=0,
        skipped_frames=skipped,
        repair_attempts=attempts,
        final_status="failed",
        engine=engine,
        pdf_size_bytes=0,
        duration_s=round(time.monotonic() - t_start, 2),
    )
