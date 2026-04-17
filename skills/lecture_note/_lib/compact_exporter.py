"""Step 11: compact.html 빌더.

기존 exporter._md_to_html / _inline_md / CSS를 최대한 재활용.
추가 요소:
- 카테고리별 glossary
- per-slide narrative + footnotes ([^N] → <sup> 링크)
- 맨 뒤 핵심 요약노트 (교수 시험 코멘트 + 압축 prose + 표)
"""

from __future__ import annotations

import html
import re
from datetime import datetime

from _lib.exporter import _DEFAULT_CSS, _inline_md, _md_to_html


# ─────────────────────────── 추가 CSS ───────────────────────────

_COMPACT_EXTRA_CSS = """
/* compact variant 전용 */
h1.compact-title { border-bottom-color: #8b3a62; }
.compact-badge {
    display: inline-block;
    background: #8b3a62;
    color: white;
    font-size: 0.7rem;
    padding: 0.15rem 0.55rem;
    border-radius: 10px;
    margin-left: 0.6rem;
    vertical-align: middle;
    letter-spacing: 0.05em;
}
.compact-glossary {
    background: #fffbef;
    padding: 1.2rem 1.6rem;
    border-radius: 10px;
    border-left: 4px solid #e6a700;
    margin: 1.5rem 0 2rem;
}
.compact-glossary h2 { margin-top: 0; }
.glossary-category { margin: 1.2rem 0 1.6rem; }
.glossary-category h3 {
    color: #8a6300;
    margin-bottom: 0.2rem;
    border-bottom: 1px dashed #e6a700;
    padding-bottom: 0.2rem;
}
.glossary-category .cat-desc {
    font-size: 0.9rem;
    color: #776;
    margin: 0.2rem 0 0.6rem;
    font-style: italic;
}
.glossary-category ul { padding-left: 1.2rem; }
.glossary-category li { margin: 0.3rem 0; }
.slide-refs {
    color: #8b3a62;
    font-size: 0.82rem;
    margin-left: 0.4rem;
}
section.slide.compact {
    border-left: 4px solid #8b3a62;
}
section.slide.compact h2 { color: #8b3a62; }
section.slide.compact .narrative { margin-top: 1rem; }
section.slide.compact .fn-block {
    margin-top: 1.4rem;
    padding: 0.8rem 1.2rem;
    background: #faf5f7;
    border-radius: 6px;
    border-left: 3px solid #b66690;
    font-size: 0.92rem;
}
section.slide.compact .fn-block h4 {
    margin: 0 0 0.4rem;
    color: #8b3a62;
    font-size: 0.95rem;
}
section.slide.compact .fn-block ol {
    margin: 0;
    padding-left: 1.2rem;
}
section.slide.compact .fn-block li { margin: 0.3rem 0; }
sup.fn-ref a {
    color: #8b3a62;
    font-weight: 600;
    text-decoration: none;
}
sup.fn-ref a:hover { text-decoration: underline; }
.exam-summary {
    margin-top: 3rem;
    padding: 1.4rem 1.8rem;
    background: #f4f1fb;
    border-radius: 10px;
    border-left: 4px solid #6a4e9a;
}
.exam-summary h2 { margin-top: 0; color: #6a4e9a; }
.exam-summary h3 { color: #6a4e9a; margin-top: 1.6rem; }
.exam-summary .prof-exam-notes {
    background: #fff;
    padding: 1rem 1.3rem;
    border-radius: 6px;
    border-left: 3px solid #d17733;
    margin: 0.8rem 0;
}
.exam-summary .prof-exam-notes h3 { color: #d17733; margin-top: 0; }
.exam-summary .compressed-prose p { margin: 0.6rem 0; }
table.summary-table {
    width: 100%;
    border-collapse: collapse;
    margin: 0.8rem 0;
    font-size: 0.92rem;
    background: #fff;
    border-radius: 6px;
    overflow: hidden;
}
table.summary-table th {
    background: #6a4e9a;
    color: white;
    padding: 0.55rem 0.8rem;
    text-align: left;
    font-weight: 600;
}
table.summary-table td {
    padding: 0.5rem 0.8rem;
    border-top: 1px solid #e8e3f1;
    vertical-align: top;
}
table.summary-table tr:nth-child(even) td { background: #faf9fc; }
"""


# ─────────────────────────── Helpers ───────────────────────────


def _slide_anchor(idx: int) -> str:
    return f"compact-slide-{idx}"


_FN_MARKER_RE = re.compile(r"\[\^(\d+)\]")


def _render_narrative_with_footnote_links(narrative_md: str, page_idx: int) -> str:
    """narrative_md 를 HTML로 렌더하고, [^N] 마커를 superscript 링크로 치환."""
    body_html = _md_to_html(narrative_md)

    def _sub(m: re.Match) -> str:
        n = m.group(1)
        return (
            f'<sup class="fn-ref">'
            f'<a href="#fn-p{page_idx}-{n}">[{n}]</a></sup>'
        )

    return _FN_MARKER_RE.sub(_sub, body_html)


def _render_footnotes_block(
    footnotes: list[dict],
    page_idx: int,
) -> str:
    """각주 목록을 <ol>로 렌더. 비어있으면 빈 문자열."""
    valid: list[tuple[str, str]] = []
    for fn in footnotes:
        if not isinstance(fn, dict):
            continue
        marker = str(fn.get("marker", "")).lstrip("^").strip()
        text = str(fn.get("text", "")).strip()
        if not marker or not text:
            continue
        valid.append((marker, text))
    if not valid:
        return ""

    parts: list[str] = []
    parts.append('<div class="fn-block">')
    parts.append("<h4>각주</h4>")
    parts.append("<ol>")
    for marker, text in valid:
        parts.append(
            f'<li id="fn-p{page_idx}-{marker}" value="{html.escape(marker)}">'
            f'{_inline_md(text)}</li>'
        )
    parts.append("</ol>")
    parts.append("</div>")
    return "\n".join(parts)


_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _render_markdown_table(md: str) -> str:
    """pipe 문법 markdown table → HTML <table>.

    실패 시 _md_to_html 결과를 반환 (fallback).
    """
    lines = [l for l in md.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return _md_to_html(md)
    header_line = lines[0].strip()
    sep_line = lines[1].strip()
    if not (header_line.count("|") >= 1 and _TABLE_SEP_RE.match(sep_line)):
        return _md_to_html(md)

    def _split_row(row: str) -> list[str]:
        row = row.strip()
        # leading/trailing pipe 제거
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]
        return [c.strip() for c in row.split("|")]

    header_cells = _split_row(header_line)
    data_rows = [_split_row(l) for l in lines[2:]]

    parts: list[str] = []
    parts.append('<table class="summary-table">')
    parts.append("<thead><tr>")
    for h in header_cells:
        parts.append(f"<th>{_inline_md(h)}</th>")
    parts.append("</tr></thead>")
    parts.append("<tbody>")
    for row in data_rows:
        parts.append("<tr>")
        # 컬럼 수가 헤더보다 적으면 빈 셀로 채움
        for i in range(len(header_cells)):
            cell = row[i] if i < len(row) else ""
            parts.append(f"<td>{_inline_md(cell)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


# ─────────────────────────── Sections ───────────────────────────


def _render_glossary_section(compact_glossary: dict) -> list[str]:
    categories = compact_glossary.get("categories", []) or []
    if not categories:
        return []

    parts: list[str] = []
    parts.append('<section class="compact-glossary">')
    parts.append("<h2>용어집 (재구성)</h2>")
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        name = cat.get("name", "")
        desc = cat.get("description", "")
        terms = cat.get("terms", []) or []
        parts.append('<div class="glossary-category">')
        parts.append(f"<h3>{html.escape(name)}</h3>")
        if desc:
            parts.append(f'<p class="cat-desc">{_inline_md(desc)}</p>')
        if terms:
            parts.append("<ul>")
            for t in terms:
                if not isinstance(t, dict):
                    continue
                term = t.get("term", "")
                definition = t.get("definition", "")
                slides = t.get("mentioned_on_slides", []) or []
                slide_refs = ""
                if slides:
                    links = []
                    for s in slides:
                        if isinstance(s, int):
                            links.append(
                                f'<a href="#{_slide_anchor(s)}">Slide {s}</a>'
                            )
                    if links:
                        slide_refs = (
                            f' <span class="slide-refs">— {", ".join(links)}</span>'
                        )
                parts.append(
                    f"<li><strong>{html.escape(term)}</strong>"
                    f"{' — ' + _inline_md(definition) if definition else ''}"
                    f"{slide_refs}</li>"
                )
            parts.append("</ul>")
        parts.append("</div>")
    parts.append("</section>")
    return parts


def _render_compact_slide_section(
    page: dict,
    compact_page: dict,
    pdf_images: list[str],
    show_source_badge: bool,
) -> list[str]:
    idx = page.get("index")
    title = page.get("title", "")
    source_pdf = page.get("source_pdf", "")
    local_page = page.get("local_page", 0)

    narrative_md = compact_page.get("narrative_markdown", "") if compact_page else ""
    footnotes = compact_page.get("footnotes", []) if compact_page else []

    parts: list[str] = []
    parts.append(f'<section class="slide compact" id="{_slide_anchor(idx)}">')

    # 이미지
    img_idx = idx - 1 if isinstance(idx, int) else -1
    parts.append('<div class="slide-image">')
    if 0 <= img_idx < len(pdf_images) and pdf_images[img_idx]:
        parts.append(f'<img src="{pdf_images[img_idx]}" alt="Slide {idx}">')
    else:
        parts.append(
            '<div class="placeholder">(PDF 이미지 로드 실패)</div>'
        )
    parts.append("</div>")

    # 헤더
    parts.append(f"<h2>Slide {idx}: {html.escape(title)}</h2>")
    if show_source_badge and source_pdf:
        parts.append(
            f'<p class="source-badge">from '
            f'{html.escape(source_pdf)} · local p.{local_page}</p>'
        )

    # narrative
    if narrative_md.strip():
        narrative_html = _render_narrative_with_footnote_links(
            narrative_md, idx if isinstance(idx, int) else 0,
        )
        parts.append(f'<div class="narrative">{narrative_html}</div>')
    else:
        parts.append('<p><em>(compact 재작성 실패)</em></p>')

    # footnotes
    fn_html = _render_footnotes_block(
        footnotes, idx if isinstance(idx, int) else 0,
    )
    if fn_html:
        parts.append(fn_html)

    parts.append("</section>")
    return parts


def _render_exam_summary_section(compact_summary: dict) -> list[str]:
    prof_md = compact_summary.get("professor_exam_notes_md", "") or ""
    prose_md = compact_summary.get("compressed_prose_md", "") or ""
    table_md = compact_summary.get("summary_table_md", "") or ""

    if not (prof_md.strip() or prose_md.strip() or table_md.strip()):
        return []

    parts: list[str] = []
    parts.append('<section class="exam-summary">')
    parts.append("<h2>핵심 요약노트</h2>")

    if prof_md.strip():
        parts.append('<div class="prof-exam-notes">')
        parts.append("<h3>교수자 시험 관련 코멘트</h3>")
        parts.append(_md_to_html(prof_md))
        parts.append("</div>")

    if prose_md.strip():
        parts.append('<div class="compressed-prose">')
        parts.append("<h3>시험 직전 요약</h3>")
        parts.append(_md_to_html(prose_md))
        parts.append("</div>")

    if table_md.strip():
        parts.append('<div class="summary-table-wrapper">')
        parts.append("<h3>핵심 포인트</h3>")
        parts.append(_render_markdown_table(table_md))
        parts.append("</div>")

    parts.append("</section>")
    return parts


# ─────────────────────────── Main builder ───────────────────────────


def build_compact_html(
    slides_data: dict,
    compact_pages: dict[int, dict],
    compact_glossary: dict,
    compact_summary: dict,
    pdf_images: list[str],
    run_id: str,
) -> str:
    """compact_lecture_note HTML 빌드. self-contained (외부 asset 없음)."""
    pages = slides_data.get("pages", []) or []
    sources = slides_data.get("sources", []) or []
    is_multi_pdf = len(sources) > 1

    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="ko">')
    parts.append("<head>")
    parts.append('<meta charset="UTF-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append("<title>강의 노트 (Compact)</title>")
    parts.append(f"<style>{_DEFAULT_CSS}{_COMPACT_EXTRA_CSS}</style>")
    parts.append("</head>")
    parts.append("<body>")
    parts.append(
        '<h1 class="compact-title">강의 노트'
        '<span class="compact-badge">COMPACT</span></h1>'
    )
    meta_line = (
        f'<p class="meta">run_id: <code>{html.escape(run_id)}</code>'
        f' · generated: {html.escape(datetime.now().isoformat(timespec="seconds"))}'
        f' · {len(pages)}페이지'
    )
    if is_multi_pdf:
        meta_line += f' · PDF {len(sources)}개'
    meta_line += "</p>"
    parts.append(meta_line)

    # 페이지 그룹핑
    pages_by_order: dict[int, list[dict]] = {}
    for p in pages:
        pages_by_order.setdefault(p.get("source_pdf_order", 0), []).append(p)

    # 목차
    parts.append('<nav class="toc">')
    parts.append("<h2>목차</h2>")
    parts.append("<ul>")
    parts.append('<li><a href="#compact-glossary">용어집 (재구성)</a></li>')
    parts.append("</ul>")
    if is_multi_pdf:
        for src in sources:
            order = src.get("order", 0)
            pdf_name = src.get("pdf", "")
            group_pages = pages_by_order.get(order, [])
            parts.append(f"<h3>📄 {html.escape(pdf_name)}</h3>")
            parts.append("<ul>")
            for p in group_pages:
                idx = p.get("index")
                title = p.get("title", "")
                if isinstance(idx, int):
                    parts.append(
                        f'<li><a href="#{_slide_anchor(idx)}">'
                        f'Slide {idx}: {html.escape(title)}</a></li>'
                    )
            parts.append("</ul>")
    else:
        parts.append("<ul>")
        for p in pages:
            idx = p.get("index")
            title = p.get("title", "")
            if isinstance(idx, int):
                parts.append(
                    f'<li><a href="#{_slide_anchor(idx)}">'
                    f'Slide {idx}: {html.escape(title)}</a></li>'
                )
        parts.append("</ul>")
    parts.append("<ul>")
    parts.append('<li><a href="#exam-summary">핵심 요약노트</a></li>')
    parts.append("</ul>")
    parts.append("</nav>")

    # Glossary
    gloss_parts = _render_glossary_section(compact_glossary)
    if gloss_parts:
        # id 부착을 위해 첫 section 태그에 삽입
        gloss_parts[0] = gloss_parts[0].replace(
            '<section class="compact-glossary">',
            '<section class="compact-glossary" id="compact-glossary">',
            1,
        )
        parts.extend(gloss_parts)

    # Per-slide sections
    if is_multi_pdf:
        for src in sources:
            order = src.get("order", 0)
            pdf_name = src.get("pdf", "")
            g_start = src.get("global_start", 0)
            g_end = src.get("global_end", 0)
            parts.append(
                f'<div class="pdf-group-header">📄 {html.escape(pdf_name)} '
                f'· global pages {g_start}~{g_end}</div>'
            )
            for p in pages_by_order.get(order, []):
                idx = p.get("index") if isinstance(p.get("index"), int) else -1
                cp = compact_pages.get(idx, {})
                parts.extend(
                    _render_compact_slide_section(p, cp, pdf_images, True)
                )
    else:
        for p in pages:
            idx = p.get("index") if isinstance(p.get("index"), int) else -1
            cp = compact_pages.get(idx, {})
            parts.extend(
                _render_compact_slide_section(p, cp, pdf_images, False)
            )

    # Exam summary
    summary_parts = _render_exam_summary_section(compact_summary)
    if summary_parts:
        summary_parts[0] = summary_parts[0].replace(
            '<section class="exam-summary">',
            '<section class="exam-summary" id="exam-summary">',
            1,
        )
        parts.extend(summary_parts)

    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)
