"""페이지 review 레이어 — bbox 전용.

설계 원칙:
  - 프롬프트는 최소한으로. Gemini 가 자체 판단 + action 제안.
  - review 는 **bbox 만** 수정 (set_bbox / split / add_object / remove).
    annotation 은 review 가 끝난 뒤 direct.py 가 일괄 수행.
  - 결정론 층은 schema 검증 + in-place 수정만 담당 (좌표 clamp, id 발급, IoU
    중복 skip, 시그니처 dedup).

Actions:
  - set_bbox    : target_id 의 bbox 를 new_bbox_norm 으로 교체
  - split       : target_id 를 sub_bboxes (N개) 로 교체
  - add_object  : new_bbox_norm 위치에 새 객체 추가
  - remove      : target_id 삭제

무한루프 방지:
  - max_rounds 상한
  - 라운드당 action cap (per_round_cap)
  - overall_ok=True 또는 Gemini 가 issues 를 비우면 조기 종료
  - 한 라운드에서 어떤 action 도 적용 실패(0건) 면 조기 종료
"""

from __future__ import annotations

import io
import json
import random
import time
from pathlib import Path
from typing import Callable

from PIL import Image

from codex_runner import (
    CodexRunError,
    ContextThreadPoolExecutor,
    current_cancel_event,
)

from _lib import image_utils as iu
from _lib.gemini_backend import run_gemini_task


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
        raise CodexRunError("doc_decode: 사용자 취소")


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_ok": {
            "type": "boolean",
            "description": (
                "모든 주요 content 가 정확한 bbox 로 감지되었으면 true. "
                "하나라도 구조적 문제 (bbox 부정확, 누락, 중복, 잘못된 감지) 가 "
                "있으면 false."
            ),
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "set_bbox", "split", "add_object", "remove",
                        ],
                        "description": (
                            "set_bbox: 특정 객체의 bbox 를 new_bbox_norm 으로 "
                            "교체. split: 한 객체를 sub_bboxes 로 분할. "
                            "add_object: 누락된 영역에 새 bbox 추가. "
                            "remove: 잘못 감지된 객체 삭제."
                        ),
                    },
                    "target_id": {
                        "type": "string",
                        "description": "set_bbox/split/remove 의 대상 id.",
                    },
                    "new_bbox_norm": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": (
                            "set_bbox / add_object 시 필수. "
                            "[x1, y1, x2, y2] 정규화 [0, 1000]. "
                            "원본 페이지 전체 기준."
                        ),
                    },
                    "sub_bboxes": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "number"},
                        },
                        "description": (
                            "split 시 필수. 각각 [x1,y1,x2,y2] 정규화 좌표의 "
                            "배열 (2개 이상)."
                        ),
                    },
                    "type_hint": {
                        "type": "string",
                        "description": (
                            "split/add_object 시 선택. 새 객체의 예상 type "
                            "(text/figure/table/equation/code/form)."
                        ),
                    },
                    "reason": {"type": "string"},
                },
                "required": ["action", "reason"],
            },
        },
    },
    "required": ["overall_ok", "issues"],
}


# 미니멀 프롬프트 — Gemini 가 이미지를 보고 스스로 판단.
_REVIEW_PROMPT_TEMPLATE = """\
첨부:
- `original.png` : 원본 페이지
- `annotated.png`: 현재 감지된 bbox 와 id/type 라벨이 오버레이된 페이지
- `objects_summary.txt`: bbox 목록 요약
{profile_block}
원본을 기준으로 annotated 의 bbox 배치를 평가하라. 정확하고 완전하면
`overall_ok=true`, 문제가 있으면 `issues` 에 action 을 넣어라.

모든 좌표는 **원본 페이지 전체** 기준 정규화 [0, 1000] 의 [x1, y1, x2, y2].
사소한 차이(몇 픽셀, 미세 오타) 는 무시.
"""


def _pairwise_overlap_warnings(
    objects: list[dict],
    threshold_px: int,
) -> list[tuple[str, str, int, int]]:
    """bbox 쌍 중 가로·세로 겹침이 둘 다 threshold_px 이상인 쌍을 반환.

    2-column 레이아웃을 넘나드는 bbox 나 인접 문단 겹침 등을 Gemini review 에
    힌트로 제공하기 위한 단순 픽셀 계산.
    """
    warnings: list[tuple[str, str, int, int]] = []
    n = len(objects)
    for i in range(n):
        a = objects[i].get("bbox") or [0, 0, 0, 0]
        if len(a) != 4:
            continue
        for j in range(i + 1, n):
            b = objects[j].get("bbox") or [0, 0, 0, 0]
            if len(b) != 4:
                continue
            x_ov = min(int(a[2]), int(b[2])) - max(int(a[0]), int(b[0]))
            y_ov = min(int(a[3]), int(b[3])) - max(int(a[1]), int(b[1]))
            if x_ov >= threshold_px and y_ov >= threshold_px:
                warnings.append((
                    str(objects[i].get("id", "?")),
                    str(objects[j].get("id", "?")),
                    x_ov, y_ov,
                ))
    return warnings


def _build_objects_summary(
    objects: list[dict],
    *,
    overlap_warn_px: int = 0,
    include_content: bool = False,
) -> str:
    """objects summary text.

    overlap_warn_px > 0 이면 픽셀 단위 겹침 경고를 마지막에 추가.
    include_content 면 각 객체의 annotation.content 발췌를 포함.
    """
    lines = [f"# 현재 {len(objects)}개 객체", ""]
    for o in objects:
        ann = o.get("annotation") or {}
        typ = (ann.get("type")
               or o.get("detected_type") or "unknown")
        bn = o.get("bbox_norm") or [0, 0, 0, 0]
        hint = (o.get("detected_hint") or "").strip()
        extra = f" hint={hint!r}" if hint else ""
        lines.append(
            f"- {o.get('id','?')} [{typ}] "
            f"bbox_norm=[{bn[0]:.0f},{bn[1]:.0f},{bn[2]:.0f},{bn[3]:.0f}]"
            f"{extra}"
        )
        if include_content:
            content = (ann.get("content") or "").strip().replace("\n", " ⏎ ")
            if len(content) > 160:
                content = content[:157] + "..."
            if content:
                lines.append(f"    content={content!r}")

    if overlap_warn_px > 0:
        warns = _pairwise_overlap_warnings(objects, overlap_warn_px)
        if warns:
            lines.append("")
            lines.append(
                f"# ⚠ 겹침 경고 (가로·세로 둘 다 {overlap_warn_px}px 이상)"
            )
            lines.append(
                "  이 쌍은 bbox 가 실질적으로 겹쳐 있음 — 2-컬럼 경계 위반, "
                "중복 감지, 잘못된 경계일 가능성. 원본 이미지로 확인 후 필요하면 "
                "set_bbox / split / remove 로 수정."
            )
            for a_id, b_id, x_ov, y_ov in warns:
                lines.append(
                    f"- {a_id} ↔ {b_id}: x_overlap={x_ov}px, y_overlap={y_ov}px"
                )

    return "\n".join(lines) + "\n"


def _parse_bbox(raw) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        bn = tuple(float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    bn = (
        max(0.0, min(iu.NORM_MAX, bn[0])),
        max(0.0, min(iu.NORM_MAX, bn[1])),
        max(0.0, min(iu.NORM_MAX, bn[2])),
        max(0.0, min(iu.NORM_MAX, bn[3])),
    )
    if bn[2] <= bn[0] or bn[3] <= bn[1]:
        return None
    return bn


def _norm_to_obj_bbox(bn: tuple[float, float, float, float],
                      page_img: Image.Image) -> tuple[list, list]:
    w, h = page_img.size
    bbox_px = iu.clip_pixel(iu.norm_to_pixel(bn, w, h), w, h)
    return list(bn), list(bbox_px)


# ── action dispatcher (bbox 전용) ──

def _apply_action(
    issue: dict,
    objects: list[dict],
    page_img: Image.Image,
    cfg: dict,
    log: LogCb,
    *,
    page_idx: int,
) -> tuple[bool, list[str]]:
    """action 을 실행. (applied, changed_ids) 반환.

    changed_ids 는 annotation 을 다시 돌려야 하는 객체 id 목록.
    - set_bbox: [target_id]
    - split: [new sub ids...] (target_id 는 제거됨)
    - add_object: [new_id]
    - remove: []
    """
    action = issue.get("action")
    target_id = issue.get("target_id", "")
    reason = (issue.get("reason") or "")[:160]

    def _find_idx(oid: str) -> int | None:
        for i, o in enumerate(objects):
            if o.get("id") == oid:
                return i
        return None

    def _next_id() -> str:
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

    if action == "remove":
        i = _find_idx(target_id)
        if i is None:
            return False, []
        _emit(log, f"[review] remove {target_id} — {reason}")
        del objects[i]
        return True, []

    if action == "set_bbox":
        i = _find_idx(target_id)
        if i is None:
            return False, []
        bn = _parse_bbox(issue.get("new_bbox_norm"))
        if bn is None:
            _emit(log, f"[review] set_bbox skip — invalid new_bbox_norm ({reason})")
            return False, []
        obj = objects[i]
        obj["bbox_norm"], obj["bbox"] = _norm_to_obj_bbox(bn, page_img)
        obj["annotation"] = None
        obj["status"] = "detected"
        _emit(
            log,
            f"[review] set_bbox {target_id} → "
            f"[{bn[0]:.0f},{bn[1]:.0f},{bn[2]:.0f},{bn[3]:.0f}] — {reason}",
        )
        return True, [target_id]

    if action == "split":
        idx = _find_idx(target_id)
        if idx is None:
            return False, []
        subs_raw = issue.get("sub_bboxes") or []
        parsed: list[tuple[float, float, float, float]] = []
        for raw in subs_raw:
            bn = _parse_bbox(raw)
            if bn is not None:
                parsed.append(bn)
        if len(parsed) < 2:
            _emit(log, f"[review] split skip — sub_bboxes<2 ({reason})")
            return False, []

        del objects[idx]

        new_objs: list[dict] = []
        for bn in parsed:
            bbox_norm, bbox_px = _norm_to_obj_bbox(bn, page_img)
            new_obj = {
                "id": "",  # 아래서 발급
                "bbox_norm": bbox_norm,
                "bbox": bbox_px,
                "detected_type": issue.get("type_hint", "") or "unknown",
                "detected_hint": "review-split",
                "detect_prompt": "review split",
                "annotation": None,
                "status": "detected",
            }
            new_objs.append(new_obj)
        objects[idx:idx] = new_objs
        for o in new_objs:
            o["id"] = _next_id()
        _emit(log, f"[review] split {target_id} → {len(new_objs)}개 — {reason}")
        return True, [o["id"] for o in new_objs]

    if action == "add_object":
        bn = _parse_bbox(issue.get("new_bbox_norm"))
        if bn is None:
            _emit(log, f"[review] add_object skip — invalid new_bbox_norm ({reason})")
            return False, []
        bbox_norm, bbox_px = _norm_to_obj_bbox(bn, page_img)
        iou_thr = float(cfg.get("iou_threshold", 0.5))
        for o in objects:
            if iu.iou(tuple(bbox_px), tuple(o.get("bbox", [0,0,0,0]))) >= iou_thr:
                _emit(log, f"[review] add_object skip — 중복 ({reason})")
                return False, []
        new_obj = {
            "id": _next_id(),
            "bbox_norm": bbox_norm,
            "bbox": bbox_px,
            "detected_type": issue.get("type_hint", "") or "unknown",
            "detected_hint": "review-add",
            "detect_prompt": "review add_object",
            "annotation": None,
            "status": "detected",
        }
        insert_at = len(objects)
        for i, o in enumerate(objects):
            if o["bbox"][1] > bbox_px[1]:
                insert_at = i
                break
        objects.insert(insert_at, new_obj)
        _emit(
            log,
            f"[review] add_object {new_obj['id']} "
            f"@[{bn[0]:.0f},{bn[1]:.0f},{bn[2]:.0f},{bn[3]:.0f}] — {reason}",
        )
        return True, [new_obj["id"]]

    return False, []


# ── bbox review loop ──

def review_bboxes(
    objects: list[dict],
    page_img: Image.Image,
    page_ckpt: Path,
    cfg: dict,
    log: LogCb,
    *,
    page_idx: int,
    profile_text: str = "",
) -> list[dict]:
    """Gemini bbox review loop. objects in-place 수정. annotation 은 건드리지 않음.

    종료 조건: overall_ok=True / 신규 action 없음 / max_rounds 도달 / 호출 실패.
    """
    max_rounds = int(cfg.get("review_max_rounds", 2))
    per_round_cap = int(cfg.get("review_actions_per_round", 5))
    review_side = int(cfg.get("review_max_image_side", 1200))
    overlap_px = int(cfg.get("overlap_warn_px", 15))
    model = cfg.get("gemini_model", "gemini-2.5-pro")

    if max_rounds <= 0:
        return objects

    profile_block = ""
    if profile_text and profile_text.strip():
        profile_block = f"\n# 문서 프로파일 (참고)\n\n{profile_text.strip()}\n"
    prompt = _REVIEW_PROMPT_TEMPLATE.format(profile_block=profile_block)

    def _maybe_resize(img: Image.Image) -> Image.Image:
        w_img, h_img = img.size
        m = max(w_img, h_img)
        if m <= review_side:
            return img
        s = review_side / m
        return img.resize(
            (max(1, int(w_img * s)), max(1, int(h_img * s))),
            Image.LANCZOS,
        )

    for round_i in range(1, max_rounds + 1):
        _raise_if_cancelled()

        summary_bytes = _build_objects_summary(
            objects, overlap_warn_px=overlap_px,
        ).encode("utf-8")

        annotated_img = iu.draw_annotated(page_img, objects)
        buf = io.BytesIO()
        _maybe_resize(annotated_img).save(buf, format="PNG")
        annotated_bytes = buf.getvalue()

        buf2 = io.BytesIO()
        _maybe_resize(page_img).save(buf2, format="PNG")
        original_bytes = buf2.getvalue()

        try:
            out = run_gemini_task(
                prompt=prompt,
                inputs={
                    "original.png": original_bytes,
                    "annotated.png": annotated_bytes,
                    "objects_summary.txt": summary_bytes,
                },
                output_schema=REVIEW_SCHEMA,
                model=model,
            )
        except CodexRunError as exc:
            _emit(log, f"[review] round {round_i} 호출 실패: {exc}")
            return objects

        try:
            data = json.loads(out["result.json"].decode("utf-8"))
        except (KeyError, json.JSONDecodeError) as exc:
            _emit(log, f"[review] round {round_i} 응답 파싱 실패: {exc}")
            return objects

        if data.get("overall_ok"):
            _emit(log, f"[review] round {round_i} overall_ok — bbox 확정")
            return objects

        issues = (data.get("issues") or [])[:per_round_cap]
        if not issues:
            _emit(log, f"[review] round {round_i} — 이슈 없음 → bbox 확정")
            return objects

        _emit(log, f"[review] round {round_i} — {len(issues)} 이슈 처리")

        any_applied = False
        for issue in issues:
            ok, _changed = _apply_action(
                issue, objects, page_img, cfg, log,
                page_idx=page_idx,
            )
            if ok:
                any_applied = True

        if not any_applied:
            _emit(log, f"[review] round {round_i} — 적용 없음 → bbox 확정")
            return objects

        # 변경 반영한 annotated 저장 (다음 라운드 입력용)
        annotated = iu.draw_annotated(page_img, objects)
        annotated.save(page_ckpt / "annotated.png", format="PNG")
        state_path = page_ckpt / "objects.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {"page": page_idx, "page_size": list(page_img.size)}
        state["objects"] = objects
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    _emit(log, f"[review] max_rounds({max_rounds}) 도달 — bbox 확정")
    return objects


# ── Phase 3: content-aware review (annotation 이후) ──

_REVIEW_ANN_PROMPT_TEMPLATE = """\
첨부:
- `original.png` : 원본 페이지
- `annotated.png`: bbox + id/type 오버레이된 페이지
- `objects_summary.txt`: 각 객체의 bbox + annotation content 발췌 + 겹침 경고
{profile_block}
이제 각 객체가 **정확한 content 를 담고 있는지** 도 함께 판정하라.
- bbox 경계 문제 (잘림, 인접 침범, 2-column 넘나듦)
- content 가 이미지의 해당 영역과 structurally 일치하는지 (줄 누락, 엉뚱한 텍스트 혼입)
- 누락된 영역, 잘못 감지된 객체, 중복 영역

문제 있으면 issues 에 action 을 넣어라. 모든 좌표는 원본 기준 [0,1000] 정규화.
사소한 차이는 무시.

action 에 따라 annotation 은 자동 재생성된다 — annotation 내용을 직접 제안할
필요는 없고, 어떤 bbox 를 어떻게 바꿀지만 명확히 지정하라.
"""


def review_with_annotations(
    objects: list[dict],
    page_img: Image.Image,
    page_ckpt: Path,
    cfg: dict,
    log: LogCb,
    *,
    page_idx: int,
    annotate_fn: Callable[[dict, list[dict]], None],
    profile_text: str = "",
) -> list[dict]:
    """Phase 3: annotation 이후 content-aware review-annotation 사이클.

    최소 `review_phase2_min_rounds` 회 (기본 1), 최대 `review_phase2_max_rounds`
    회 (기본 3) 루프. 각 라운드에서 변경된 (set_bbox/split/add_object) 객체에
    대해서만 annotate_fn 을 재호출해 비용 절감.

    종료 조건:
      - round > min_rounds 이고 (overall_ok=True / issues 비어있음 / 적용 0건)
      - max_rounds 도달
    """
    min_rounds = max(1, int(cfg.get("review_phase2_min_rounds", 1)))
    max_rounds = max(min_rounds, int(cfg.get("review_phase2_max_rounds", 3)))
    per_round_cap = int(cfg.get("review_actions_per_round", 5))
    review_side = int(cfg.get("review_max_image_side", 1200))
    overlap_px = int(cfg.get("overlap_warn_px", 15))
    model = cfg.get("gemini_model", "gemini-2.5-pro")

    profile_block = ""
    if profile_text and profile_text.strip():
        profile_block = f"\n# 문서 프로파일 (참고)\n\n{profile_text.strip()}\n"
    prompt = _REVIEW_ANN_PROMPT_TEMPLATE.format(profile_block=profile_block)

    def _maybe_resize(img: Image.Image) -> Image.Image:
        w_img, h_img = img.size
        m = max(w_img, h_img)
        if m <= review_side:
            return img
        s = review_side / m
        return img.resize(
            (max(1, int(w_img * s)), max(1, int(h_img * s))),
            Image.LANCZOS,
        )

    state_path = page_ckpt / "objects.json"

    for round_i in range(1, max_rounds + 1):
        _raise_if_cancelled()

        summary_bytes = _build_objects_summary(
            objects,
            overlap_warn_px=overlap_px,
            include_content=True,
        ).encode("utf-8")

        annotated_img = iu.draw_annotated(page_img, objects)
        buf = io.BytesIO()
        _maybe_resize(annotated_img).save(buf, format="PNG")
        annotated_bytes = buf.getvalue()
        buf2 = io.BytesIO()
        _maybe_resize(page_img).save(buf2, format="PNG")
        original_bytes = buf2.getvalue()

        try:
            out = run_gemini_task(
                prompt=prompt,
                inputs={
                    "original.png": original_bytes,
                    "annotated.png": annotated_bytes,
                    "objects_summary.txt": summary_bytes,
                },
                output_schema=REVIEW_SCHEMA,
                model=model,
            )
        except CodexRunError as exc:
            _emit(log, f"[review2] round {round_i} 호출 실패: {exc}")
            return objects

        try:
            data = json.loads(out["result.json"].decode("utf-8"))
        except (KeyError, json.JSONDecodeError) as exc:
            _emit(log, f"[review2] round {round_i} 응답 파싱 실패: {exc}")
            return objects

        issues = (data.get("issues") or [])[:per_round_cap]
        overall_ok = bool(data.get("overall_ok"))

        if overall_ok and round_i >= min_rounds:
            _emit(log, f"[review2] round {round_i} overall_ok — 종료")
            return objects
        if not issues and round_i >= min_rounds:
            _emit(log, f"[review2] round {round_i} — 이슈 없음 → 종료")
            return objects

        _emit(log, f"[review2] round {round_i} — {len(issues)} 이슈 처리")

        changed_ids: list[str] = []
        any_applied = False
        for issue in issues:
            ok, changed = _apply_action(
                issue, objects, page_img, cfg, log,
                page_idx=page_idx,
            )
            if ok:
                any_applied = True
                changed_ids.extend(changed)

        if not any_applied and round_i >= min_rounds:
            _emit(log, f"[review2] round {round_i} — 적용 없음 → 종료")
            return objects

        # 변경된 객체만 재annotate (annotation 은 set_bbox 등에서 None 처리됨)
        if changed_ids:
            _emit(log, f"[review2] 재annotate {len(changed_ids)}개: {changed_ids}")
            id_set = set(changed_ids)
            targets = [o for o in objects if o.get("id") in id_set]
            parallel = max(1, int(cfg.get("annotate_parallel", 1)))
            jitter_s = float(cfg.get("annotate_jitter_s", 0.5))

            def _one(target):
                if parallel > 1 and jitter_s > 0:
                    time.sleep(random.uniform(0, jitter_s))
                annotate_fn(target, objects)

            if parallel > 1 and len(targets) > 1:
                with ContextThreadPoolExecutor(max_workers=parallel) as pool:
                    for fut in [pool.submit(_one, t) for t in targets]:
                        fut.result()
            else:
                for t_obj in targets:
                    _one(t_obj)

        # 중간 저장
        annotated = iu.draw_annotated(page_img, objects)
        annotated.save(page_ckpt / "annotated.png", format="PNG")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {"page": page_idx, "page_size": list(page_img.size)}
        state["objects"] = objects
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    _emit(log, f"[review2] max_rounds({max_rounds}) 도달 — 종료")
    return objects


# 구 API 호환
def review_and_fix(
    objects: list[dict],
    page_img: Image.Image,
    page_ckpt: Path,
    cfg: dict,
    log: LogCb,
    *,
    detect_fn=None,
    normalize_fn=None,
    annotate_fn=None,
    page_idx: int,
    profile_text: str = "",
) -> list[dict]:
    return review_bboxes(
        objects, page_img, page_ckpt, cfg, log,
        page_idx=page_idx, profile_text=profile_text,
    )
