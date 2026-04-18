"""objects.json (페이지별) → doc.md + structure.json 조립.

Deterministic. LLM 호출 없음.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable


LogCb = Callable[[str], None]


def _emit(log: LogCb | None, msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


def _marker_for(type_str: str, obj_id: str) -> str:
    mapping = {
        "figure": "FIG",
        "table": "TAB",
        "equation": "EQ",
        "code": "CODE",
        "form": "FORM",
    }
    tag = mapping.get(type_str, "ASSET")
    return f"[[{tag}:{obj_id}]]"


def _md_alt_escape(text: str) -> str:
    """markdown alt text 에서 링크 파싱을 깨지 않도록 `[`/`]` 이스케이프."""
    return text.replace("[", r"\[").replace("]", r"\]")


def _truncate_alt(text: str, limit: int = 400) -> str:
    """alt text 를 limit 내로 줄이되 문장 경계에서 자른다.

    한국어는 정보밀도가 높아 200자 제한은 중간 문장에서 끊김. 400자로 여유.
    마지막 마침표/느낌표/물음표 위치를 찾아 거기서 끊고, 없으면 그냥 잘라 `…` 추가.
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    # 문장 종결 후보 중 가장 뒤쪽
    best = max((head.rfind(ch) for ch in ("다.", "요.", ". ", "! ", "? ", ".")), default=-1)
    if best >= limit // 2:
        return head[: best + 1].rstrip()
    return head.rstrip() + "…"


def _emit_object_md(obj: dict, page_assets_dir: str) -> str:
    """한 객체를 markdown 조각으로 변환. reading_order 는 호출 쪽에서 결정.

    page_assets_dir: 이 객체가 속한 페이지의 자산 디렉토리 (예: ``assets/foo/pages/page_001``).
        obj["crop"] 은 ``crops/ID.png`` 형태라 결합하면 최종 링크가 만들어진다.
    """
    ann = obj.get("annotation") or {}
    typ = ann.get("type") or obj.get("detected_type") or "unknown"
    content = (ann.get("content") or "").strip()
    oid = obj.get("id", "?")
    crop_rel = obj.get("crop")

    if typ == "text":
        return content + "\n\n"

    if typ == "equation":
        latex = ann.get("latex") or content
        verified = ann.get("render_verified", None)
        # 검증 실패 시에는 마커로 참조하되 이미지 경로도 기록해 사용자가 확인 가능.
        if latex:
            return f"$$\n{latex}\n$$\n\n"
        return _marker_for(typ, oid) + "\n\n"

    if typ == "table":
        html = ann.get("html") or content
        if html:
            return html.strip() + "\n\n"
        return _marker_for(typ, oid) + "\n\n"

    if typ == "figure":
        desc = ann.get("description") or content or ""
        desc_clean = " ".join(desc.split())
        if crop_rel:
            rel = f"{page_assets_dir}/{crop_rel}"
            alt_raw = _truncate_alt(desc_clean) if desc_clean else oid
            alt = _md_alt_escape(alt_raw)
            return f"![{alt}]({rel})\n\n"
        return _marker_for(typ, oid) + "\n\n"

    if typ == "code":
        # 코드 블록 — 첫 줄에 lang 주석 있으면 파싱, 아니면 ``` 그대로.
        lines = content.splitlines()
        lang = ""
        body = content
        if lines and lines[0].startswith("// lang:"):
            lang = lines[0].split(":", 1)[1].strip()
            body = "\n".join(lines[1:])
        return f"```{lang}\n{body}\n```\n\n"

    if typ == "form":
        return content + "\n\n"

    # unknown / fallback
    if content:
        return content + "\n\n"
    return _marker_for(typ, oid) + "\n\n"


def assemble(
    page_states: list[tuple[int, dict]],
    assets_root: str = "assets",
    log: LogCb | None = None,
) -> tuple[str, dict]:
    """페이지별 state 를 받아 (doc_md, structure) 반환.

    page_states: [(page_idx, objects_json_dict), ...] 오름차순.
    assets_root: 링크가 참조할 자산 루트 (예: ``{stem}-assets``). 페이지별
        서브디렉토리는 ``{assets_root}/pages/page_NNN`` 으로 자동 구성.
    """
    md_parts: list[str] = []
    structure: dict = {"pages": []}

    for page_idx, state in page_states:
        objects = state.get("objects", []) or []
        page_dir = f"{assets_root}/pages/page_{page_idx:03d}"
        md_parts.append(f"\n\n<!-- page {page_idx} -->\n\n")
        for obj in objects:
            md_parts.append(_emit_object_md(obj, page_dir))

        structure["pages"].append({
            "page": page_idx,
            "page_size": state.get("page_size", [0, 0]),
            "object_count": len(objects),
            "objects": [
                {
                    "id": o.get("id"),
                    "bbox": o.get("bbox"),
                    "bbox_norm": o.get("bbox_norm"),
                    "type": (o.get("annotation") or {}).get("type") or o.get("detected_type"),
                    "status": o.get("status"),
                    "render_verified": (o.get("annotation") or {}).get("render_verified"),
                    "crop": o.get("crop"),
                }
                for o in objects
            ],
        })

    doc_md = "".join(md_parts).strip() + "\n"
    _emit(log, f"[assemble] doc.md {len(doc_md)} 자, 페이지 {len(page_states)}")
    return doc_md, structure
