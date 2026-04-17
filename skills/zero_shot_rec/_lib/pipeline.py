"""zero_shot_rec 메인 orchestrator.

skill.py에서 `run = run_rec`로 노출되어 codex_runner.run_skill이 호출한다.
"""

from __future__ import annotations

import json
from concurrent.futures import as_completed
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from codex_runner import (
    CodexRunError,
    ContextThreadPoolExecutor,
    load_skill,
)

from _lib import image_utils as iu
from _lib.edge_refine import refine_one_edge
from _lib.llm import call_llm
from _lib.prompts import (
    build_stage0_pick_prompt,
    build_stage0_presence_prompt,
    build_stage0_propose_prompt,
    build_stage1_containment_prompt,
)
from _lib.schemas import (
    STAGE0_PICK_SCHEMA,
    STAGE0_PRESENCE_SCHEMA,
    STAGE0_PROPOSE_SCHEMA,
    STAGE1_SCHEMA,
)


LogCb = Callable[[str], None]


# ───────────────────────── 입력 해결 ─────────────────────────

def _read_conditioning(input_paths: list[Path]) -> str:
    """conditioning_<id>.txt 또는 conditioning.txt에서 text 필드 읽기."""
    for p in input_paths:
        name = p.name.lower()
        if (name.startswith("conditioning") and name.endswith(".txt")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise CodexRunError(
                    f"conditioning 파일 JSON 파싱 실패: {p}: {exc}"
                ) from exc
            text = (data.get("text") or "").strip()
            if not text:
                raise CodexRunError(f"conditioning 텍스트가 비어있음: {p}")
            return text
    raise CodexRunError(
        "conditioning 파일이 input_paths에 없음. "
        "GUI에서 ask_conditioning 모달로 입력하거나, "
        "conditioning_X.txt (JSON: {text: ...})를 함께 제공하라."
    )


def _load_source_image(input_paths: list[Path], max_side: int) -> tuple[Image.Image, Path]:
    """PNG/JPG 입력 1개를 로드."""
    candidates = [
        p for p in input_paths
        if p.suffix.lower() in (".png", ".jpg", ".jpeg")
    ]
    if len(candidates) != 1:
        raise CodexRunError(
            f"zero_shot_rec: 이미지 1개 필요 (found {len(candidates)})"
        )
    return iu.load_image(candidates[0], max_side=max_side), candidates[0]


# ───────────────────────── Stage 0 ─────────────────────────

def _stage0_presence_single(
    img: Image.Image,
    condition: str,
    cfg: dict,
) -> bool:
    """1회 presence 판정. True/False만 반환."""
    prompt = build_stage0_presence_prompt(condition)
    png = iu.save_png_bytes(img)
    outputs = call_llm(
        cfg=cfg,
        prompt=prompt,
        inputs={"image.png": png},
        output_schema=STAGE0_PRESENCE_SCHEMA,
        reasoning_effort=cfg.get("stage0_reasoning_effort", "high"),
    )
    data = json.loads(outputs["result.json"].decode("utf-8"))
    return bool(data.get("target_present", True))


def _stage0_presence_voted(
    img: Image.Image,
    condition: str,
    cfg: dict,
    log: LogCb,
) -> tuple[bool, dict]:
    """presence를 N회 호출해 majority voting. (target_present, vote_meta) 반환."""
    n_votes = int(cfg.get("stage0_vote_calls", 3))
    log(f"[stage0 presence] {n_votes}회 voting 시작")

    results: list[bool] = []
    errors: list[str] = []
    for i in range(n_votes):
        try:
            present = _stage0_presence_single(img, condition, cfg)
            results.append(present)
            log(f"[stage0 vote {i + 1}/{n_votes}] present={present}")
        except (CodexRunError, json.JSONDecodeError, KeyError, ValueError) as exc:
            errors.append(str(exc))
            log(f"[stage0 vote {i + 1}/{n_votes}] 실패, 기권: {exc}")

    if not results:
        raise CodexRunError(
            f"stage0 presence: {n_votes}회 voting 전부 실패.\n"
            + "\n".join(errors)
        )

    present_count = sum(1 for p in results if p)
    absent_count = len(results) - present_count
    unanimous = present_count == len(results) or absent_count == len(results)
    # tie → present: 사용자가 condition을 입력한 prior(존재 기대)에 맞춤
    target_present = present_count >= absent_count

    vote_meta = {
        "n_votes": n_votes,
        "successful_calls": len(results),
        "present_votes": present_count,
        "absent_votes": absent_count,
        "unanimous": unanimous,
    }
    log(
        f"[stage0 vote] present={present_count}/{len(results)}, "
        f"absent={absent_count}/{len(results)}, unanimous={unanimous} "
        f"→ target_present={target_present}"
    )
    return target_present, vote_meta


def _stage0_propose(
    img: Image.Image,
    condition: str,
    cfg: dict,
    log: LogCb,
) -> list[tuple[float, float, float, float]]:
    """Presence 확정 후 1회 호출로 candidate 박스 N개 제안."""
    n = int(cfg.get("stage0_candidates", 3))
    log(f"[stage0 propose] {n}개 후보 제안 요청")
    prompt = build_stage0_propose_prompt(condition, n)
    png = iu.save_png_bytes(img)
    outputs = call_llm(
        cfg=cfg,
        prompt=prompt,
        inputs={"image.png": png},
        output_schema=STAGE0_PROPOSE_SCHEMA,
        reasoning_effort=cfg.get("stage0_reasoning_effort", "high"),
    )
    data = json.loads(outputs["result.json"].decode("utf-8"))
    boxes: list[tuple[float, float, float, float]] = []
    for c in data.get("candidates", []) or []:
        box = iu.clip_box_norm(
            (float(c["x1"]), float(c["y1"]),
             float(c["x2"]), float(c["y2"]))
        )
        boxes.append(box)
    if not boxes:
        raise CodexRunError("stage0 propose: 후보가 비어있음")
    log(f"[stage0 propose] {len(boxes)}개 후보 수신")
    return boxes


def _stage0_pick(
    img: Image.Image,
    candidates: list[tuple[float, float, float, float]],
    condition: str,
    cfg: dict,
    log: LogCb,
) -> tuple[float, float, float, float]:
    """번호 매긴 후보 이미지로 best 선택."""
    if len(candidates) == 1:
        return candidates[0]

    ids = list(range(1, len(candidates) + 1))
    annotated = iu.annotate_stage0_candidates(img, candidates, ids)
    png = iu.save_png_bytes(annotated)
    prompt = build_stage0_pick_prompt(condition)
    log(f"[stage0 pick] {len(candidates)}개 후보 중 최선 선택 요청")
    try:
        outputs = call_llm(
            cfg=cfg,
            prompt=prompt,
            inputs={"image.png": png},
            output_schema=STAGE0_PICK_SCHEMA,
            reasoning_effort=cfg.get("stage0_reasoning_effort", "high"),
        )
        data = json.loads(outputs["result.json"].decode("utf-8"))
        best_id = int(data["best_id"])
        if not (1 <= best_id <= len(candidates)):
            raise ValueError(f"best_id 범위 밖: {best_id}")
        log(f"[stage0 pick] 선택: #{best_id} — {data.get('reason', '')[:80]}")
        return candidates[best_id - 1]
    except (CodexRunError, json.JSONDecodeError, KeyError, ValueError) as exc:
        log(f"[stage0 pick] 실패, candidates[0] 사용: {exc}")
        return candidates[0]


# ───────────────────────── Stage 1 ─────────────────────────

def _stage1_containment(
    img: Image.Image,
    box_norm: tuple[float, float, float, float],
    condition: str,
    cfg: dict,
    log: LogCb,
) -> dict[str, Any]:
    """박스 오버레이로 fit 품질 판정. 존재 여부는 묻지 않음 (Stage 0가 확정)."""
    annotated = img.copy()
    w, h = annotated.size
    box_px = iu.norm_to_pixel(box_norm, w, h)
    iu.draw_box(annotated, box_px, color="red", width=4)

    prompt = build_stage1_containment_prompt(condition)
    png = iu.save_png_bytes(annotated)
    try:
        outputs = call_llm(
            cfg=cfg,
            prompt=prompt,
            inputs={"image.png": png},
            output_schema=STAGE1_SCHEMA,
            reasoning_effort=cfg.get(
                "stage1_reasoning_effort",
                cfg.get("stage12_reasoning_effort", "high"),
            ),
        )
        data = json.loads(outputs["result.json"].decode("utf-8"))
    except (CodexRunError, json.JSONDecodeError, KeyError) as exc:
        log(f"[stage1] 실패 — verdict=A로 처리 (루프 종료): {exc}")
        return {"verdict": "A", "loose_sides": [], "clipped_sides": [], "notes": str(exc)}
    log(
        f"[stage1] verdict={data.get('verdict')} "
        f"loose={data.get('loose_sides')} clipped={data.get('clipped_sides')}"
    )
    return data


# ───────────────────────── Stage 2 ─────────────────────────

def _edge_margins(edge: str,
                  loose_sides: set[str],
                  clipped_sides: set[str],
                  baseline: float,
                  biased_out: float,
                  biased_in: float,
                  ) -> tuple[float, float]:
    """Stage 1 verdict로 edge의 outward/inward margin 결정.

    clipped(target이 박스 밖) → outward 큼, inward 작음.
    loose(target이 박스 안쪽)  → outward 작음, inward 큼.
    없음 → 대칭 baseline.
    """
    if edge in clipped_sides:
        return biased_out, biased_in
    if edge in loose_sides:
        return biased_in, biased_out
    return baseline, baseline


def _stage2_refine_edges(
    img: Image.Image,
    box_norm: tuple[float, float, float, float],
    condition: str,
    cfg: dict,
    log: LogCb,
    verdict: dict[str, Any],
) -> tuple[float, float, float, float]:
    """4-edge 병렬 refinement. Stage 1 verdict로 edge별 ROI 비대칭 margin."""
    rows, cols = cfg.get("edge_dot_grid", [5, 8])
    baseline = float(cfg.get("edge_roi_margin", 0.25))
    biased_out = float(cfg.get("edge_roi_biased_out", 0.60))
    biased_in = float(cfg.get("edge_roi_biased_in", 0.10))
    lateral = float(cfg.get("edge_roi_lateral", 0.20))
    min_roi = int(cfg.get("edge_min_roi_px", 400))
    effort = cfg.get("stage2_reasoning_effort",
                     cfg.get("stage12_reasoning_effort", "medium"))

    loose = set(verdict.get("loose_sides") or [])
    clipped = set(verdict.get("clipped_sides") or [])

    edges = ["top", "bottom", "left", "right"]
    results: dict[str, float] = {}

    def _worker(edge: str) -> tuple[str, float]:
        outward, inward = _edge_margins(
            edge, loose, clipped, baseline, biased_out, biased_in,
        )
        bias_tag = (
            "clipped→out" if edge in clipped
            else "loose→in" if edge in loose
            else "sym"
        )
        log(f"[stage2 {edge}] ROI margin: out={outward:.2f} in={inward:.2f} ({bias_tag})")
        new_val, _raw = refine_one_edge(
            img, box_norm, edge, condition,
            cfg=cfg,
            reasoning_effort=effort,
            outward=outward,
            inward=inward,
            lateral=lateral,
            min_roi_px=min_roi,
            rows=rows,
            cols=cols,
            log_callback=log,
        )
        return edge, new_val

    with ContextThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_worker, e): e for e in edges}
        for fut in as_completed(futs):
            e = futs[fut]
            try:
                _, new_val = fut.result()
                results[e] = new_val
            except Exception as exc:
                log(f"[stage2 {e}] worker 예외 — edge 유지: {exc}")
                results[e] = _edge_current(box_norm, e)

    new_box = (
        results["left"],
        results["top"],
        results["right"],
        results["bottom"],
    )
    return iu.clip_box_norm(new_box)


def _edge_current(box_norm, edge: str) -> float:
    x1, y1, x2, y2 = box_norm
    return {"top": y1, "bottom": y2, "left": x1, "right": x2}[edge]


# ───────────────────────── 메인 loop ─────────────────────────

def run_rec(
    skill_dir: Path,
    input_paths: list[Path],
    log_callback: LogCb | None = None,
) -> dict[str, bytes]:
    log = log_callback or (lambda m: None)
    cfg = load_skill(skill_dir).config

    max_side = int(cfg.get("max_image_side", 1600))
    max_iter = int(cfg.get("max_iterations", 3))
    conv_iou = float(cfg.get("convergence_iou", 0.95))
    sanity_iou = float(cfg.get("sanity_iou", 0.2))
    enable_refinement = bool(cfg.get("enable_refinement", False))

    # 입력
    condition = _read_conditioning(input_paths)
    img, src_path = _load_source_image(input_paths, max_side=max_side)
    w, h = img.size
    log(f'[zero_shot_rec] 조건: "{condition}"  (이미지: {src_path.name}, '
        f'{w}x{h})  refinement={"ON" if enable_refinement else "OFF"}')

    # Stage 0-pre: presence voting (가벼움, N회)
    target_present, vote_meta = _stage0_presence_voted(img, condition, cfg, log)

    # target 부재 → 조기 반환 (propose/pick 건너뜀)
    if not target_present:
        log("[done] target 부재 — 빈 결과 반환")
        result_absent = {
            "condition": condition,
            "image": src_path.name,
            "image_size_px": [w, h],
            "target_present": False,
            "stage0_vote": vote_meta,
            "box_norm": None,
            "box_pixel": None,
            "init_box_norm": None,
            "iterations": 0,
            "refinement_enabled": enable_refinement,
            "refinement_ran": False,
            "sanity_triggered": False,
        }
        return {
            "result.json": json.dumps(
                result_absent, ensure_ascii=False, indent=2,
            ).encode("utf-8"),
            "annotated.png": iu.save_png_bytes(img),
        }

    # Stage 0a: propose (1회, presence 확정 후)
    candidates = _stage0_propose(img, condition, cfg, log)
    # Stage 0b: pick (top-N → best 1)
    current_box = _stage0_pick(img, candidates, condition, cfg, log)
    init_box = current_box
    log(f"[stage0] init box: {_fmt_box(init_box)}")

    # ───────── Stage 1~3 refinement loop (optional) ─────────
    iterations_done = 0
    sanity_triggered = False
    if enable_refinement:
        for it in range(max_iter):
            iterations_done = it + 1
            log(f"[iter {it + 1}/{max_iter}] box: {_fmt_box(current_box)}")
            v = _stage1_containment(img, current_box, condition, cfg, log)
            verdict = v.get("verdict", "A")
            if verdict == "A":
                log(f"[iter {it + 1}] verdict=A — 수렴")
                break

            new_box = _stage2_refine_edges(
                img, current_box, condition, cfg, log, v,
            )
            step_iou = iu.iou(current_box, new_box)
            log(
                f"[iter {it + 1}] new box: {_fmt_box(new_box)} "
                f"(step IoU={step_iou:.3f})"
            )
            if step_iou > conv_iou:
                current_box = new_box
                log(f"[iter {it + 1}] step IoU > {conv_iou} — 수렴")
                break
            current_box = new_box

        # sanity guard
        final_iou_vs_init = iu.iou(init_box, current_box)
        if final_iou_vs_init < sanity_iou:
            log(
                f"[sanity] IoU(init, final)={final_iou_vs_init:.3f} < "
                f"{sanity_iou} — 발산 감지, init 반환"
            )
            current_box = init_box
            sanity_triggered = True
    else:
        log("[refinement] disabled by config (enable_refinement=false) — Stage 0 결과 그대로 사용")

    # ───────── 출력 ─────────
    box_px = iu.norm_to_pixel(current_box, w, h)
    annotated = img.copy()
    iu.draw_box(annotated, box_px, color="red", width=4)

    result = {
        "condition": condition,
        "image": src_path.name,
        "image_size_px": [w, h],
        "target_present": True,
        "stage0_vote": vote_meta,
        "box_norm": [round(v, 3) for v in current_box],
        "box_pixel": list(box_px),
        "init_box_norm": [round(v, 3) for v in init_box],
        "iterations": iterations_done,
        "refinement_enabled": enable_refinement,
        "refinement_ran": enable_refinement,
        "sanity_triggered": sanity_triggered,
    }
    log(f"[done] final box: {_fmt_box(current_box)} (px={box_px})")
    return {
        "result.json": json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"),
        "annotated.png": iu.save_png_bytes(annotated),
    }


def _fmt_box(box: tuple[float, float, float, float]) -> str:
    return f"[{box[0]:.1f}, {box[1]:.1f}, {box[2]:.1f}, {box[3]:.1f}]"
