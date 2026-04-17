"""Step 9: 교수자의 시험 관련 코멘트 + 슬라이드별 강조 수준 추출.

전체 녹취록을 1회 스캔해서:
- '시험', '출제', '평가', '반드시 외워', '중요' 등 명시적 발화를 professor_exam_comments로
- 모든 슬라이드에 대한 high/medium/low 강조 평가를 page_emphasis로
"""

import json
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, run_codex_task

from _lib.compact_schemas import build_exam_cues_schema


def _emit(log_callback: Callable[[str], None] | None, msg: str) -> None:
    if log_callback is None:
        return
    try:
        log_callback(msg)
    except Exception:
        pass


def _build_exam_cues_prompt(
    numbered: dict[str, dict],
    mapping: dict[int, list[dict]],
    slides_data: dict,
    lecture_summary: dict,
) -> str:
    lines: list[str] = []
    lines.append(
        "당신은 강의 녹취록에서 **교수자가 시험·평가·출제에 대해 명시적으로 "
        "언급한 발화**를 추출하고, **각 슬라이드의 강조 수준**을 평가합니다."
    )
    lines.append("")

    # 강의 전역 컨텍스트
    lines.append("## 강의 전체 컨텍스트")
    lines.append("")
    theme = lecture_summary.get("overall_theme", "")
    if theme:
        lines.append(f"**핵심 주제**: {theme}")
        lines.append("")
    mechs = lecture_summary.get("key_mechanisms", []) or []
    if mechs:
        lines.append("**핵심 메커니즘**:")
        for m in mechs:
            lines.append(f"- {m}")
        lines.append("")

    # 슬라이드 전체 리스트 (page_emphasis 모든 엔트리 필요)
    pages = slides_data.get("pages", []) or []
    sources = slides_data.get("sources", []) or []
    lines.append(f"## 슬라이드 ({len(pages)}개)")
    lines.append("")
    if len(sources) > 1:
        pages_by_order: dict[int, list[dict]] = {}
        for p in pages:
            pages_by_order.setdefault(p.get("source_pdf_order", 0), []).append(p)
        for src in sources:
            order = src.get("order", 0)
            lines.append(f"- 📄 **[{order}] {src.get('pdf', '')}**")
            for p in pages_by_order.get(order, []):
                lines.append(
                    f"  - Page {p.get('index')}: {p.get('title', '')}"
                )
    else:
        for p in pages:
            lines.append(f"- Page {p.get('index')}: {p.get('title', '')}")
    lines.append("")

    # 페이지 → 녹취 라인 매핑 (LLM이 슬라이드 귀속 판단에 사용)
    lines.append("## 슬라이드 → 녹취 라인 매핑")
    lines.append("")
    for idx in sorted(mapping.keys()):
        rngs = mapping[idx]
        parts = [
            f"{r.get('source')}:{r.get('start_line')}-{r.get('end_line')}"
            for r in rngs
        ]
        lines.append(
            f"- Page {idx}: {', '.join(parts) if parts else '(없음)'}"
        )
    lines.append("")

    # 전체 전사록 (line 번호 prefix 포함)
    lines.append("## 전사록 (line 번호 prefix 포함)")
    lines.append("")
    for src in sorted(numbered.keys()):
        info = numbered[src]
        path = Path(info["path"])
        try:
            text = path.read_text(encoding="utf-8").rstrip()
        except Exception:
            text = "(파일 읽기 실패)"
        lines.append(f"### {src}")
        lines.append("```")
        lines.append(text)
        lines.append("```")
        lines.append("")

    lines.append("## 작업")
    lines.append("")
    lines.append(
        "`outputs/exam_cues.json` 에 두 필드를 담아 저장:"
    )
    lines.append("")
    lines.append("### 1. `professor_exam_comments`")
    lines.append("")
    lines.append(
        "교수가 **시험, 평가, 출제, 반드시 외울 것, 꼭 알아야 할 것, "
        "시험에 나온다** 등을 **명시적으로** 언급한 발화만 포함."
    )
    lines.append("")
    lines.append(
        "- 미묘한 뉘앙스·추정은 절대 넣지 말 것. '중요하다' 자체는 강조일 수 있으나, "
        "시험 관련성이 **명시**된 경우만 포함."
    )
    lines.append(
        "- 각 엔트리: source, start_line, end_line, paraphrase (한 줄 요약), "
        "slide_page (관련 슬라이드 index — 매핑에서 추정, 모호하면 0)"
    )
    lines.append("- 언급이 없으면 **빈 배열**.")
    lines.append("")
    lines.append("### 2. `page_emphasis`")
    lines.append("")
    lines.append(
        f"**반드시 {len(pages)}개 슬라이드 전부에 대한 엔트리**를 포함해야 함."
    )
    lines.append("")
    lines.append("- `high`: 교수가 반복/강조했거나 시험 언급이 있는 페이지")
    lines.append("- `medium`: 일반적으로 설명한 페이지")
    lines.append("- `low`: 녹취가 없거나 교수가 빠르게 넘긴 페이지")
    lines.append("- `reason`: 한 줄 근거")
    lines.append("")
    lines.append("언어: 한국어 (원 녹취록 언어가 영어면 그대로 영어 OK).")

    return "\n".join(lines) + "\n"


def extract_exam_cues(
    numbered: dict[str, dict],
    mapping: dict[int, list[dict]],
    slides_data: dict,
    lecture_summary: dict,
    model: str,
    reasoning_effort: str,
    service_tier: str = "default",
    timeout: int = 1800,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """LLM 1회 호출로 exam cues 추출. returns dict (schema와 동일 구조)."""
    prompt = _build_exam_cues_prompt(
        numbered=numbered, mapping=mapping,
        slides_data=slides_data, lecture_summary=lecture_summary,
    )
    schema = build_exam_cues_schema(list(numbered.keys()))

    _emit(log_callback, "[step9] Codex 호출 (exam_cues)...")
    result = run_codex_task(
        prompt=prompt,
        inputs={"_marker.txt": "exam_cues"},
        expected_outputs=["exam_cues.json"],
        output_schema=schema,
        model=model,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
        timeout=timeout,
    )
    raw = result.get("exam_cues.json", b"").decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexRunError(
            f"[exam_cues] JSON 파싱 실패: {exc}\n내용 앞 300: {raw[:300]}"
        ) from exc

    comments = data.get("professor_exam_comments", [])
    emphasis = data.get("page_emphasis", [])
    if not isinstance(comments, list):
        comments = []
    if not isinstance(emphasis, list):
        emphasis = []

    _emit(
        log_callback,
        f"[step9] 시험 코멘트 {len(comments)}개, emphasis {len(emphasis)}페이지",
    )
    return {
        "professor_exam_comments": comments,
        "page_emphasis": emphasis,
    }
