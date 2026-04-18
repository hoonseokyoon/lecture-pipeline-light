"""detect.py — Codex agent 의 object-detection 도구.

사용:
  python scripts/detect.py --prompt "..."
  python scripts/detect.py --prompt "..." --target OBJ_ID
  python scripts/detect.py --prompt "..." --overwrite

입출력 관례:
  inputs/page.png     : 원본 페이지 이미지
  inputs/objects.json : (선택) 이전 run 의 state — seed
  outputs/objects.json: 갱신된 state (SSOT)
  outputs/annotated.png: 박스+라벨 오버레이
  outputs/crops/obj_ID.png: 각 객체 crop

동작:
  - 기본: 페이지 전체에서 prompt 에 매칭되는 객체들을 한 호출로 동시 감지,
    기존과 IoU<0.5 인 것만 추가.
  - --target OBJ_ID: 그 객체의 bbox 내부를 focus 로 재감지 → sub-객체 여러 개로
    분할. 원래 객체는 제거되고 sub 들이 삽입.
  - --overwrite: 기존 전부 버리고 페이지 전체 재감지.

표준 출력: 결과 요약 JSON (agent 가 읽음).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# scripts/ 디렉토리를 sys.path 에 추가해서 _gemini 등 import.
_SCRIPTS_DIR = Path(__file__).parent.resolve()
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _gemini import call as gemini_call, GeminiError
import _image as im
import _prompts
import _schemas
import _state


WORKDIR = Path(".").resolve()
INPUTS = WORKDIR / "inputs"
OUTPUTS = WORKDIR / "outputs"
CROPS = OUTPUTS / "crops"


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="doc_decode 객체 감지 도구")
    ap.add_argument("--prompt", required=True, help="Gemini 에 줄 감지 지시문")
    ap.add_argument("--target", default=None, help="이 객체 bbox 내부만 focus detection")
    ap.add_argument("--overwrite", action="store_true", help="기존 객체 전부 삭제 후 재감지")
    ap.add_argument("--iou-threshold", type=float, default=0.5)
    ap.add_argument("--model", default=os.environ.get("DOC_DECODE_GEMINI_MODEL", "gemini-2.5-pro"))
    return ap.parse_args()


def _emit_summary(d: dict) -> None:
    print(json.dumps(d, ensure_ascii=False, indent=2))


def _bbox_norm_to_pixel(box_norm, w: int, h: int):
    return im.norm_to_pixel(tuple(box_norm), w, h)


def _write_crops(page_img, objects: list[dict]) -> None:
    CROPS.mkdir(parents=True, exist_ok=True)
    w, h = page_img.size
    for obj in objects:
        bbox_px = _bbox_norm_to_pixel(obj["bbox_norm"], w, h)
        bbox_px = im.clip_pixel(bbox_px, w, h)
        crop = im.crop(page_img, bbox_px)
        rel = f"crops/{obj['id']}.png"
        obj["crop"] = rel
        obj["bbox"] = list(bbox_px)
        crop.save(OUTPUTS / rel, format="PNG")


def _write_annotated(page_img, objects: list[dict]) -> None:
    annotated = im.draw_annotated(page_img, objects)
    annotated.save(OUTPUTS / "annotated.png", format="PNG")


def main() -> int:
    args = _parse_args()

    page_png = INPUTS / "page.png"
    if not page_png.exists():
        _emit_summary({"ok": False, "error": f"입력 없음: {page_png}"})
        return 2

    page_img = im.load_png(page_png)
    w, h = page_img.size

    state = _state.load_state(OUTPUTS, INPUTS)
    state.setdefault("page", None)
    state["page_size"] = [w, h]
    objects: list[dict] = list(state.get("objects", []))

    page_idx = state.get("page") or 1

    # overwrite 경로
    if args.overwrite and args.target is None:
        objects = []

    # focus 경로
    focus_bbox_norm = None
    replaced_target_idx: int | None = None
    if args.target:
        tgt = None
        for i, obj in enumerate(objects):
            if obj.get("id") == args.target:
                tgt = obj
                replaced_target_idx = i
                break
        if tgt is None:
            _emit_summary({"ok": False, "error": f"target 객체 없음: {args.target}"})
            return 2
        bbox_px = tgt.get("bbox") or _bbox_norm_to_pixel(
            tgt["bbox_norm"], w, h,
        )
        focus_bbox_norm = im.pixel_to_norm(tuple(bbox_px), w, h)
        # target 은 결과로 대체 — 제거.
        del objects[replaced_target_idx]

    # Gemini prompt 구성
    existing_norms = [
        tuple(obj["bbox_norm"]) for obj in objects
    ] if (focus_bbox_norm is None and not args.overwrite) else None

    prompt = _prompts.build_detect_prompt(
        args.prompt,
        focus_bbox_norm=focus_bbox_norm,
        existing_bboxes_norm=existing_norms,
    )

    try:
        resp = gemini_call(
            prompt=prompt,
            inputs={"page.png": page_png},
            output_schema=_schemas.DETECT_SCHEMA,
            model=args.model,
        )
    except GeminiError as exc:
        _emit_summary({"ok": False, "error": f"Gemini 실패: {exc}"})
        return 3

    new_raw = resp.get("objects", []) or []

    # 정규화 좌표 클램프 + IoU 중복 제외 + id 발급.
    # focus 모드에서는 reading order 보존을 위해 일단 버퍼에 모아 두고
    # 마지막에 `replaced_target_idx` 위치에 한꺼번에 삽입한다.
    new_objects: list[dict] = []
    skipped_duplicates = 0

    # id 발급을 위한 임시 state — 기존 objects + 아직 확정 안 된 new_objects 포함.
    def _next_id() -> str:
        return _state.next_obj_id({"objects": objects + new_objects}, page_idx)

    for item in new_raw:
        bbox = item.get("bbox") or []
        if len(bbox) != 4:
            continue
        try:
            bn = tuple(float(v) for v in bbox)
        except (TypeError, ValueError):
            continue
        # 정규화 클램프.
        bn = (
            max(0.0, min(im.NORM_MAX, bn[0])),
            max(0.0, min(im.NORM_MAX, bn[1])),
            max(0.0, min(im.NORM_MAX, bn[2])),
            max(0.0, min(im.NORM_MAX, bn[3])),
        )
        if bn[2] <= bn[0] or bn[3] <= bn[1]:
            continue

        bbox_px = _bbox_norm_to_pixel(bn, w, h)

        # focus 모드가 아닐 때: 기존 객체들과 IoU 체크.
        if focus_bbox_norm is None:
            dup = False
            for obj in objects:
                other_px = tuple(obj.get("bbox")) if obj.get("bbox") else _bbox_norm_to_pixel(obj["bbox_norm"], w, h)
                if im.iou_px(tuple(bbox_px), other_px) >= args.iou_threshold:
                    dup = True
                    break
            if dup:
                skipped_duplicates += 1
                continue

        oid = _next_id()
        typ = item.get("type", "unknown")
        hint = item.get("hint", "")
        obj = {
            "id": oid,
            "bbox_norm": list(bn),
            "bbox": list(bbox_px),
            "detected_type": typ,
            "detected_hint": hint,
            "detect_prompt": args.prompt,
            "detect_stamp": _state.stamp_now(),
            "annotation": None,
            "status": "detected",
        }
        new_objects.append(obj)

    if focus_bbox_norm is not None and replaced_target_idx is not None:
        # focus 모드: 제거된 target 위치에 sub-객체를 삽입해 reading order 유지.
        objects[replaced_target_idx:replaced_target_idx] = new_objects
    else:
        objects.extend(new_objects)
    added_ids = [o["id"] for o in new_objects]

    # crop 생성 + annotated 재렌더 + state 저장.
    _write_crops(page_img, objects)
    _write_annotated(page_img, objects)

    state["objects"] = objects
    _state.save_state(OUTPUTS, state)

    _emit_summary({
        "ok": True,
        "mode": "focus" if focus_bbox_norm else ("overwrite" if args.overwrite else "incremental"),
        "target_replaced": args.target if focus_bbox_norm else None,
        "added_ids": added_ids,
        "added_count": len(added_ids),
        "duplicates_skipped": skipped_duplicates,
        "total_objects": len(objects),
        "annotated_png": str((OUTPUTS / "annotated.png").relative_to(WORKDIR)),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
