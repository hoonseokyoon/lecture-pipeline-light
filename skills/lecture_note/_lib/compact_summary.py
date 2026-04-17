"""Step 10c: 마지막 '핵심 요약노트' 섹션 작성.

Input:
- lecture_summary (step1b)
- compact_pages (step10: narrative + footnotes)
- exam_cues.professor_exam_comments

Output: {professor_exam_notes_md, compressed_prose_md, summary_table_md}
"""

import json
from typing import Callable

from codex_runner import CodexRunError, run_codex_task

from _lib.compact_schemas import COMPACT_SUMMARY_SCHEMA


def _emit(log_callback: Callable[[str], None] | None, msg: str) -> None:
    if log_callback is None:
        return
    try:
        log_callback(msg)
    except Exception:
        pass


def _build_summary_prompt(
    lecture_summary: dict,
    compact_pages: dict[int, dict],
    exam_cues: dict,
    slides_data: dict,
) -> str:
    lines: list[str] = []
    lines.append(
        "당신은 강의의 **시험 직전 치트시트**를 작성합니다. 재작성된 슬라이드 "
        "내용과 교수자의 시험 코멘트를 종합해, 핵심 요약노트를 만드세요."
    )
    lines.append("")

    # 전역 컨텍스트
    lines.append("## 강의 전체 컨텍스트 (lecture_summary)")
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

    # 슬라이드 목록 (importance level 포함)
    importance_map: dict[int, str] = {}
    for item in lecture_summary.get("page_importance", []) or []:
        if isinstance(item, dict) and isinstance(item.get("page"), int):
            lvl = item.get("level", "")
            if lvl:
                importance_map[item["page"]] = lvl

    pages = slides_data.get("pages", []) or []
    lines.append(f"## 슬라이드 목록 ({len(pages)}개)")
    lines.append("")
    for p in pages:
        idx = p.get("index")
        title = p.get("title", "")
        imp = importance_map.get(idx, "normal")
        lines.append(f"- Page {idx} [{imp}]: {title}")
    lines.append("")

    # compact 재작성본 (narrative + footnotes)
    lines.append("## 재작성된 슬라이드 내용 (compact narratives)")
    lines.append("")
    for idx in sorted(compact_pages.keys()):
        data = compact_pages[idx]
        narrative = data.get("narrative_markdown", "")
        footnotes = data.get("footnotes", []) or []
        lines.append(f"### Page {idx}")
        lines.append("")
        if narrative:
            lines.append(narrative.rstrip())
            lines.append("")
        if footnotes:
            lines.append("**각주**:")
            for fn in footnotes:
                marker = fn.get("marker", "")
                text = fn.get("text", "")
                lines.append(f"- [{marker}] {text}")
            lines.append("")

    # 교수자 시험 코멘트
    comments = exam_cues.get("professor_exam_comments", []) or []
    lines.append("## 교수자의 시험 관련 코멘트")
    lines.append("")
    if comments:
        for c in comments:
            src = c.get("source", "")
            s = c.get("start_line", "")
            e = c.get("end_line", "")
            ph = c.get("paraphrase", "")
            sp = c.get("slide_page", 0)
            slide_tag = f" (Slide {sp})" if sp else ""
            lines.append(f"- [{src}:{s}-{e}]{slide_tag} {ph}")
    else:
        lines.append("(시험 관련 명시 언급 없음)")
    lines.append("")

    # 작업 지시
    lines.append("## 작업")
    lines.append("")
    lines.append(
        "`outputs/compact_summary.json`에 세 필드를 담아 저장."
    )
    lines.append("")
    lines.append("### 1. `professor_exam_notes_md`")
    lines.append("")
    lines.append(
        "- 위 '교수자의 시험 관련 코멘트' 섹션에 언급이 있으면, 이를 "
        "**정리된 markdown 섹션**으로 재구성. 비슷한 코멘트는 묶고, 관련 "
        "슬라이드 번호를 함께 명시."
    )
    lines.append(
        "- 언급이 **전혀 없으면** 빈 문자열 (`\"\"`). **추측하지 말 것**."
    )
    lines.append(
        "- 헤더는 사용 가능 (#### 이하). 본문에 `Slide N` 식 레퍼런스 포함."
    )
    lines.append("")
    lines.append("### 2. `compressed_prose_md`")
    lines.append("")
    lines.append(
        "- 강의 전체를 **시험 직전에 훑어보는 한 페이지 치트시트**로 압축한 "
        "prose (markdown, 헤더 금지)."
    )
    lines.append(
        "- 강의 핵심 주제 → 주요 메커니즘 → 결론 순으로 **연결성 있는 문단**으로. "
        "불릿 리스트만 늘어놓는 건 금지. 서술형으로."
    )
    lines.append(
        "- 3~8문단 정도. 중요한 용어는 **굵게**."
    )
    lines.append("")
    lines.append("### 3. `summary_table_md`")
    lines.append("")
    lines.append(
        "- 핵심 포인트를 markdown 파이프 테이블로 정리. 컬럼은 **강의 맥락에 "
        "맞게 LLM이 결정** (예: `주제 | 메커니즘 | 키워드 | 시험 관련성`, "
        "또는 `단계 | 작용 | 임상 의의`)."
    )
    lines.append(
        "- 행 수: 강의 중요 포인트 만큼 (5~15 권장). 너무 세밀하지 않게."
    )
    lines.append(
        "- 예시 구조:\n"
        "  ```\n"
        "  | 컬럼1 | 컬럼2 | 컬럼3 |\n"
        "  |-------|-------|-------|\n"
        "  | ... | ... | ... |\n"
        "  ```"
    )
    lines.append("")
    lines.append("언어: 한국어 기준.")
    return "\n".join(lines) + "\n"


def build_compact_summary(
    lecture_summary: dict,
    compact_pages: dict[int, dict],
    exam_cues: dict,
    slides_data: dict,
    model: str,
    reasoning_effort: str,
    service_tier: str = "default",
    timeout: int = 1800,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    prompt = _build_summary_prompt(
        lecture_summary=lecture_summary,
        compact_pages=compact_pages,
        exam_cues=exam_cues,
        slides_data=slides_data,
    )
    _emit(log_callback, "[step10c] 핵심 요약노트 Codex 호출...")
    result = run_codex_task(
        prompt=prompt,
        inputs={"_marker.txt": "compact_summary"},
        expected_outputs=["compact_summary.json"],
        output_schema=COMPACT_SUMMARY_SCHEMA,
        model=model,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
        timeout=timeout,
    )
    raw = result.get("compact_summary.json", b"").decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexRunError(
            f"[compact_summary] JSON 파싱 실패: {exc}\n내용 앞 300: {raw[:300]}"
        ) from exc

    return {
        "professor_exam_notes_md": data.get("professor_exam_notes_md", "") or "",
        "compressed_prose_md": data.get("compressed_prose_md", "") or "",
        "summary_table_md": data.get("summary_table_md", "") or "",
    }
