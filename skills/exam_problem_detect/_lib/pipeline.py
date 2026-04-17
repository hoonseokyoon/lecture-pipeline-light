"""exam_problem_detect 메인 orchestrator.

per-page 처리:
  1. multi-box LLM 반복 호출 → incremental clustering (page_detect)
  2. 각 cluster: percentile 3-후보 산출 → LLM pick 대표 (pick)
  3. 시스템 결정론적 번호 부여 (numbering)
  4. annotated PNG 렌더 (render)

전체:
  - problems.json / clusters.json / bridge_boxes.json 조립
  - pages/page_NNN.png 출력
  - page_parallel > 1이면 ContextThreadPoolExecutor로 페이지 병렬화
"""

from __future__ import annotations

import io
import json
from concurrent.futures import as_completed
from pathlib import Path
from typing import Callable

from PIL import Image

from codex_runner import CodexRunError, ContextThreadPoolExecutor, load_skill

from _lib import image_utils as iu
from _lib.clustering import BridgeBox, Cluster
from _lib.numbering import assign_ids
from _lib.page_detect import detect_page_sampling
from _lib.percentile import compute_candidates
from _lib.pick import pick_representative
from _lib.render import render_page_annotated


LogCb = Callable[[str], None]


# ─────────────────────── 입력 로딩 ───────────────────────

def _load_pages(
    input_paths: list[Path],
    cfg: dict,
    log: LogCb,
) -> list[tuple[int, Image.Image, str]]:
    """입력 파일(들)을 페이지별 PIL 이미지 리스트로 변환.

    Returns:
        [(page_num, PIL.Image, source_label), ...] — page_num은 1-based
    """
    max_side = int(cfg.get("max_image_side", 1600))
    dpi = int(cfg.get("render_dpi", 200))

    result: list[tuple[int, Image.Image, str]] = []

    for p in input_paths:
        ext = p.suffix.lower()
        if ext == ".pdf":
            log(f"[load] PDF rasterize: {p.name} (dpi={dpi})")
            pages = _rasterize_pdf(p, dpi=dpi, max_side=max_side)
            for i, img in enumerate(pages):
                result.append((i + 1, img, p.stem))
        elif ext in (".png", ".jpg", ".jpeg"):
            log(f"[load] 이미지: {p.name}")
            img = iu.load_image(p, max_side=max_side)
            result.append((len(result) + 1, img, p.stem))
        else:
            log(f"[load] 건너뜀 (지원 안 하는 확장자): {p.name}")

    if not result:
        raise CodexRunError(
            "exam_problem_detect: 처리 가능한 입력 없음 (PDF/PNG/JPG 필요)"
        )
    return result


def _rasterize_pdf(
    pdf_path: Path,
    dpi: int = 200,
    max_side: int = 1600,
) -> list[Image.Image]:
    """PDF를 페이지별 PIL 이미지로. lecture_note.vision_extract.render_page_image 재사용."""
    # PyMuPDF import는 필요 시점에 (벤치마크 시 로드 비용 감소 용도)
    import fitz

    images: list[Image.Image] = []
    doc = fitz.open(str(pdf_path))
    try:
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        for i in range(doc.page_count):
            page = doc[i]
            pix = page.get_pixmap(matrix=mat)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            img.load()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            # max_side 적용
            w, h = img.size
            if max(w, h) > max_side:
                scale = max_side / max(w, h)
                img = img.resize(
                    (round(w * scale), round(h * scale)), Image.LANCZOS,
                )
            images.append(img)
    finally:
        doc.close()
    return images


# ─────────────────────── 단일 페이지 처리 ───────────────────────

def _process_page(
    page_num: int,
    img: Image.Image,
    source_label: str,
    cfg: dict,
    log: LogCb,
) -> dict:
    """페이지 1장 처리 → page_result dict."""
    log(f"[page {page_num}] ({source_label}) 시작")

    clusters, bridges, history = detect_page_sampling(img, cfg, log, page_num)

    do_pick = bool(cfg.get("pick_representative", True))

    page_problems: list[dict] = []
    cluster_records: list[dict] = []

    rep_pairs: list[tuple[Cluster, dict, tuple, str]] = []
    for c in clusters:
        candidates = compute_candidates(c.members)
        if do_pick:
            rep_box, variant = pick_representative(img, candidates, cfg, log)
        else:
            rep_box, variant = candidates["medium"], "medium"
        rep_pairs.append((c, candidates, rep_box, variant))

    # 번호 부여 (대표 박스 기준)
    id_box_pairs = assign_ids(page_num, [rp[2] for rp in rep_pairs])

    # id와 cluster 매칭 (assign_ids가 입력 순서를 정렬했으므로 매핑 필요)
    # rep_pairs와 id_box_pairs의 box 동일성으로 매칭
    rep_box_to_cluster = {tuple(rp[2]): rp for rp in rep_pairs}
    for id_str, rep_box in id_box_pairs:
        rp = rep_box_to_cluster[tuple(rep_box)]
        c, candidates, rep, variant = rp
        page_problems.append({
            "id": id_str,
            "page": page_num,
            "source": source_label,
            "cluster_id": c.id,
            "bbox": list(rep),
            "pick_variant": variant,
            "n_detections": len(c.members),
        })
        cluster_records.append({
            "cluster_id": c.id,
            "page": page_num,
            "problem_id": id_str,
            "candidates": {k: list(v) for k, v in candidates.items()},
            "pick_variant": variant,
            "detection_boxes": [list(m) for m in c.members],
            "n_detections": len(c.members),
            "call_indices": c.call_indices,
        })

    # annotated PNG 렌더
    annotated_img = render_page_annotated(img, id_box_pairs)

    bridge_records = [
        {
            "page": page_num,
            "source": source_label,
            "box": list(b.box),
            "linked_cluster_ids": b.linked_cluster_ids,
            "call_idx": b.call_idx,
        }
        for b in bridges
    ]

    return {
        "page_num": page_num,
        "source": source_label,
        "problems": page_problems,
        "clusters": cluster_records,
        "bridges": bridge_records,
        "history": history,
        "annotated": annotated_img,
    }


# ─────────────────────── 메인 엔트리 ───────────────────────

def run_exam_detect(
    skill_dir: Path,
    input_paths: list[Path],
    log_callback: LogCb | None = None,
) -> dict[str, bytes]:
    log = log_callback or (lambda m: None)
    cfg = load_skill(skill_dir).config

    pages = _load_pages(input_paths, cfg, log)
    log(f"[exam_detect] 총 {len(pages)} 페이지 처리 시작")

    page_parallel = int(cfg.get("page_parallel", 1))
    page_results: list[dict] = []

    if page_parallel > 1 and len(pages) > 1:
        log(f"[exam_detect] page_parallel={page_parallel}")
        with ContextThreadPoolExecutor(max_workers=page_parallel) as ex:
            futs = {
                ex.submit(_process_page, pnum, img, src, cfg, log): pnum
                for pnum, img, src in pages
            }
            for fut in as_completed(futs):
                try:
                    page_results.append(fut.result())
                except Exception as exc:
                    pnum = futs[fut]
                    log(f"[page {pnum}] 예외: {exc}")
    else:
        for pnum, img, src in pages:
            try:
                page_results.append(_process_page(pnum, img, src, cfg, log))
            except Exception as exc:
                log(f"[page {pnum}] 예외: {exc}")

    # 페이지 순서대로 정렬
    page_results.sort(key=lambda r: r["page_num"])

    # 집계
    all_problems: list[dict] = []
    all_clusters: list[dict] = []
    all_bridges: list[dict] = []
    outputs: dict[str, bytes] = {}
    for r in page_results:
        all_problems.extend(r["problems"])
        all_clusters.extend(r["clusters"])
        all_bridges.extend(r["bridges"])
        outputs[f"pages/page_{r['page_num']:03d}.png"] = iu.save_png_bytes(
            r["annotated"],
        )

    outputs["problems.json"] = json.dumps(
        all_problems, ensure_ascii=False, indent=2,
    ).encode("utf-8")
    outputs["clusters.json"] = json.dumps(
        all_clusters, ensure_ascii=False, indent=2,
    ).encode("utf-8")
    outputs["bridge_boxes.json"] = json.dumps(
        all_bridges, ensure_ascii=False, indent=2,
    ).encode("utf-8")

    log(
        f"[done] 페이지 {len(page_results)}, 문제 {len(all_problems)}, "
        f"bridge {len(all_bridges)}"
    )
    return outputs
