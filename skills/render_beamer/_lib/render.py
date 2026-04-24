"""render_beamer composite skill 메인 entry.

입력: `<stem>-outline.with-figures.json` (pick_figures_for_slide 산출).
산출:
- doc.tex              : 최종 LaTeX source (수리 후)
- doc.pdf              : 컴파일 성공 시
- render-log.json      : RenderReport (per-frame 수리 기록)
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, current_cancel_event, load_skill

from _lib.compile_loop import compile_with_repair
from _lib.figure_resolver import (
    collect_missing,
    collect_tikz_libraries,
    resolve_figures,
)
from _lib.schema import LectureOutline
from _lib.templates import render_frame, render_preamble

logger = logging.getLogger("lecture_pipeline.render_beamer")

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


def _guess_workspace(outline_path: Path) -> Path:
    """<workspace>/pick_figures/<stem>-*.json 배치를 기본 가정.

    parent.parent 가 workspace. 그 외 배치면 사용자가 input 경로 그대로 두는
    수밖에 없는데, 최소한 parent 가 workspace 일 가능성도 체크 (doc_to_md 등이
    같이 있으면 OK).
    """
    cand1 = outline_path.parent.parent
    if (cand1 / "doc_to_md").exists() or (cand1 / "md_to_handout").exists():
        return cand1
    cand2 = outline_path.parent
    if (cand2 / "doc_to_md").exists() or (cand2 / "md_to_handout").exists():
        return cand2
    return cand1  # 없으면 default


def run_render_beamer(
    skill_dir: Path,
    input_paths: list[Path],
    log_callback: LogCb | None = None,
) -> dict[str, bytes]:
    log = log_callback
    t_start = time.monotonic()

    jsons = [p for p in input_paths if p.suffix.lower() == ".json"]
    if not jsons:
        raise CodexRunError(
            "render_beamer: outline.with-figures.json 입력 필요"
        )
    if len(jsons) > 1:
        _emit(log, f"[render] 경고: json {len(jsons)}개 — 첫 번째만 처리")
    outline_path = jsons[0]

    try:
        outline = LectureOutline.model_validate_json(
            outline_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise CodexRunError(
            f"outline json 파싱 실패 ({outline_path.name}): {exc}"
        ) from exc

    cfg = load_skill(skill_dir).config
    workspace = _guess_workspace(outline_path)

    _emit(
        log,
        f"[render] 시작 — slides={len(outline.slides)}, "
        f"stem={outline.source_stem}, workspace={workspace}",
    )
    _raise_if_cancelled()

    # 1. figure resolve
    resolved = resolve_figures(outline.slides, workspace)
    missing = collect_missing(resolved)
    tikz_libs = collect_tikz_libraries(resolved)
    n_image = sum(1 for r in resolved.values() if r.kind == "image")
    n_tikz = sum(1 for r in resolved.values() if r.kind == "tikz")
    _emit(
        log,
        f"[render] figures: image={n_image} tikz={n_tikz} missing={len(missing)} "
        f"(tikz libs: {sorted(tikz_libs)})",
    )
    if missing:
        for ref in missing[:5]:
            _emit(log, f"[render] figure missing: {ref}")
        if len(missing) > 5:
            _emit(log, f"[render] … {len(missing)-5}개 더 생략")

    # 2. preamble + frame 조립
    hangul_font = cfg.get("hangul_font", "NanumGothic")
    aspect_ratio = cfg.get("aspect_ratio", "16:9")
    emit_notes = bool(cfg.get("emit_notes", True))

    preamble = render_preamble(
        chapter_title=outline.chapter_title,
        workspace_abs=workspace.resolve(),
        hangul_font=hangul_font,
        aspect_ratio=aspect_ratio,
        tikz_libraries=tikz_libs,
    )

    frames_tex = "\n".join(
        render_frame(slide, resolved, emit_notes) for slide in outline.slides
    )

    full_tex = preamble + frames_tex + "\n\\end{document}\n"
    _emit(log, f"[render] tex 조립 완료 — {len(full_tex)} chars")

    _raise_if_cancelled()

    # 3. 임시 dir 에서 컴파일 + 수리 loop
    with tempfile.TemporaryDirectory(prefix="render_beamer_") as td:
        td_path = Path(td)
        tex_path = td_path / "doc.tex"
        pdf_bytes, final_tex, report = compile_with_repair(
            initial_tex=full_tex,
            outline=outline,
            tex_path=tex_path,
            cfg=cfg,
            log_cb=log,
        )

    dt = time.monotonic() - t_start
    _emit(
        log,
        f"[render] 완료 — status={report.final_status}, "
        f"compiled={report.compiled_frames}/{report.total_frames}, "
        f"skipped={report.skipped_frames}, repairs={len(report.repair_attempts)} "
        f"({dt:.1f}s)",
    )

    # 4. 산출
    # missing figure 정보를 report 에 포함 (로그 이후 뮤테이트는 비권장이지만
    # render.py 가 최종 소비자라 여기서 세팅)
    report = report.model_copy(update={"missing_figures": missing})

    outputs: dict[str, bytes] = {
        "doc.tex": final_tex.encode("utf-8"),
        "render-log.json": report.model_dump_json(indent=2).encode("utf-8"),
    }
    if pdf_bytes is not None:
        outputs["doc.pdf"] = pdf_bytes
    else:
        _emit(log, "[render] PDF 미생성 — .tex 와 log 만 저장")

    return outputs
