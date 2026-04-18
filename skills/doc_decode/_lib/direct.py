"""doc_decode 직접 실행 파이프라인 — 순수 Gemini 센서 기반.

흐름:
  Round 1 (초기 detect+annotate):
    1. detect 1회: 페이지 전체에서 paragraph-level bbox 감지
    2. 각 객체 crop → annotate (equation/table 은 render+verify loop)
    3. annotated.png 저장
  Round 2+ (review):
    4. review.py 가 annotated.png + 요약을 Gemini 에 주고 corrective action
       (set_bbox, split, add_object, reannotate, remove) 제안 → 실행
    5. max_rounds 도달 또는 overall_ok=True 면 종료

결정론적 휴리스틱 (auto coarse-split, padding, clip-based retry) 은 전부 제거.
Gemini 가 vision 으로 이슈를 발견하고 정확한 좌표까지 지정.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Callable

from PIL import Image

from codex_runner import CodexRunError, current_cancel_event

from _lib import image_utils as iu
from _lib.gemini_backend import run_gemini_task, set_rpm
from _lib.review import review_bboxes, review_with_annotations


LogCb = Callable[[str], None]


# scripts/ 는 subprocess 전용이지만, 여기서 prompt/schema/render 를 재사용한다.
_SKILL_DIR = Path(__file__).parent.parent
_SCRIPTS_DIR = _SKILL_DIR / "scripts"


def _import_scripts_module(name: str):
    path = str(_SCRIPTS_DIR)
    added = path not in sys.path
    if added:
        sys.path.insert(0, path)
    try:
        import importlib
        mod = importlib.import_module(name)
    finally:
        if added:
            try:
                sys.path.remove(path)
            except ValueError:
                pass
    return mod


_prompts_mod = _import_scripts_module("_prompts")
_schemas_mod = _import_scripts_module("_schemas")
try:
    _render_latex_mod = _import_scripts_module("_render_latex")
except Exception:
    _render_latex_mod = None


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
        raise CodexRunError("doc_decode: 사용자 취소")


def _next_obj_id(objects: list[dict], page_idx: int) -> str:
    prefix = f"p{page_idx}_"
    max_n = 0
    for o in objects:
        oid = str(o.get("id", ""))
        if oid.startswith(prefix):
            try:
                n = int(oid[len(prefix):])
                if n > max_n:
                    max_n = n
            except ValueError:
                pass
    return f"{prefix}{max_n + 1:03d}"


# ── detect ──

_PARAGRAPH_PROMPT = (
    "본문 문단(paragraph), 제목·부제·저자, 섹션 헤더, 그림(figure) 본체, "
    "그림 캡션, 표, 표 캡션, 독립 수식, 코드 블록, 각주, 페이지 번호 "
    "— 이 모든 요소를 각각 별도 bbox 로"
)


def _detect_call(
    page_img: Image.Image,
    prompt_body: str,
    existing_objs: list[dict],
    *,
    focus_bbox_norm: tuple[float, float, float, float] | None = None,
    overwrite: bool = False,
    model: str,
) -> list[dict]:
    """Gemini detect 1회 호출 → 원시 객체 리스트 반환."""
    existing_norms = None
    if focus_bbox_norm is None and not overwrite:
        existing_norms = [
            tuple(o["bbox_norm"]) for o in existing_objs
            if o.get("bbox_norm")
        ] or None

    prompt = _prompts_mod.build_detect_prompt(
        prompt_body,
        focus_bbox_norm=focus_bbox_norm,
        existing_bboxes_norm=existing_norms,
    )
    img_bytes = iu.save_png_bytes(page_img)
    out = run_gemini_task(
        prompt=prompt,
        inputs={"page.png": img_bytes},
        output_schema=_schemas_mod.DETECT_SCHEMA,
        model=model,
    )
    data = json.loads(out["result.json"].decode("utf-8"))
    return data.get("objects", []) or []


def _normalize_raw_objects(
    new_raw: list[dict],
    existing_objs: list[dict],
    page_img: Image.Image,
    *,
    iou_threshold: float,
    focus_bbox_norm: tuple[float, float, float, float] | None,
    detect_prompt: str,
    page_idx: int,
) -> tuple[list[dict], int]:
    """Gemini 반환값을 정규화 + IoU 중복 제거 + id 발급."""
    w, h = page_img.size
    new_objects: list[dict] = []
    skipped = 0

    for item in new_raw:
        bbox = item.get("bbox") or []
        if len(bbox) != 4:
            continue
        try:
            bn = tuple(float(v) for v in bbox)
        except (TypeError, ValueError):
            continue
        bn = (
            max(0.0, min(iu.NORM_MAX, bn[0])),
            max(0.0, min(iu.NORM_MAX, bn[1])),
            max(0.0, min(iu.NORM_MAX, bn[2])),
            max(0.0, min(iu.NORM_MAX, bn[3])),
        )
        if bn[2] <= bn[0] or bn[3] <= bn[1]:
            continue
        bbox_px = iu.norm_to_pixel(bn, w, h)
        bbox_px = iu.clip_pixel(bbox_px, w, h)

        if focus_bbox_norm is None:
            dup = False
            for obj in existing_objs:
                other = obj.get("bbox") or list(iu.norm_to_pixel(
                    tuple(obj["bbox_norm"]), w, h,
                ))
                if iu.iou(tuple(bbox_px), tuple(other)) >= iou_threshold:
                    dup = True
                    break
            if dup:
                skipped += 1
                continue

        oid = _next_obj_id(existing_objs + new_objects, page_idx)
        new_objects.append({
            "id": oid,
            "bbox_norm": list(bn),
            "bbox": list(bbox_px),
            "detected_type": str(item.get("type", "unknown")),
            "detected_hint": str(item.get("hint", "")),
            "detect_prompt": detect_prompt,
            "annotation": None,
            "status": "detected",
        })
    return new_objects, skipped


# ── annotate ──

_VERIFY_TYPES = {"equation", "table"}  # host 가 render+verify 가능한 type 만


def _annotate_once(
    crop_bytes: bytes,
    type_hint: str | None,
    previous_attempt: str | None,
    previous_error: str | None,
    model: str,
) -> dict:
    prompt = _prompts_mod.build_annotate_prompt(
        type_hint=type_hint,
        previous_attempt=previous_attempt,
        previous_error=previous_error,
    )
    out = run_gemini_task(
        prompt=prompt,
        inputs={"crop.png": crop_bytes},
        output_schema=_schemas_mod.ANNOTATE_TEXT_SCHEMA,
        model=model,
    )
    data = json.loads(out["result.json"].decode("utf-8"))
    return {
        "type": str(data.get("type", "unknown")),
        "content": str(data.get("content", "")),
    }


def _verify(
    crop_bytes: bytes,
    rendered_bytes: bytes | None,
    type_str: str,
    content: str,
    model: str,
) -> tuple[bool, str]:
    inputs: dict[str, object] = {"crop.png": crop_bytes}
    if rendered_bytes is not None:
        inputs["rendered.png"] = rendered_bytes
    prompt = _prompts_mod.build_verify_prompt(type_str, content)
    try:
        out = run_gemini_task(
            prompt=prompt,
            inputs=inputs,
            output_schema=_schemas_mod.VERIFY_SCHEMA,
            model=model,
        )
    except CodexRunError as exc:
        return False, f"verify_call_failed: {exc}"
    data = json.loads(out["result.json"].decode("utf-8"))
    return bool(data.get("approved", False)), str(data.get("reason", ""))[:400]


def _render_latex_bytes(content: str) -> tuple[bytes | None, str | None]:
    if _render_latex_mod is None:
        return None, "render_latex 모듈 없음"
    import tempfile
    tmp = Path(tempfile.mktemp(suffix=".png"))
    try:
        _render_latex_mod.render_latex(content, tmp)
        return tmp.read_bytes(), None
    except Exception as exc:
        return None, str(exc)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _annotate_object(
    obj: dict,
    crop_bytes: bytes,
    *,
    max_attempts: int,
    model: str,
) -> tuple[dict, bytes | None]:
    """객체 annotate. equation/table 만 render+verify loop, 나머지는 단일 호출.

    bbox 는 절대 건드리지 않는다 — 경계 문제는 review 가 `set_bbox` 로 해결.
    """
    type_hint = obj.get("detected_type") or None
    previous_attempt: str | None = None
    previous_error: str | None = None

    final_type = type_hint or "unknown"
    final_content = ""
    render_verified: bool | None = None
    last_reason = ""
    attempts_used = 0
    rendered_bytes: bytes | None = None

    for attempt in range(1, max_attempts + 1):
        attempts_used = attempt
        try:
            result = _annotate_once(
                crop_bytes,
                type_hint=type_hint,
                previous_attempt=previous_attempt,
                previous_error=previous_error,
                model=model,
            )
        except CodexRunError as exc:
            previous_error = f"annotate_call_failed: {exc}"
            previous_attempt = None
            continue

        final_type = result["type"]
        final_content = result["content"]

        if final_type not in _VERIFY_TYPES:
            render_verified = None
            last_reason = ""
            break

        if final_type == "equation":
            rendered_bytes, render_err = _render_latex_bytes(final_content)
            if render_err is not None:
                previous_attempt = final_content
                previous_error = f"render_failed: {render_err}"
                render_verified = False
                last_reason = previous_error
                continue
        else:
            rendered_bytes = None  # table: HTML 텍스트만 비교

        approved, reason = _verify(
            crop_bytes=crop_bytes,
            rendered_bytes=rendered_bytes,
            type_str=final_type,
            content=final_content,
            model=model,
        )
        if approved:
            render_verified = True
            last_reason = reason
            break
        previous_attempt = final_content
        previous_error = f"rejected: {reason}"
        render_verified = False
        last_reason = reason

    ann: dict = {
        "type": final_type,
        "content": final_content,
        "attempts_used": attempts_used,
    }
    if final_type == "equation":
        ann["latex"] = final_content
    elif final_type == "table":
        ann["html"] = final_content
    elif final_type == "figure":
        ann["description"] = final_content
    elif final_type == "text":
        ann["raw_ocr"] = final_content

    if render_verified is not None:
        ann["render_verified"] = render_verified
        ann["verify_reason"] = last_reason

    return ann, rendered_bytes


def _crop_and_annotate(
    obj: dict,
    page_img: Image.Image,
    page_ckpt: Path,
    *,
    max_attempts: int,
    model: str,
    log: LogCb,
    crop_padding_px: int = 0,
) -> None:
    """obj["bbox"] 에 맞춰 crop 저장 + annotate → obj["annotation"] 세팅.

    annotate 시점에 기계적으로 `crop_padding_px` 만큼 bbox 를 확장해 crop.
    obj["bbox"] 자체는 Gemini 가 준 좌표 그대로 유지 (structure.json 과
    annotated.png 는 원본 bbox 기준). padding 은 OCR 안전 버퍼 목적.
    """
    w, h = page_img.size
    raw_bbox = iu.clip_pixel(tuple(int(v) for v in obj["bbox"]), w, h)
    if crop_padding_px > 0:
        x1, y1, x2, y2 = raw_bbox
        bbox_px = (
            max(0, x1 - crop_padding_px),
            max(0, y1 - crop_padding_px),
            min(w, x2 + crop_padding_px),
            min(h, y2 + crop_padding_px),
        )
    else:
        bbox_px = raw_bbox
    crop_img = iu.crop_bbox(page_img, bbox_px)
    buf = io.BytesIO()
    crop_img.save(buf, format="PNG")
    crop_bytes = buf.getvalue()

    crops_dir = page_ckpt / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    crop_path = crops_dir / f"{obj['id']}.png"
    crop_path.write_bytes(crop_bytes)
    obj["crop"] = f"crops/{obj['id']}.png"

    try:
        ann, rendered_bytes = _annotate_object(
            obj, crop_bytes,
            max_attempts=max_attempts, model=model,
        )
    except CodexRunError as exc:
        _emit(log, f"[{obj['id']}] annotate 실패: {exc}")
        return

    obj["annotation"] = ann
    rv = ann.get("render_verified")
    obj["status"] = "verified" if rv is True else "annotated"
    if rendered_bytes is not None:
        renders_dir = page_ckpt / "renders"
        renders_dir.mkdir(parents=True, exist_ok=True)
        (renders_dir / f"{obj['id']}.png").write_bytes(rendered_bytes)


# ── page orchestration ──

def process_page_direct(
    page_idx: int,
    page_ckpt: Path,
    page_img_bytes: bytes,
    profile_text: str,
    cfg: dict,
    log: LogCb,
) -> dict:
    """한 페이지를 순수 Gemini 센서 기반으로 처리.

    Round 1: detect + per-object annotate
    Round 2+: review_and_fix 가 Gemini 에 annotated 이미지를 주고 action 제안 → 실행
    """
    _raise_if_cancelled()
    set_rpm(int(cfg.get("gemini_rpm", 0) or 0))

    model = cfg.get("gemini_model", "gemini-2.5-pro")
    iou_threshold = float(cfg.get("iou_threshold", 0.5))
    max_attempts = int(cfg.get("annotate_max_attempts", 4))

    page_img = Image.open(io.BytesIO(page_img_bytes))
    page_img.load()
    if page_img.mode not in ("RGB", "RGBA"):
        page_img = page_img.convert("RGB")
    w, h = page_img.size

    # 기존 state (부분 진행) 재사용
    state_path = page_ckpt / "objects.json"
    state: dict = {"page": page_idx, "page_size": [w, h], "objects": []}
    if state_path.exists():
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(prev, dict):
                state["objects"] = prev.get("objects", []) or []
        except json.JSONDecodeError:
            pass

    objects: list[dict] = state["objects"]

    # ── Round 1: detect ──
    if not objects:
        _emit(log, f"[page {page_idx}] detect (paragraph-level)")
        raw = _detect_call(
            page_img, _PARAGRAPH_PROMPT,
            existing_objs=[], model=model,
        )
        new_objs, _ = _normalize_raw_objects(
            raw, existing_objs=[],
            page_img=page_img,
            iou_threshold=iou_threshold,
            focus_bbox_norm=None,
            detect_prompt=_PARAGRAPH_PROMPT,
            page_idx=page_idx,
        )
        objects.extend(new_objs)
        _emit(log, f"[page {page_idx}] detect → {len(new_objs)} 객체")
        state["objects"] = objects
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # Round 1 결과 annotated.png (bbox 만 표시)
    annotated = iu.draw_annotated(page_img, objects)
    annotated.save(page_ckpt / "annotated.png", format="PNG")

    # ── Phase 1: bbox review loop (annotation 없이) ──
    # bbox 가 정확해진 시점 이후에만 annotate 에 들어간다.
    try:
        review_bboxes(
            objects, page_img, page_ckpt, cfg, log,
            page_idx=page_idx,
            profile_text=profile_text,
        )
    except CodexRunError as exc:
        ev = current_cancel_event.get()
        if ev is not None and ev.is_set():
            raise
        _emit(log, f"[page {page_idx}] review 건너뜀: {exc}")

    state["objects"] = objects
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ── Phase 2: annotate (bbox 확정 후) ──
    _raise_if_cancelled()
    crop_padding = int(cfg.get("crop_padding_px", 0) or 0)
    pending = [o for o in objects if not o.get("annotation")]
    _emit(
        log,
        f"[page {page_idx}] bbox 확정 — annotate {len(pending)}개 "
        f"(crop_padding={crop_padding}px)",
    )
    for i, obj in enumerate(pending, start=1):
        _raise_if_cancelled()
        _crop_and_annotate(
            obj, page_img, page_ckpt,
            max_attempts=max_attempts, model=model, log=log,
            crop_padding_px=crop_padding,
        )
        state["objects"] = objects
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if i % 5 == 0 or i == len(pending):
            _emit(log, f"[page {page_idx}] annotate 진행 {i}/{len(pending)}")

    # Phase 2 끝 — 중간 annotated.png
    annotated = iu.draw_annotated(page_img, objects)
    annotated.save(page_ckpt / "annotated.png", format="PNG")

    # ── Phase 3: content-aware review-annotation 사이클 ──
    # bbox 변경이 필요한 객체가 발견되면 해당 객체만 재annotate.
    def _reannotate_closure(target_obj: dict, all_objs: list[dict]) -> None:
        _crop_and_annotate(
            target_obj, page_img, page_ckpt,
            max_attempts=max_attempts, model=model, log=log,
            crop_padding_px=crop_padding,
        )

    try:
        review_with_annotations(
            objects, page_img, page_ckpt, cfg, log,
            page_idx=page_idx,
            annotate_fn=_reannotate_closure,
            profile_text=profile_text,
        )
    except CodexRunError as exc:
        ev = current_cancel_event.get()
        if ev is not None and ev.is_set():
            raise
        _emit(log, f"[page {page_idx}] review2 건너뜀: {exc}")

    # 최종 annotated.png / state
    annotated = iu.draw_annotated(page_img, objects)
    annotated.save(page_ckpt / "annotated.png", format="PNG")
    state["objects"] = objects
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return state
