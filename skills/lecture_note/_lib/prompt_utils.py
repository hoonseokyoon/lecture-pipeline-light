"""Multi-source prompt 공통 헬퍼.

여러 prompt 빌더(`align`, `review`, `lecture_summary`, `reconcile`)가 공유하는
"입력 PDF 구성" + "녹취록 순서" + "multi-source 규칙" 블록.
"""

from __future__ import annotations


def build_source_context_block(
    slides_data: dict,
    numbered_txts: dict[str, dict],
) -> list[str]:
    """Multi-source 컨텍스트 블록 (markdown lines). 단일 소스면 빈 리스트.

    `slides_data["sources"]` 의 PDF 정보와 `numbered_txts` 의 녹취록 순서를
    표시. 순서는 dict insertion order (=입력 순서) 를 따름.
    """
    lines: list[str] = []
    sources = slides_data.get("sources", []) or []
    is_multi_pdf = len(sources) > 1
    is_multi_txt = len(numbered_txts) > 1

    if not (is_multi_pdf or is_multi_txt):
        return lines

    lines.append("## 입력 구성 (순서 중요)")
    lines.append("")

    if is_multi_pdf:
        lines.append("**PDF 구성** (입력 순서대로 강의 진행):")
        for src in sources:
            order = src.get("order", 0)
            pdf = src.get("pdf", "")
            g_start = src.get("global_start", 0)
            g_end = src.get("global_end", 0)
            cnt = src.get("page_count", 0)
            lines.append(
                f"- [{order}] {pdf} — global pages {g_start}~{g_end} "
                f"({cnt} 페이지)"
            )
        lines.append("")
    elif sources:
        # 단일 PDF 지만 source 메타는 있는 경우
        src = sources[0]
        lines.append(f"**PDF**: {src.get('pdf', '')} ({src.get('page_count', 0)} 페이지)")
        lines.append("")

    if is_multi_txt:
        lines.append("**녹취록 순서** (입력 순서 = 수업 시간 순):")
        for i, (src_name, info) in enumerate(numbered_txts.items()):
            line_count = info.get("line_count", 0) if isinstance(info, dict) else 0
            lines.append(f"- [{i}] {src_name} ({line_count} lines)")
        lines.append("")

    return lines


def build_multi_source_rules(
    slides_data: dict,
    numbered_txts: dict[str, dict],
) -> list[str]:
    """Multi-source 상황의 규칙 블록 (markdown lines). 단일 소스면 빈 리스트."""
    sources = slides_data.get("sources", []) or []
    is_multi_pdf = len(sources) > 1
    is_multi_txt = len(numbered_txts) > 1

    if not (is_multi_pdf or is_multi_txt):
        return []

    lines: list[str] = []
    lines.append("## Multi-source 규칙")
    lines.append("")
    if is_multi_pdf:
        lines.append(
            "- 입력 PDF 는 `source_pdf_order` 작은 순서대로 강의가 진행됩니다 "
            "(예: [0] → [1])."
        )
        lines.append(
            "- **monotonicity 는 global page index 기준**입니다. PDF 경계에서 "
            "라인 번호가 연속되지 않더라도 global page 순서는 유지."
        )
    if is_multi_txt:
        lines.append(
            "- 입력 녹취록도 입력 순서가 수업 시간 순서입니다 (예: 1교시 → 2교시)."
        )
        lines.append(
            "- 녹취록 라인 번호는 각 녹취록 파일 내에서만 유효합니다. "
            "`source` 필드로 구분해서 배정."
        )
    if is_multi_pdf and is_multi_txt:
        lines.append(
            "- 한 PDF 의 후반이 한 교시 안에서 끝나지 않고 다음 교시로 "
            "넘어갈 수 있습니다 (교시 경계와 PDF 경계는 독립)."
        )
    lines.append("")
    return lines


def build_grouped_slide_summary(slides_data: dict) -> list[str]:
    """전체 슬라이드 요약을 PDF 별로 그룹핑해 markdown lines 로.

    단일 PDF 면 기존 flat 포맷.
    """
    lines: list[str] = []
    lines.append("## 전체 슬라이드 요약")
    lines.append("")
    pages = slides_data.get("pages", []) or []
    sources = slides_data.get("sources", []) or []

    pages_by_order: dict[int, list[dict]] = {}
    for p in pages:
        order = p.get("source_pdf_order", 0)
        pages_by_order.setdefault(order, []).append(p)

    if len(sources) > 1:
        for src in sources:
            order = src.get("order", 0)
            pdf_name = src.get("pdf", "")
            lines.append(f"### 📄 [{order}] {pdf_name}")
            for p in pages_by_order.get(order, []):
                _append_page_summary(lines, p, show_local=True)
            lines.append("")
    else:
        for p in pages:
            _append_page_summary(lines, p, show_local=False)
        lines.append("")
    return lines


def build_source_whitelist_rule(
    numbered_txts: dict[str, dict],
) -> list[str]:
    """`source` 필드 화이트리스트 규칙 블록 (markdown lines).

    LLM이 `prompt.txt` / `task.txt` 같은 시스템 파일명으로 hallucinate 하지
    않도록 허용되는 정확한 파일명 리스트를 명시.
    """
    names = list(numbered_txts.keys())
    if not names:
        return []
    lines: list[str] = []
    lines.append("## `source` 필드 화이트리스트 (엄격)")
    lines.append("")
    lines.append(
        "`source` 필드는 **아래 목록 중 정확히 하나**여야 합니다. "
        "목록에 없는 값은 스키마 레벨에서 거부됩니다."
    )
    lines.append("")
    for name in names:
        lines.append(f'- `"{name}"`')
    lines.append("")
    lines.append(
        "**절대 금지** — `prompt.txt`, `task.txt`, `input.txt`, `source.txt`, "
        "`instructions.md` 같은 시스템/instruction 파일명을 `source` 로 쓰면 안 됨. "
        "오직 위에 나열된 녹취록 파일명만 사용."
    )
    lines.append("")
    return lines


def _append_page_summary(
    lines: list[str], page: dict, show_local: bool,
) -> None:
    idx = page.get("index")
    title = page.get("title", "")
    anchors = page.get("anchors", [])
    brief = page.get("brief", "")
    local_page = page.get("local_page", 0)
    if show_local and local_page:
        lines.append(f"- **Page {idx}** (local {local_page}): {title}")
    else:
        lines.append(f"- **Page {idx}**: {title}")
    if anchors:
        lines.append(f"  - anchors: {', '.join(anchors)}")
    if brief:
        lines.append(f"  - brief: {brief}")
