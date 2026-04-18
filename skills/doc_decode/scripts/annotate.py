"""annotate.py — Codex agent 의 annotation 도구 (verify loop 내장).

사용:
  python scripts/annotate.py
  python scripts/annotate.py --target OBJ_ID
  python scripts/annotate.py --reannotate

입력:
  inputs/page.png
  outputs/objects.json         (detect.py 가 이미 만든 SSOT)
  outputs/crops/<id>.png       (detect.py 생성)

출력:
  outputs/objects.json         (annotation 채워진 상태)
  outputs/renders/<id>.png     (렌더 검증이 필요한 type — equation/table 만)
  outputs/annotated.png        (라벨 갱신)

verify loop (equation/table 만):
  for attempt in 1..max_attempts:
    content = gemini_annotate(crop, prev_attempt, prev_error)
    try:
      rendered = render(content)
    except RenderError as e:
      prev = (content, f"render_failed: {e}"); continue
    verdict = gemini_verify(crop, rendered, type, content)
    if verdict.approved:
      return render_verified=True
    prev = (content, f"rejected: {verdict.reason}")
  else:
    return render_verified=False, error=prev_error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).parent.resolve()
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _gemini import call as gemini_call, GeminiError
import _image as im
import _prompts
import _schemas
import _state
from _render_latex import render_latex, RenderError


WORKDIR = Path(".").resolve()
INPUTS = WORKDIR / "inputs"
OUTPUTS = WORKDIR / "outputs"
CROPS = OUTPUTS / "crops"
RENDERS = OUTPUTS / "renders"


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="doc_decode annotation + verify loop")
    ap.add_argument("--target", default=None, help="이 객체만 재annotate")
    ap.add_argument("--reannotate", action="store_true", help="전 객체 재처리")
    ap.add_argument("--max-attempts", type=int,
                    default=int(os.environ.get("DOC_DECODE_ANNOT_ATTEMPTS", "4")))
    ap.add_argument("--model", default=os.environ.get("DOC_DECODE_GEMINI_MODEL", "gemini-2.5-pro"))
    return ap.parse_args()


def _emit_summary(d: dict) -> None:
    print(json.dumps(d, ensure_ascii=False, indent=2))


def _needs_annotation(obj: dict, reannotate_all: bool, target: str | None) -> bool:
    if target is not None:
        return obj.get("id") == target
    if reannotate_all:
        return True
    ann = obj.get("annotation")
    return not ann


def _render_for_verify(obj_id: str, type_str: str, content: str
                       ) -> tuple[Path | None, str | None]:
    """equation/table 만 렌더. (path, error) 반환.

    table 은 별도 렌더 없이 Gemini 가 직접 텍스트 비교하므로 path=None.
    """
    if type_str == "equation":
        RENDERS.mkdir(parents=True, exist_ok=True)
        out = RENDERS / f"{obj_id}.png"
        try:
            render_latex(content, out)
            return out, None
        except RenderError as exc:
            return None, str(exc)
    # table: HTML 원문을 Gemini 가 직접 crop 과 비교 (별도 렌더 필요 없음)
    # → 검증 단계에서 path 없이 HTML 텍스트만 들고 비교.
    return None, None


def _verify(
    crop_path: Path,
    rendered_path: Path | None,
    type_str: str,
    content: str,
    model: str,
) -> tuple[bool, str]:
    """Gemini 에게 검증 요청. 승인/거부 + 이유 반환.

    equation: crop + 렌더이미지 2장 비교.
    table: crop 1장 + HTML content 텍스트 비교.
    """
    inputs: dict[str, object] = {"crop.png": crop_path}
    if rendered_path is not None:
        inputs["rendered.png"] = rendered_path

    prompt = _prompts.build_verify_prompt(type_str, content)
    try:
        resp = gemini_call(
            prompt=prompt,
            inputs=inputs,
            output_schema=_schemas.VERIFY_SCHEMA,
            model=model,
        )
    except GeminiError as exc:
        # 검증 자체가 실패하면 보수적으로 거부 처리.
        return False, f"verify_call_failed: {exc}"

    approved = bool(resp.get("approved", False))
    reason = str(resp.get("reason", ""))[:400]
    return approved, reason


def _annotate_once(
    crop_path: Path,
    type_hint: str | None,
    previous_attempt: str | None,
    previous_error: str | None,
    model: str,
) -> dict:
    prompt = _prompts.build_annotate_prompt(
        type_hint=type_hint,
        previous_attempt=previous_attempt,
        previous_error=previous_error,
    )
    resp = gemini_call(
        prompt=prompt,
        inputs={"crop.png": crop_path},
        output_schema=_schemas.ANNOTATE_TEXT_SCHEMA,
        model=model,
    )
    return {
        "type": str(resp.get("type", "unknown")),
        "content": str(resp.get("content", "")),
    }


_VERIFY_TYPES = {"equation", "table"}


def _process_object(
    obj: dict,
    crop_path: Path,
    max_attempts: int,
    model: str,
) -> dict:
    """한 객체 annotate + verify loop.

    반환: 업데이트된 annotation dict.
    """
    type_hint = obj.get("detected_type") or None
    previous_attempt: str | None = None
    previous_error: str | None = None

    final_type = type_hint or "unknown"
    final_content = ""
    render_verified: bool | None = None  # None = 검증 불필요
    last_reason = ""
    attempts_used = 0

    for attempt in range(1, max_attempts + 1):
        attempts_used = attempt
        try:
            result = _annotate_once(
                crop_path,
                type_hint=type_hint,
                previous_attempt=previous_attempt,
                previous_error=previous_error,
                model=model,
            )
        except GeminiError as exc:
            previous_error = f"annotate_call_failed: {exc}"
            previous_attempt = None
            continue

        final_type = result["type"]
        final_content = result["content"]

        # 검증이 필요 없는 type 은 즉시 종료.
        if final_type not in _VERIFY_TYPES:
            render_verified = None
            last_reason = ""
            break

        # 렌더 + verify.
        rendered_path, render_err = _render_for_verify(
            obj["id"], final_type, final_content,
        )
        if render_err is not None:
            previous_attempt = final_content
            previous_error = f"render_failed: {render_err}"
            render_verified = False
            last_reason = previous_error
            continue

        approved, reason = _verify(
            crop_path=crop_path,
            rendered_path=rendered_path,
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

    # annotation 구성.
    ann: dict = {
        "type": final_type,
        "content": final_content,
        "attempts_used": attempts_used,
        "annotate_stamp": _state.stamp_now(),
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

    return ann


def main() -> int:
    args = _parse_args()

    page_png = INPUTS / "page.png"
    if not page_png.exists():
        _emit_summary({"ok": False, "error": f"입력 없음: {page_png}"})
        return 2

    page_img = im.load_png(page_png)

    state = _state.load_state(OUTPUTS, INPUTS)
    objects: list[dict] = state.get("objects") or []
    if not objects:
        _emit_summary({"ok": False, "error": "objects.json 비어있음 — 먼저 detect.py 호출 필요"})
        return 2

    # target 지정됐는데 존재 확인.
    if args.target is not None:
        if not any(o.get("id") == args.target for o in objects):
            _emit_summary({"ok": False, "error": f"target 객체 없음: {args.target}"})
            return 2

    processed_ids: list[str] = []
    verified_count = 0
    failed_ids: list[str] = []

    for obj in objects:
        if not _needs_annotation(obj, args.reannotate, args.target):
            continue

        crop_rel = obj.get("crop")
        if not crop_rel:
            # detect 가 crop 없이 둔 경우 — 그 자리에서 다시 만들기.
            w, h = page_img.size
            bbox_px = obj.get("bbox") or im.norm_to_pixel(
                tuple(obj["bbox_norm"]), w, h,
            )
            bbox_px = im.clip_pixel(tuple(bbox_px), w, h)
            CROPS.mkdir(parents=True, exist_ok=True)
            crop_img = im.crop(page_img, bbox_px)
            crop_rel = f"crops/{obj['id']}.png"
            crop_img.save(OUTPUTS / crop_rel, format="PNG")
            obj["crop"] = crop_rel
            obj["bbox"] = list(bbox_px)

        crop_path = OUTPUTS / crop_rel

        ann = _process_object(
            obj,
            crop_path=crop_path,
            max_attempts=args.max_attempts,
            model=args.model,
        )
        obj["annotation"] = ann

        rv = ann.get("render_verified")
        if rv is True:
            obj["status"] = "verified"
            verified_count += 1
        elif rv is False:
            obj["status"] = "annotated"
            failed_ids.append(obj["id"])
        else:
            obj["status"] = "annotated"

        processed_ids.append(obj["id"])

    # annotated 재렌더.
    annotated = im.draw_annotated(page_img, objects)
    annotated.save(OUTPUTS / "annotated.png", format="PNG")

    state["objects"] = objects
    _state.save_state(OUTPUTS, state)

    _emit_summary({
        "ok": True,
        "processed_ids": processed_ids,
        "processed_count": len(processed_ids),
        "verified_count": verified_count,
        "render_failed_ids": failed_ids,
        "total_objects": len(objects),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
