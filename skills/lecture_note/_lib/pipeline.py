"""lecture_note composite skill의 메인 orchestrator.

`skill.py`에서 `run = run_pipeline`로 노출되고, codex_runner.run_skill이
Skill.run을 발견하면 이 함수를 호출.

각 step 결과를 checkpoint에 저장해 재실행 시 이어서 진행.
"""

import json
import shutil
from pathlib import Path
from typing import Any, Callable

from codex_runner import CodexRunError, load_skill, run_skill

from _lib.align import align_pages_batched
from _lib.batching import compute_run_id
from _lib.checkpoint import CheckpointDir
from _lib.compose import compose_pages_parallel, merge_pages
from _lib.exporter import build_note_html, build_note_json, render_pdf_pages_base64
from _lib.lecture_summary import generate_lecture_summary
from _lib.numbering import number_transcripts
from _lib.polish import polish_pages
from _lib.reconcile import reconcile_orphans
from _lib.review import review_alignment
from _lib.slides_flatten import flatten_slides


CACHE_VERSION = "v4"


def _invalidate_stale_cache(
    cache_dir: Path,
    from_version: str,
    log_callback,
) -> None:
    """이전 버전 cache를 현재로 업그레이드.

    - v1 → v2: reconcile 포맷 변경
    - v2 → v3: compose 프롬프트 변경 + step1b/step5b 추가
    - v3 → v4: multi-PDF slides_data 포맷 변경 + compute_run_id 순서 민감화
    - 누적 적용 (v1→v4 는 세 단계 모두 적용)
    """
    removed: list[str] = []
    files_to_remove: set[str] = set()
    dirs_to_remove: set[str] = set()

    if from_version == "v1":
        files_to_remove.update({
            "step2b_reviewed.json",
            "step3_mapping.json",
        })

    if from_version in ("v1", "v2"):
        files_to_remove.update({
            "step6_note.md",
            "step7_note.json",
            "step8_note.html",
        })
        dirs_to_remove.add("step5_pages")
        dirs_to_remove.add("step5b_polished")

    if from_version in ("v1", "v2", "v3"):
        # v4: slides_data 포맷 변경 → 사실상 전 파이프라인 재계산
        files_to_remove.update({
            "step1_slides.json",
            "step1b_summary.json",
            "step2_alignments.json",
            "step2b_reviewed.json",
            "step3_mapping.json",
            "step4_glossary.md",
            "step6_note.md",
            "step7_note.json",
            "step8_note.html",
            "numbered_meta.json",
        })
        dirs_to_remove.update({
            "step1_slides",
            "step2_batches",
            "step5_pages",
            "step5b_polished",
            "numbered",
        })

    for name in sorted(files_to_remove):
        p = cache_dir / name
        if p.exists():
            p.unlink()
            removed.append(name)
    for dirname in sorted(dirs_to_remove):
        d = cache_dir / dirname
        if d.exists():
            try:
                shutil.rmtree(d)
                removed.append(f"{dirname}/")
            except Exception as exc:
                _emit(log_callback, f"[pipeline] {dirname}/ 삭제 실패: {exc}")

    if removed:
        _emit(
            log_callback,
            f"[pipeline] stale cache 제거 ({from_version}→{CACHE_VERSION}): "
            f"{', '.join(removed)}",
        )


def _emit(log_callback: Callable[[str], None] | None, msg: str) -> None:
    if log_callback is None:
        return
    try:
        log_callback(msg)
    except Exception:
        pass


def run_pipeline(
    skill_dir: Path,
    input_paths: list[Path],
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, bytes]:
    """Composite pipeline entry point. Returns {"note.md": bytes}."""

    # 입력 순서 보존 (GUI의 FileOrderDialog가 이미 강의·교시 순서를 결정).
    pdfs = [p for p in input_paths if p.suffix.lower() == ".pdf"]
    txts = [p for p in input_paths if p.suffix.lower() == ".txt"]

    if not pdfs:
        raise CodexRunError("lecture_note: PDF 입력 필요 (1개 이상)")
    if not txts:
        raise CodexRunError("lecture_note: transcript(.txt) 입력 필요 (1개 이상)")
    _emit(
        log_callback,
        f"[pipeline] PDF {len(pdfs)}개 + transcript {len(txts)}개 입력",
    )

    # Config 로드 (skill_dir의 config.json)
    skill = load_skill(skill_dir)
    cfg = skill.config
    model = cfg.get("model", "gpt-5.4")
    align_effort = cfg.get("align_reasoning_effort", cfg.get("reasoning_effort", "high"))
    compose_effort = cfg.get("compose_reasoning_effort", cfg.get("reasoning_effort", "high"))
    service_tier = cfg.get("service_tier", "default")
    timeout = int(cfg.get("timeout", 2400))
    batch_size = int(cfg.get("batch_size", 12))
    overlap = int(cfg.get("overlap", 3))
    compose_workers = int(cfg.get("compose_parallel", 8))

    # Run id + checkpoint dir
    run_id = compute_run_id(input_paths)
    cache = CheckpointDir("lecture_note", run_id)
    cache.write_inputs_manifest(input_paths)
    _emit(log_callback, f"[pipeline] run_id={run_id}")
    _emit(log_callback, f"[pipeline] checkpoint: {cache.path}")

    # Cache version 체크
    version_file = cache.path / "_cache_version.txt"
    current_version = (
        version_file.read_text(encoding="utf-8").strip()
        if version_file.exists()
        else "v1"
    )
    if current_version != CACHE_VERSION:
        _emit(
            log_callback,
            f"[pipeline] cache version {current_version} → {CACHE_VERSION}",
        )
        _invalidate_stale_cache(cache.path, current_version, log_callback)
        version_file.write_text(CACHE_VERSION, encoding="utf-8")

    # ── Step 0: number transcript lines ──
    numbered_dir = cache.path / "numbered"
    numbered_meta_path = cache.path / "numbered_meta.json"
    if numbered_meta_path.exists():
        numbered_meta_raw = json.loads(numbered_meta_path.read_text(encoding="utf-8"))
        numbered: dict[str, dict] = {
            name: {"path": Path(info["path"]), "line_count": info["line_count"]}
            for name, info in numbered_meta_raw.items()
        }
        _emit(log_callback, "[step0] numbered 캐시 로드")
    else:
        _emit(log_callback, "[step0] transcript 라인번호 부여 중...")
        numbered = number_transcripts(txts, numbered_dir)
        numbered_meta_path.write_text(
            json.dumps(
                {name: {"path": str(info["path"]), "line_count": info["line_count"]}
                 for name, info in numbered.items()},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        _emit(log_callback, f"[step0] 완료 — {len(numbered)}개 파일")

    # ── Step 1: slides_textify (PDF별 개별 실행 후 flatten) ──
    def _run_textify_all() -> dict:
        per_pdf_dir = cache.subdir("step1_slides")
        per_pdf_results: list[tuple[Path, dict]] = []
        for i, pdf_path in enumerate(pdfs):
            ckpt = per_pdf_dir / f"{i:02d}_{pdf_path.stem}.json"
            if ckpt.exists():
                raw = json.loads(ckpt.read_text(encoding="utf-8"))
                _emit(
                    log_callback,
                    f"[step1] {pdf_path.name} checkpoint 로드",
                )
            else:
                _emit(
                    log_callback,
                    f"[step1] slides_textify 실행: {pdf_path.name} "
                    f"({i + 1}/{len(pdfs)})",
                )
                out = run_skill(
                    str(skill_dir.parent / "slides_textify"),
                    [pdf_path],
                    log_callback=log_callback,
                )
                raw = json.loads(out.get("pages.json", b"").decode("utf-8"))
                ckpt.write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            per_pdf_results.append((pdf_path, raw))
        return flatten_slides(per_pdf_results)

    slides_data = cache.get_or_compute_json("step1_slides.json", _run_textify_all)
    num_pages = len(slides_data.get("pages", []))
    sources_info = slides_data.get("sources", [])
    _emit(
        log_callback,
        f"[step1] 완료 — {num_pages} 페이지 (PDF {len(sources_info)}개 합침)",
    )

    # ── Step 1b: lecture_summary (전역 context) ──
    def _run_summary() -> dict:
        return generate_lecture_summary(
            slides_data=slides_data,
            numbered_txts=numbered,
            model=model,
            reasoning_effort=align_effort,
            service_tier=service_tier,
            timeout=timeout,
            log_callback=log_callback,
        )

    lecture_summary = cache.get_or_compute_json("step1b_summary.json", _run_summary)
    _emit(
        log_callback,
        f"[step1b] 완료 — {len(lecture_summary.get('key_mechanisms', []))} mechanisms",
    )

    # ── Step 2: align pages (batched) ──
    def _run_align() -> dict[str, list]:
        batch_ckpt_dir = cache.subdir("step2_batches")
        assignments_int = align_pages_batched(
            slides_data=slides_data,
            numbered_txts=numbered,
            pdf_paths=pdfs,
            batch_ckpt_dir=batch_ckpt_dir,
            batch_size=batch_size,
            overlap=overlap,
            model=model,
            reasoning_effort=align_effort,
            service_tier=service_tier,
            timeout=timeout,
            log_callback=log_callback,
        )
        return {str(k): v for k, v in assignments_int.items()}

    assignments_raw = cache.get_or_compute_json("step2_alignments.json", _run_align)
    # int key로 복원
    assignments: dict[int, list[dict]] = {
        int(k): v for k, v in assignments_raw.items()
    }
    _emit(log_callback, f"[step2] 완료 — {len(assignments)}페이지 배정")

    # ── Step 2b: LLM review pass (monotonicity 위반이 있을 때만) ──
    def _run_review() -> dict[str, list]:
        reviewed_int = review_alignment(
            assignments=assignments,
            slides_data=slides_data,
            numbered_txts=numbered,
            model=model,
            reasoning_effort=align_effort,
            service_tier=service_tier,
            timeout=timeout,
            log_callback=log_callback,
        )
        return {str(k): v for k, v in reviewed_int.items()}

    reviewed_raw = cache.get_or_compute_json("step2b_reviewed.json", _run_review)
    assignments_reviewed: dict[int, list[dict]] = {
        int(k): v for k, v in reviewed_raw.items()
    }
    _emit(
        log_callback,
        f"[step2b] 완료 — {len(assignments_reviewed)}페이지 (리뷰 반영)",
    )

    # ── Step 3: reconcile orphans (Python + LLM 하이브리드) ──
    def _run_reconcile() -> dict:
        res = reconcile_orphans(
            assignments=assignments_reviewed,
            numbered_txts=numbered,
            slides_data=slides_data,
            model=model,
            reasoning_effort=align_effort,
            service_tier=service_tier,
            timeout=timeout,
            log_callback=log_callback,
        )
        return {
            "mapping": {str(k): v for k, v in res["mapping"].items()},
            "unassigned": res["unassigned"],
        }

    step3 = cache.get_or_compute_json("step3_mapping.json", _run_reconcile)
    mapping: dict[int, list[dict]] = {int(k): v for k, v in step3["mapping"].items()}
    unassigned: list[dict] = step3.get("unassigned", [])
    _emit(log_callback, f"[step3] 완료 — unassigned {len(unassigned)}구간")

    # ── Step 4: glossary (기존 skill 재사용) ──
    def _run_glossary() -> bytes:
        _emit(log_callback, "[step4] glossary 실행 중...")
        out = run_skill(
            str(skill_dir.parent / "glossary"),
            pdfs + txts,
            log_callback=log_callback,
        )
        return out.get("glossary.md", b"")

    glossary_md = cache.get_or_compute_bytes("step4_glossary.md", _run_glossary)
    _emit(log_callback, f"[step4] 완료 — glossary {len(glossary_md)} bytes")

    # ── Step 5: compose pages (parallel) ──
    _emit(log_callback, f"[step5] 페이지별 compose 시작 (병렬 {compose_workers}, semaphore 4)...")
    page_notes = compose_pages_parallel(
        slides_data=slides_data,
        mapping=mapping,
        numbered_txts=numbered,
        glossary_md=glossary_md,
        lecture_summary=lecture_summary,
        pages_dir=cache.subdir("step5_pages"),
        model=model,
        reasoning_effort=compose_effort,
        service_tier=service_tier,
        timeout=timeout,
        max_workers=compose_workers,
        log_callback=log_callback,
    )
    _emit(log_callback, f"[step5] 완료 — {len(page_notes)}/{num_pages}페이지")

    # ── Step 5b: 전체 페이지 polish (단일 Codex 호출, 파일 편집 기반) ──
    _emit(log_callback, "[step5b] 전체 폴리시 호출...")
    page_notes = polish_pages(
        page_notes=page_notes,
        slides_data=slides_data,
        lecture_summary=lecture_summary,
        polish_cache_dir=cache.subdir("step5b_polished"),
        model=model,
        reasoning_effort=compose_effort,
        service_tier=service_tier,
        timeout=timeout,
        log_callback=log_callback,
    )

    # ── Step 6: merge ──
    def _run_merge() -> bytes:
        _emit(log_callback, "[step6] 최종 merge...")
        final_md = merge_pages(
            slides_data=slides_data,
            page_notes=page_notes,
            glossary_md=glossary_md,
            unassigned=unassigned,
            numbered_txts=numbered,
            mapping=mapping,
        )
        return final_md.encode("utf-8")

    final_bytes = cache.get_or_compute_bytes("step6_note.md", _run_merge)
    _emit(log_callback, f"[step6] 완료 — note.md {len(final_bytes)} bytes")

    # ── Step 7: 구조화 JSON (Python 재처리용) ──
    def _run_export_json() -> bytes:
        _emit(log_callback, "[step7] note.json 생성...")
        note_data = build_note_json(
            slides_data=slides_data,
            mapping=mapping,
            numbered_txts=numbered,
            glossary_md=glossary_md,
            unassigned=unassigned,
            page_notes=page_notes,
            run_id=run_id,
        )
        return json.dumps(note_data, ensure_ascii=False, indent=2).encode("utf-8")

    note_json_bytes = cache.get_or_compute_bytes("step7_note.json", _run_export_json)
    _emit(log_callback, f"[step7] 완료 — note.json {len(note_json_bytes)} bytes")

    # ── Step 8: self-contained HTML (목차 + 페이지 이미지 + 섹션) ──
    def _run_export_html() -> bytes:
        _emit(log_callback, "[step8] PDF 페이지 이미지 렌더 + HTML 빌드...")
        note_data = json.loads(note_json_bytes.decode("utf-8"))
        pdf_images = render_pdf_pages_base64(
            pdfs, slides_data.get("sources", []), log_callback=log_callback,
        )
        html_str = build_note_html(note_data, pdf_images)
        return html_str.encode("utf-8")

    note_html_bytes = cache.get_or_compute_bytes("step8_note.html", _run_export_html)
    _emit(log_callback, f"[step8] 완료 — note.html {len(note_html_bytes)} bytes")

    return {
        "note.md": final_bytes,
        "note.json": note_json_bytes,
        "note.html": note_html_bytes,
    }
