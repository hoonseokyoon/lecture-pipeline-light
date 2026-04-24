"""md_to_handout composite skill entry.

Markdown (doc_to_md 산출물) + sibling assets → Gemini 3 Pro 로 강의 outline JSON
생성. density 규칙 (핸드아웃 < 대본 < 원문) 은 prompt.txt + Pydantic schema 에서
강제됨.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from google.genai import types

from codex_runner import CodexRunError, current_cancel_event, load_skill

from _lib.gemini_backend import (
    _prepare_image_part,
    count_tokens_for,
    run_gemini_structured,
    set_rpm,
)
from _lib.schema import LectureOutline

logger = logging.getLogger("lecture_pipeline.md_to_handout")

LogCb = Callable[[str], None]

_SUPPORTED_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _emit(log: LogCb | None, msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


def _is_cancelled() -> bool:
    ev = current_cancel_event.get()
    return ev is not None and ev.is_set()


def _raise_if_cancelled() -> None:
    if _is_cancelled():
        raise CodexRunError("md_to_handout: 사용자 취소")


def _discover_assets(md_path: Path) -> tuple[Path | None, list[Path]]:
    """Markdown 파일 옆에서 figure assets 디렉토리를 찾는다.

    doc_to_md 는 `output_rename` 에서 `"doc.md": "{stem}-doc.md"` 로 md 파일에
    `-doc` 접미를 붙이는 반면, assets 디렉토리는 내부적으로 `"{pdf_stem}-assets"`
    (접미 없음) 로 생성한다. 그래서 md_stem 에서 `-doc` 를 벗긴 base 로도 찾는다.

    탐색 순서:
    1. `<md_stem without -doc>-assets/` — doc_to_md 기본 산출물 규약
    2. `<md_stem>-assets/`               — stem 그대로
    3. `assets/`                          — 일반적인 규약
    """
    stem = md_path.stem
    base_stem = stem[:-4] if stem.endswith("-doc") else stem

    candidates = [
        md_path.parent / f"{base_stem}-assets",
        md_path.parent / f"{stem}-assets",
        md_path.parent / "assets",
    ]
    # 중복 경로 제거 (base_stem == stem 인 경우)
    seen: set[Path] = set()
    unique_cands: list[Path] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique_cands.append(c)

    for cand in unique_cands:
        if cand.is_dir():
            imgs: list[Path] = []
            for ext in _SUPPORTED_IMAGE_EXTS:
                imgs.extend(cand.rglob(f"*{ext}"))
            imgs.sort()
            return cand, imgs
    return None, []


def _build_task_instruction(cfg: dict) -> str:
    """system_instruction 뒤에 붙는 런타임 task 메시지."""
    target = int(cfg.get("target_minutes", 60))
    return (
        "위 교재 챕터 (+ 첨부된 figure 이미지) 를 기반으로 `LectureOutline` "
        f"JSON 을 생성하세요. 목표 강의 시간: {target}분. "
        "텍스트 밀도 규칙 엄수. schema 위반 시 응답이 거부됩니다."
    )


def run_md_to_handout(
    skill_dir: Path,
    input_paths: list[Path],
    log_callback: LogCb | None = None,
) -> dict[str, bytes]:
    """Composite skill entry. `batch=false` — Markdown 1개 단위로 호출됨."""
    log = log_callback

    mds = [p for p in input_paths if p.suffix.lower() == ".md"]
    if not mds:
        raise CodexRunError("md_to_handout: Markdown(.md) 입력 필요")
    if len(mds) > 1:
        _emit(log, f"[md_to_handout] 경고: .md {len(mds)}개 — 첫 번째만 처리")
    md_path = mds[0]

    cfg = load_skill(skill_dir).config
    model = cfg.get("model", "gemini-pro-latest")
    thinking_level = cfg.get("thinking_level", "high")
    temperature = float(cfg.get("temperature", 1.0))
    seed = cfg.get("seed")
    max_output_tokens = int(cfg.get("max_output_tokens", 32000))
    media_resolution = cfg.get("media_resolution", "media_resolution_high")
    include_figures = bool(cfg.get("include_figures", True))
    max_figures = int(cfg.get("max_figures", 20))
    figure_max_dim = int(cfg.get("figure_max_dim", 1024))
    token_abort = int(cfg.get("token_abort_threshold", 900000))
    emit_preview = bool(cfg.get("emit_preview", True))
    rpm = int(cfg.get("rpm", 0))
    set_rpm(rpm)

    size_kb = md_path.stat().st_size / 1024.0
    _emit(log, f"[md_to_handout] 시작: {md_path.name} ({size_kb:.1f} KB)")
    _raise_if_cancelled()

    # ── assets 수집 ──
    assets_dir, all_imgs = _discover_assets(md_path)
    figure_parts: list[types.Part] = []
    selected_figures: list[Path] = []
    if assets_dir is not None:
        _emit(
            log,
            f"[md_to_handout] assets 발견: {assets_dir.name}/ ({len(all_imgs)}개)",
        )
    else:
        _emit(log, "[md_to_handout] assets 디렉토리 없음 — text-only")

    if include_figures and all_imgs:
        selected_figures = all_imgs[:max_figures]
        if len(selected_figures) < len(all_imgs):
            _emit(
                log,
                f"[md_to_handout] figures={len(selected_figures)} "
                f"(of {len(all_imgs)} available, capped at {max_figures})",
            )
        else:
            _emit(log, f"[md_to_handout] figures={len(selected_figures)}")
        for fig in selected_figures:
            _raise_if_cancelled()
            try:
                figure_parts.append(
                    _prepare_image_part(fig, max_dim=figure_max_dim)
                )
            except Exception as exc:
                _emit(
                    log,
                    f"[md_to_handout] figure 건너뜀 ({fig.name}): {exc}",
                )
    elif not include_figures:
        _emit(log, "[md_to_handout] figures 비활성 (include_figures=false)")

    # ── parts 조립 ──
    md_text = md_path.read_text(encoding="utf-8")
    system_instruction = (skill_dir / "prompt.txt").read_text(encoding="utf-8")
    task_instruction = _build_task_instruction(cfg)

    parts: list[types.Part] = [types.Part.from_text(text=md_text)]
    parts.extend(figure_parts)
    parts.append(types.Part.from_text(text=task_instruction))

    # ── 사전 token 체크 ──
    if token_abort > 0:
        _raise_if_cancelled()
        tokens = count_tokens_for(
            model=model,
            parts=parts,
            system_instruction=system_instruction,
        )
        if tokens > 0:
            _emit(
                log,
                f"[md_to_handout] tokens={tokens} (threshold={token_abort})",
            )
            if tokens > token_abort:
                raise CodexRunError(
                    f"md_to_handout: 입력 토큰 {tokens} > threshold "
                    f"{token_abort}. config.token_abort_threshold 상향 또는 "
                    f"max_figures 하향 필요."
                )

    # ── Gemini 호출 ──
    _raise_if_cancelled()
    _emit(
        log,
        f"[md_to_handout] Gemini 호출 (model={model}, thinking={thinking_level})",
    )
    parsed, usage = run_gemini_structured(
        model=model,
        system_instruction=system_instruction,
        parts=parts,
        response_model=LectureOutline,
        thinking_level=thinking_level,
        temperature=temperature,
        seed=seed,
        max_output_tokens=max_output_tokens,
        media_resolution=media_resolution,
        log_cb=log,
    )
    _emit(log, f"[md_to_handout] usage={usage}")

    # ── 밀도 통계 + source_stem 주입 ──
    assert isinstance(parsed, LectureOutline)
    parsed = parsed.model_copy(update={"source_stem": md_path.stem})

    stats = parsed.bullet_stats()
    _emit(
        log,
        f"[md_to_handout] parsed OK — slides={stats['slides']}, "
        f"bullets_total={stats['bullets_total']}, "
        f"bullets_mean={stats['bullets_mean']:.2f}, "
        f"bullet_chars_mean={stats['bullet_chars_mean']:.1f}, "
        f"empty_slides={stats['empty_slides']}",
    )

    # ── 산출물 직렬화 ──
    outputs: dict[str, bytes] = {
        "outline.json": parsed.model_dump_json(
            indent=2, exclude_none=False,
        ).encode("utf-8"),
    }
    if emit_preview:
        outputs["outline.preview.md"] = parsed.to_preview_md().encode("utf-8")

    _emit(
        log,
        f"[md_to_handout] 완료 — {len(outputs)}개 파일 반환",
    )
    return outputs
