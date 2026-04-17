"""Step 10b: 기존 glossary를 카테고리 단위로 재구성 + 슬라이드 크로스 참조.

기존 step4_glossary.md와 compact_pages의 referenced_terms를 모아,
강의 맥락에 맞는 카테고리(기본 개념 / 핵심 메커니즘 / 등)로 재분류.
각 용어에 언급된 슬라이드 index 리스트 부착.
"""

import json
from typing import Callable

from codex_runner import CodexRunError, run_codex_task

from _lib.compact_schemas import COMPACT_GLOSSARY_SCHEMA


def _emit(log_callback: Callable[[str], None] | None, msg: str) -> None:
    if log_callback is None:
        return
    try:
        log_callback(msg)
    except Exception:
        pass


def _collect_term_mentions(
    compact_pages: dict[int, dict],
) -> dict[str, list[int]]:
    """compact_pages의 referenced_terms를 {term_lower: [page_idx, ...]} 로 집계."""
    mentions: dict[str, list[int]] = {}
    for idx in sorted(compact_pages.keys()):
        data = compact_pages[idx]
        terms = data.get("referenced_terms", []) or []
        for t in terms:
            if not isinstance(t, str) or not t.strip():
                continue
            key = t.strip()
            mentions.setdefault(key, [])
            if idx not in mentions[key]:
                mentions[key].append(idx)
    return mentions


def _build_glossary_reorg_prompt(
    glossary_md: bytes,
    term_mentions: dict[str, list[int]],
    slides_data: dict,
    lecture_summary: dict,
) -> str:
    lines: list[str] = []
    lines.append(
        "당신은 강의의 **용어집을 체계적으로 재정리**합니다. 원본 용어집의 "
        "항목을 주제 단위 카테고리로 묶고, 각 용어에 언급된 슬라이드 번호를 "
        "크로스 참조로 부착합니다."
    )
    lines.append("")

    # 강의 컨텍스트
    lines.append("## 강의 전체 컨텍스트")
    lines.append("")
    theme = lecture_summary.get("overall_theme", "")
    if theme:
        lines.append(f"**주제**: {theme}")
        lines.append("")
    mechs = lecture_summary.get("key_mechanisms", []) or []
    if mechs:
        lines.append("**핵심 메커니즘**:")
        for m in mechs:
            lines.append(f"- {m}")
        lines.append("")

    # 슬라이드 리스트 (title 참고용)
    pages = slides_data.get("pages", []) or []
    lines.append(f"## 슬라이드 목록 ({len(pages)}개)")
    lines.append("")
    for p in pages:
        lines.append(f"- Page {p.get('index')}: {p.get('title', '')}")
    lines.append("")

    # 원 용어집
    lines.append("## 원본 용어집 (`glossary.md`)")
    lines.append("")
    gtext = (
        glossary_md.decode("utf-8", errors="replace").rstrip() if glossary_md else ""
    )
    if gtext:
        lines.append("```markdown")
        lines.append(gtext)
        lines.append("```")
    else:
        lines.append("(용어집 비어있음)")
    lines.append("")

    # 각 페이지의 referenced_terms 집계
    lines.append("## 각 슬라이드에서 언급된 핵심 용어 (compact 재작성본에서 추출)")
    lines.append("")
    if term_mentions:
        for term, pages_list in sorted(term_mentions.items()):
            lines.append(f"- **{term}** — Pages: {pages_list}")
    else:
        lines.append("(referenced_terms 없음)")
    lines.append("")

    # 작업 지시
    lines.append("## 작업")
    lines.append("")
    lines.append(
        "`outputs/compact_glossary.json`에 `categories` 배열을 담아 저장."
    )
    lines.append("")
    lines.append("### 지시사항")
    lines.append("")
    lines.append(
        "- **원 용어집의 모든 중요 항목을 포함**. referenced_terms에만 있고 "
        "glossary에 없는 경우, 맥락에서 정의 가능하면 추가."
    )
    lines.append(
        "- 카테고리 3~7개 권장. 예시: `기본 개념`, `핵심 메커니즘`, "
        "`세부 용어`, `등장 인물·연도`, `약어·기호`, `임상 적용` 등. "
        "강의 주제에 맞게 유연하게 결정."
    )
    lines.append(
        "- 각 카테고리에 `name`, `description`(한 줄), `terms` 배열."
    )
    lines.append(
        "- 각 term 엔트리: `term`(용어), `definition`(한 줄), "
        "`mentioned_on_slides`(위 매핑 참고)."
    )
    lines.append(
        "- 중복 최소화: 같은 개념의 동의어는 하나로 통합하고 정의에서 구 표현 언급."
    )
    lines.append(
        "- 카테고리 안에서 용어 정렬 순서는 논리적 연관성 우선 (알파벳 아님)."
    )
    lines.append("")
    lines.append("언어: 원 용어집과 동일 (보통 한국어).")
    return "\n".join(lines) + "\n"


def reorganize_glossary(
    glossary_md: bytes,
    compact_pages: dict[int, dict],
    slides_data: dict,
    lecture_summary: dict,
    model: str,
    reasoning_effort: str,
    service_tier: str = "default",
    timeout: int = 1800,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """LLM 호출로 용어집 재구성. returns {categories: [...]}"""
    term_mentions = _collect_term_mentions(compact_pages)
    prompt = _build_glossary_reorg_prompt(
        glossary_md=glossary_md,
        term_mentions=term_mentions,
        slides_data=slides_data,
        lecture_summary=lecture_summary,
    )
    _emit(log_callback, "[step10b] 용어집 재구성 Codex 호출...")
    result = run_codex_task(
        prompt=prompt,
        inputs={"_marker.txt": "compact_glossary"},
        expected_outputs=["compact_glossary.json"],
        output_schema=COMPACT_GLOSSARY_SCHEMA,
        model=model,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
        timeout=timeout,
    )
    raw = result.get("compact_glossary.json", b"").decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexRunError(
            f"[compact_glossary] JSON 파싱 실패: {exc}\n내용 앞 300: {raw[:300]}"
        ) from exc

    categories = data.get("categories", [])
    if not isinstance(categories, list):
        categories = []
    _emit(
        log_callback,
        f"[step10b] 완료 — {len(categories)} 카테고리",
    )
    return {"categories": categories}
