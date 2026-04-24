"""pick_figures_for_slide 메인 오케스트레이터 + composite skill entry."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, current_cancel_event, load_skill

from _lib.generate_call import call_image_generation
from _lib.gemini_embed import set_rpm
from _lib.match import match_figure_for_slide
from _lib.preprocess import preprocess_assets
from _lib.schema import PickReport, SlidePickResult

logger = logging.getLogger("lecture_pipeline.pick_figures_for_slide")

LogCb = Callable[[str], None]


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
        raise CodexRunError("pick_figures_for_slide: 사용자 취소")


_SUPPORTED_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _discover_assets_and_md(
    outline_path: Path,
    source_stem: str,
) -> tuple[Path | None, Path | None, Path]:
    """outline.json 위치 + source_stem 으로 workspace / assets_dir / md_path 추정.

    레이아웃 가정:
      <workspace>/md_to_handout/<stem>-outline.json           # outline_path
      <workspace>/doc_to_md/<pdf_stem>-doc.md                 # md_path
      <workspace>/doc_to_md/<pdf_stem>-assets/                # assets_dir

    source_stem 은 md_path.stem (보통 `-doc` 로 끝남). pdf_stem 은 `-doc` 제거.
    """
    workspace = outline_path.parent.parent  # <workspace>
    md_root = workspace / "doc_to_md"

    # source_stem 기반 + `-doc` 제거 base stem 기반 둘 다 시도
    base_stem = source_stem[:-4] if source_stem.endswith("-doc") else source_stem

    md_candidates = [
        md_root / f"{source_stem}.md",
        md_root / f"{base_stem}-doc.md",
        md_root / f"{base_stem}.md",
    ]
    md_path = next((p for p in md_candidates if p.exists()), None)

    assets_candidates = [
        md_root / f"{base_stem}-assets",
        md_root / f"{source_stem}-assets",
        md_root / "assets",
    ]
    assets_dir = next((p for p in assets_candidates if p.is_dir()), None)

    return assets_dir, md_path, workspace


def _bullets_text(bullets: list) -> list[str]:
    out = []
    for b in bullets:
        if isinstance(b, dict):
            out.append(str(b.get("text", "")))
        elif hasattr(b, "text"):
            out.append(str(b.text))
        else:
            out.append(str(b))
    return out


def run_pick_figures(
    skill_dir: Path,
    input_paths: list[Path],
    log_callback: LogCb | None = None,
) -> dict[str, bytes]:
    """composite skill entry.

    입력: outline.json (md_to_handout 산출물) 하나.
    출력:
      - outline.with-figures.json  : figure_refs 채워진 outline
      - pick_report.json            : slide 별 decision 리포트
    (Tier 2 생성물들은 workspace/generated_figures/ 에 별도로 저장됨 — OutputWriter 경로와 독립)
    """
    log = log_callback

    specs = [p for p in input_paths if p.suffix.lower() == ".json"]
    if not specs:
        raise CodexRunError("pick_figures_for_slide: outline.json 입력 필요")
    outline_path = specs[0]

    try:
        outline = json.loads(outline_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CodexRunError(f"outline.json 파싱 실패: {exc}") from exc

    source_stem = outline.get("source_stem") or outline_path.stem
    slides = outline.get("slides", [])
    _emit(log, f"[pick] 시작 — slides={len(slides)}, source_stem={source_stem}")

    cfg = load_skill(skill_dir).config
    set_rpm(int(cfg.get("rpm", 0)))

    assets_dir, md_path, workspace = _discover_assets_and_md(
        outline_path, source_stem
    )
    if assets_dir is None:
        _emit(log, "[pick] assets 디렉토리 찾지 못함 — Tier 1 skip, 전부 Tier 2 또는 skip")
    else:
        _emit(
            log,
            f"[pick] assets={assets_dir.name}/ · md={md_path.name if md_path else '(없음)'}",
        )

    # ── Preprocessing (assets 있으면 only) ──
    preprocessed = None
    if assets_dir is not None:
        _raise_if_cancelled()
        preprocessed = preprocess_assets(
            assets_dir=assets_dir,
            md_path=md_path,
            output_dim=int(cfg.get("embed_output_dim", 1536)),
            force_refresh=bool(cfg.get("force_preprocess_refresh", False)),
            log_cb=log,
        )

    # ── 슬라이드별 처리 ──
    results: list[SlidePickResult] = []
    tier1_count = 0
    tier2_count = 0
    skipped = 0
    failed = 0
    with_hint = 0

    confidence_threshold = float(cfg.get("confidence_threshold", 0.65))
    max_refs = int(cfg.get("max_figures_per_slide", 2))
    top_k = int(cfg.get("top_k", 5))
    fallback_gen = bool(cfg.get("fallback_to_generation", True))
    gen_mode = cfg.get("generation_mode", "auto")
    gen_style = cfg.get("generation_style", "academic ML lecture, journal sans-serif")

    for slide in slides:
        _raise_if_cancelled()
        slide_num = int(slide.get("slide_number", 0))
        figure_hint = str(slide.get("figure_hint", "")).strip()
        title = str(slide.get("title", ""))
        bullets_text = _bullets_text(slide.get("bullets", []))

        t0 = time.monotonic()

        # figure_hint 없으면 skip
        if not figure_hint:
            results.append(SlidePickResult(
                slide_number=slide_num,
                tier="skip_no_hint",
                figure_refs=[],
                confidence=0.0,
                reasoning="no figure_hint",
                duration_s=round(time.monotonic() - t0, 2),
            ))
            skipped += 1
            continue
        with_hint += 1

        # ── Tier 1: assets 매칭 ──
        tier1_refs: list[str] = []
        tier1_conf = 0.0
        tier1_reason = ""
        if preprocessed is not None and preprocessed["filenames"]:
            try:
                match = match_figure_for_slide(
                    slide_title=title,
                    slide_bullets=bullets_text,
                    figure_hint=figure_hint,
                    asset_embeddings=preprocessed["embeddings"],
                    asset_names=preprocessed["filenames"],
                    captions=preprocessed["captions"],
                    top_k=top_k,
                    max_refs=max_refs,
                    embed_output_dim=int(cfg.get("embed_output_dim", 1536)),
                    log_cb=log,
                )
                tier1_refs = match.matches
                tier1_conf = match.confidence
                tier1_reason = match.reasoning
            except Exception as exc:
                _emit(log, f"[pick] slide {slide_num} Tier 1 실패: {exc}")
                failed += 1

        # confidence 충분하면 Tier 1 채택
        if tier1_refs and tier1_conf >= confidence_threshold:
            # figure_refs 는 assets/<filename> 상대 경로로 기록
            if assets_dir is not None:
                try:
                    rel_assets = assets_dir.relative_to(workspace)
                    tier1_refs = [str(rel_assets / r).replace("\\", "/") for r in tier1_refs]
                except ValueError:
                    tier1_refs = [str(assets_dir / r).replace("\\", "/") for r in tier1_refs]

            slide["figure_refs"] = tier1_refs
            results.append(SlidePickResult(
                slide_number=slide_num,
                tier="tier1_match",
                figure_refs=tier1_refs,
                confidence=tier1_conf,
                reasoning=tier1_reason,
                duration_s=round(time.monotonic() - t0, 2),
            ))
            tier1_count += 1
            _emit(log, f"[pick] slide {slide_num}: Tier 1 ✓ conf={tier1_conf:.2f} → {tier1_refs}")
            continue

        # ── Tier 2: 생성 ──
        if not fallback_gen:
            results.append(SlidePickResult(
                slide_number=slide_num,
                tier="skip_no_hint",  # low confidence + no fallback = 사실상 skip
                figure_refs=[],
                confidence=tier1_conf,
                reasoning=f"tier1 low conf ({tier1_conf:.2f}) and fallback disabled",
                duration_s=round(time.monotonic() - t0, 2),
            ))
            skipped += 1
            continue

        try:
            gen_out_dir = workspace / "generated_figures"
            gen_out_dir.mkdir(parents=True, exist_ok=True)
            slot_name = f"slide_{slide_num:03d}"
            slot_dir = gen_out_dir / slot_name
            # 기본 format: png (TikZ 는 slide.layout 이 diagram 성격이면 tex 로)
            gen_format = cfg.get("default_generation_format", "png")

            result = call_image_generation(
                hint=figure_hint,
                context=f"{title}\n" + "\n".join(f"- {b}" for b in bullets_text[:6]),
                style=gen_style,
                format=gen_format,
                mode=gen_mode,
                seed=slide_num,
                workspace=workspace,
                out_dir=slot_dir,
                log_cb=log,
            )

            # 생성된 메인 이미지/소스 경로 → outline 에 상대경로로 기록
            written = result.get("written", [])
            main_rel = None
            # 우선순위: fig.png > fig.source.tex > fig.source.py
            preferred = ["fig.png", "fig.source.tex", "fig.source.py"]
            for pref in preferred:
                for w in written:
                    if w.endswith(pref):
                        wp = Path(w)
                        try:
                            main_rel = str(wp.relative_to(workspace)).replace("\\", "/")
                        except ValueError:
                            main_rel = str(wp).replace("\\", "/")
                        break
                if main_rel:
                    break

            refs = [main_rel] if main_rel else []
            slide["figure_refs"] = refs

            results.append(SlidePickResult(
                slide_number=slide_num,
                tier="tier2_generate",
                figure_refs=refs,
                confidence=1.0,  # 생성은 요청에 부합한다고 가정
                reasoning=(
                    f"tier1 conf {tier1_conf:.2f} < threshold — "
                    f"generated ({result.get('mode_used')}, cache_hit={result.get('cache_hit')})"
                ),
                duration_s=round(time.monotonic() - t0, 2),
            ))
            tier2_count += 1
            _emit(
                log,
                f"[pick] slide {slide_num}: Tier 2 ✓ mode={result.get('mode_used')} "
                f"→ {refs}",
            )
        except Exception as exc:
            results.append(SlidePickResult(
                slide_number=slide_num,
                tier="failed",
                figure_refs=[],
                confidence=tier1_conf,
                reasoning=tier1_reason,
                duration_s=round(time.monotonic() - t0, 2),
                error=str(exc)[:400],
            ))
            failed += 1
            _emit(log, f"[pick] slide {slide_num}: Tier 2 실패 — {exc}")

    # ── 리포트 조립 ──
    report = PickReport(
        total_slides=len(slides),
        with_figure_hint=with_hint,
        tier1_matched=tier1_count,
        tier2_generated=tier2_count,
        skipped=skipped,
        failed=failed,
        per_slide=results,
        preprocess={
            "n_images": len(preprocessed["filenames"]) if preprocessed else 0,
            "signature": (preprocessed or {}).get("meta", {}).get("signature"),
        },
    )

    _emit(
        log,
        f"[pick] 완료 — hint={with_hint} T1={tier1_count} T2={tier2_count} "
        f"skip={skipped} fail={failed}",
    )

    # ── 산출물 ──
    outline_json = json.dumps(outline, ensure_ascii=False, indent=2).encode("utf-8")
    report_json = report.model_dump_json(indent=2).encode("utf-8")

    return {
        "outline.with-figures.json": outline_json,
        "pick_report.json": report_json,
    }
