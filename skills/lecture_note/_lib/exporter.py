"""Step 7/8: note.json + note.html 산출.

- `build_note_json`: 전체 파이프라인 결과를 구조화된 dict로 (Python 재처리 용).
- `render_pdf_pages_base64`: pypdfium2로 PDF 각 페이지를 PNG base64 data URI로.
- `build_note_html`: 목차 + 페이지 이미지 + 렌더된 섹션을 self-contained HTML로.
- `_md_to_html`: compose 템플릿에 한정된 최소 markdown 렌더러 (외부 의존 없음).
"""

from __future__ import annotations

import base64
import html
import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable


def _emit(log_callback: Callable[[str], None] | None, msg: str) -> None:
    if log_callback is None:
        return
    try:
        log_callback(msg)
    except Exception:
        pass


_PREFIX_RE = re.compile(r"^\[\d+\]\s?")


def _strip_number_prefix(line: str) -> str:
    m = _PREFIX_RE.match(line)
    return line[m.end():] if m else line


def _format_range_label(ranges: list[dict]) -> str:
    """`40, 56-58` 형태 요약 라벨."""
    if not ranges:
        return ""
    by_src: dict[str, list[tuple[int, int]]] = {}
    for r in ranges:
        src = r.get("source", "")
        s = r.get("start_line")
        e = r.get("end_line")
        if not isinstance(s, int) or not isinstance(e, int):
            continue
        by_src.setdefault(src, []).append((s, e))
    parts: list[str] = []
    for src in sorted(by_src.keys()):
        intervals = sorted(by_src[src])
        rng = ", ".join(f"{s}" if s == e else f"{s}-{e}" for s, e in intervals)
        parts.append(rng if len(by_src) == 1 else f"{src}: {rng}")
    return "; ".join(parts)


def _read_range_text(
    path: Path,
    start: int,
    end: int,
    strip_prefix: bool = False,
) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    selected = lines[start - 1:end]
    if strip_prefix:
        selected = [_strip_number_prefix(l) for l in selected]
    return "\n".join(selected)


def build_note_json(
    slides_data: dict,
    mapping: dict[int, list[dict]],
    numbered_txts: dict[str, dict],
    glossary_md: bytes,
    unassigned: list[dict],
    page_notes: dict[int, str],
    run_id: str,
) -> dict:
    """전체 파이프라인 결과를 JSON 직렬화 가능한 dict로.

    Multi-PDF: 각 page 에 `source_pdf`/`source_pdf_order`/`local_page` 메타
    부착 + top-level `sources` 배열 포함.
    """
    pages_out: list[dict] = []
    for page in slides_data.get("pages", []):
        idx = page.get("index")
        ranges = mapping.get(idx, []) if isinstance(idx, int) else []

        # transcript 발췌 (각 range별로 numbered text + plain text)
        excerpts: list[dict] = []
        for r in ranges:
            src = r.get("source")
            s = r.get("start_line")
            e = r.get("end_line")
            if not (isinstance(src, str) and isinstance(s, int) and isinstance(e, int)):
                continue
            info = numbered_txts.get(src)
            if info is None:
                continue
            path = Path(info["path"])
            raw = _read_range_text(path, s, e, strip_prefix=False)
            plain = _read_range_text(path, s, e, strip_prefix=True)
            excerpts.append({
                "source": src,
                "start_line": s,
                "end_line": e,
                "text": raw,
                "text_plain": plain,
            })

        pages_out.append({
            "index": idx,
            "title": page.get("title", ""),
            "anchors": page.get("anchors", []),
            "brief": page.get("brief", ""),
            "source_pdf": page.get("source_pdf", ""),
            "source_pdf_order": page.get("source_pdf_order", 0),
            "local_page": page.get("local_page", 0),
            "ranges": ranges,
            "range_label": _format_range_label(ranges),
            "transcript_excerpts": excerpts,
            "section_markdown": page_notes.get(idx, "") if isinstance(idx, int) else "",
        })

    unassigned_out: list[dict] = []
    for u in unassigned:
        src = u.get("source")
        s = u.get("start_line")
        e = u.get("end_line")
        text_plain = ""
        if isinstance(src, str) and isinstance(s, int) and isinstance(e, int):
            info = numbered_txts.get(src)
            if info is not None:
                text_plain = _read_range_text(
                    Path(info["path"]), s, e, strip_prefix=True,
                )
        unassigned_out.append({
            "source": src,
            "start_line": s,
            "end_line": e,
            "type": u.get("type", "other"),
            "reason": u.get("reason", ""),
            "text": text_plain,
        })

    return {
        "schema_version": 2,
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "slides_count": len(pages_out),
        "sources": list(slides_data.get("sources", [])),
        "pages": pages_out,
        "glossary_markdown": (
            glossary_md.decode("utf-8", errors="replace") if glossary_md else ""
        ),
        "unassigned": unassigned_out,
    }


# ─────────────────────────── PDF 이미지 렌더 ───────────────────────────


def render_pdf_pages_base64(
    pdf_paths: list[Path],
    sources: list[dict] | None = None,
    scale: float = 1.5,
    log_callback: Callable[[str], None] | None = None,
) -> list[str]:
    """여러 PDF 를 순서대로 렌더해 flat base64 data URI 리스트 반환.

    반환 리스트 i 번째 원소는 글로벌 page (i+1) 에 대응.
    `sources` 는 참고용 (page_count 검증). 없어도 동작.

    pypdfium2 미설치 시 빈 리스트 반환 (HTML 에서 placeholder 처리).
    """
    try:
        import pypdfium2 as pdfium  # type: ignore
    except ImportError:
        _emit(
            log_callback,
            "[export] pypdfium2 미설치 — PDF 이미지는 placeholder 로 대체",
        )
        return []

    images: list[str] = []
    total_rendered = 0
    for pdf_order, pdf_path in enumerate(pdf_paths):
        try:
            pdf = pdfium.PdfDocument(str(pdf_path))
        except Exception as exc:
            _emit(log_callback, f"[export] PDF 열기 실패 ({pdf_path.name}): {exc}")
            # 이 PDF 의 예상 페이지 수만큼 빈 placeholder 채움 (가능하면)
            expected = 0
            if sources and pdf_order < len(sources):
                expected = int(sources[pdf_order].get("page_count", 0) or 0)
            images.extend([""] * expected)
            continue

        try:
            n = len(pdf)
            _emit(
                log_callback,
                f"[export] {pdf_path.name}: {n}페이지 렌더 시작",
            )
            for i in range(n):
                try:
                    page = pdf[i]
                    pil = page.render(scale=scale).to_pil()
                    buf = io.BytesIO()
                    pil.save(buf, format="PNG", optimize=True)
                    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                    images.append(f"data:image/png;base64,{b64}")
                except Exception as exc:
                    _emit(
                        log_callback,
                        f"[export] {pdf_path.name} page {i + 1} 렌더 실패: {exc}",
                    )
                    images.append("")
                total_rendered += 1
                if total_rendered % 10 == 0:
                    _emit(
                        log_callback,
                        f"[export] 총 {total_rendered}페이지 렌더 완료",
                    )
        finally:
            try:
                pdf.close()
            except Exception:
                pass
    return images


# ─────────────────────────── Markdown → HTML ───────────────────────────


_BOLD_RE = re.compile(r"\*\*([^*]+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+?)`")
_ITALIC_RE = re.compile(r"(?<![A-Za-z0-9_])_([^_\n]+?)_(?![A-Za-z0-9_])")


def _inline_md(text: str) -> str:
    """inline markdown(**bold**, `code`, _italic_) → HTML.

    순서: escape → code placeholder → bold → italic → restore.
    """
    escaped = html.escape(text, quote=False)

    placeholders: list[str] = []

    def _code_sub(m: re.Match) -> str:
        placeholders.append(f"<code>{m.group(1)}</code>")
        return f"\x00C{len(placeholders) - 1}\x00"

    escaped = _CODE_RE.sub(_code_sub, escaped)
    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _ITALIC_RE.sub(r"<em>\1</em>", escaped)

    for i, code in enumerate(placeholders):
        escaped = escaped.replace(f"\x00C{i}\x00", code)
    return escaped


def _md_to_html(md: str) -> str:
    """compose 템플릿에 한정된 minimal markdown 렌더러.

    지원: #/##/###/#### 헤더, > blockquote, - list, **bold**, `code`, _italic_,
    빈 줄 기반 paragraph.
    """
    if not md:
        return ""

    lines = md.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.rstrip()

        if not stripped.strip():
            i += 1
            continue

        if stripped.startswith("#### "):
            out.append(f"<h4>{_inline_md(stripped[5:])}</h4>")
            i += 1
            continue
        if stripped.startswith("### "):
            out.append(f"<h3>{_inline_md(stripped[4:])}</h3>")
            i += 1
            continue
        if stripped.startswith("## "):
            out.append(f"<h2>{_inline_md(stripped[3:])}</h2>")
            i += 1
            continue
        if stripped.startswith("# "):
            out.append(f"<h1>{_inline_md(stripped[2:])}</h1>")
            i += 1
            continue

        if stripped.startswith(">"):
            bq_paras: list[list[str]] = [[]]
            while i < n:
                cur = lines[i].rstrip()
                if cur.startswith(">"):
                    content = cur[1:].lstrip()
                    if content:
                        bq_paras[-1].append(content)
                    else:
                        if bq_paras[-1]:
                            bq_paras.append([])
                    i += 1
                elif not cur.strip():
                    break
                else:
                    break
            paras_html = "".join(
                f"<p>{_inline_md(' '.join(p))}</p>" for p in bq_paras if p
            )
            out.append(f"<blockquote>{paras_html}</blockquote>")
            continue

        if stripped.startswith("- "):
            items: list[str] = []
            while i < n and lines[i].lstrip().startswith("- "):
                items.append(_inline_md(lines[i].lstrip()[2:]))
                i += 1
            li_html = "".join(f"<li>{it}</li>" for it in items)
            out.append(f"<ul>{li_html}</ul>")
            continue

        para_lines: list[str] = []
        while i < n:
            cur = lines[i].rstrip()
            if not cur.strip():
                break
            if cur.lstrip().startswith(("#", ">", "- ")):
                break
            para_lines.append(cur)
            i += 1
        if para_lines:
            out.append(f"<p>{_inline_md(' '.join(para_lines))}</p>")

    return "\n".join(out)


# ─────────────────────────── HTML 빌더 ───────────────────────────


_DEFAULT_CSS = """
* { box-sizing: border-box; }
body {
    font-family: -apple-system, "Segoe UI", "Pretendard", "Malgun Gothic",
                 "Apple SD Gothic Neo", sans-serif;
    max-width: 960px;
    margin: 0 auto;
    padding: 2rem 1.5rem 4rem;
    line-height: 1.7;
    color: #1e1e1e;
    background: #f5f5f7;
}
h1 {
    font-size: 1.9rem;
    border-bottom: 2px solid #333;
    padding-bottom: 0.5rem;
    margin-top: 0;
}
h2 { font-size: 1.4rem; color: #1a4480; margin-top: 2.4rem; }
h3 { font-size: 1.15rem; color: #333; margin-top: 1.8rem; }
h4 { font-size: 1.02rem; color: #555; margin-top: 1.2rem; }
p  { margin: 0.6rem 0; }
a  { color: #1a4480; text-decoration: none; }
a:hover { text-decoration: underline; }
nav.toc {
    background: #ffffff;
    padding: 1.2rem 1.6rem;
    border-radius: 10px;
    margin: 1.5rem 0 2.5rem;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}
nav.toc h2 { margin-top: 0; color: #333; font-size: 1.1rem; border-bottom: 1px solid #eee; padding-bottom: 0.5rem; }
nav.toc ul { list-style: none; padding-left: 0; margin: 0.7rem 0 0; columns: 2; column-gap: 2rem; }
nav.toc li { margin: 0.25rem 0; break-inside: avoid; font-size: 0.92rem; }
section.slide {
    background: #ffffff;
    margin: 1.8rem 0;
    padding: 1.5rem 1.8rem;
    border-radius: 10px;
    box-shadow: 0 1px 6px rgba(0,0,0,0.07);
}
section.slide > .slide-image {
    text-align: center;
    margin: 0.2rem 0 1rem;
    background: #fafafa;
    border-radius: 6px;
    padding: 0.6rem;
}
section.slide > .slide-image img {
    max-width: 100%;
    max-height: 540px;
    border: 1px solid #ddd;
    border-radius: 4px;
}
section.slide > .slide-image .placeholder {
    color: #888;
    font-size: 0.9rem;
    padding: 2rem;
}
section.slide .debug-range {
    color: #888;
    font-size: 0.82rem;
    font-style: italic;
    margin: 0 0 1rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px dashed #e0e0e0;
}
blockquote {
    border-left: 4px solid #1a4480;
    padding: 0.7rem 1.2rem;
    margin: 1rem 0;
    background: #f4f7fc;
    color: #222;
    border-radius: 0 6px 6px 0;
}
blockquote p { margin: 0.35rem 0; }
code {
    background: #edeef2;
    padding: 0.08rem 0.35rem;
    border-radius: 3px;
    font-family: "Consolas", "Cascadia Code", "Menlo", monospace;
    font-size: 0.92em;
}
ul { padding-left: 1.3rem; }
li { margin: 0.2rem 0; }
.glossary {
    background: #fffbef;
    padding: 1.2rem 1.6rem;
    border-radius: 10px;
    border-left: 4px solid #e6a700;
    margin: 1.5rem 0 2rem;
}
.glossary h2 { margin-top: 0; }
.unassigned {
    margin-top: 3rem;
    padding: 1.2rem 1.6rem;
    background: #fef3f3;
    border-radius: 10px;
    border-left: 4px solid #c88;
}
.unassigned h2 { margin-top: 0; }
.unassigned h3 { color: #a55; }
.unassigned pre {
    background: #fff;
    padding: 0.8rem 1rem;
    border-radius: 4px;
    border: 1px solid #eedede;
    white-space: pre-wrap;
    word-wrap: break-word;
    font-size: 0.9rem;
    line-height: 1.6;
}
.meta {
    color: #777;
    font-size: 0.85rem;
    margin-bottom: 1.5rem;
}
.source-badge {
    display: inline-block;
    color: #555;
    background: #eef1f8;
    border-radius: 10px;
    padding: 0.1rem 0.5rem;
    font-size: 0.78rem;
    margin: 0.1rem 0 0.6rem;
}
nav.toc h3 {
    font-size: 0.95rem;
    color: #555;
    margin: 1rem 0 0.3rem;
    border-bottom: 1px solid #eee;
    padding-bottom: 0.2rem;
}
.pdf-group-header {
    margin: 2.5rem 0 1rem;
    padding: 0.8rem 1.2rem;
    background: #eef1f8;
    border-left: 4px solid #1a4480;
    border-radius: 6px;
    color: #1a4480;
    font-size: 1.15rem;
    font-weight: 600;
}
"""


def _slide_anchor(idx: int) -> str:
    return f"slide-{idx}"


def _strip_leading_header(md: str) -> str:
    """compose 섹션의 맨 앞 `### Slide N: ...` 헤더 제거 (HTML에서 중복 방지)."""
    lines = md.splitlines()
    skipped = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("### "):
            skipped = i + 1
            break
        if line.strip():
            return md
    # 헤더 이후 빈 줄도 skip
    while skipped < len(lines) and not lines[skipped].strip():
        skipped += 1
    return "\n".join(lines[skipped:])


def _render_slide_section(
    page: dict,
    pdf_images: list[str],
    show_source_badge: bool,
) -> list[str]:
    """한 개 slide 섹션 HTML 조각 (parts 리스트)."""
    idx = page.get("index")
    title = page.get("title", "")
    range_label = page.get("range_label", "")
    section_md = page.get("section_markdown", "")
    source_pdf = page.get("source_pdf", "")
    local_page = page.get("local_page", 0)

    chunk: list[str] = []
    chunk.append(f'<section class="slide" id="{_slide_anchor(idx)}">')

    # 이미지
    img_idx = idx - 1 if isinstance(idx, int) else -1
    chunk.append('<div class="slide-image">')
    if 0 <= img_idx < len(pdf_images) and pdf_images[img_idx]:
        chunk.append(f'<img src="{pdf_images[img_idx]}" alt="Slide {idx}">')
    else:
        chunk.append(
            '<div class="placeholder">'
            '(PDF 이미지 로드 실패 — pypdfium2 미설치 또는 렌더 오류)'
            '</div>'
        )
    chunk.append("</div>")

    # 헤더
    chunk.append(f"<h2>Slide {idx}: {html.escape(title)}</h2>")

    # source 배지 (multi-PDF 시에만)
    if show_source_badge and source_pdf:
        chunk.append(
            f'<p class="source-badge">from '
            f'{html.escape(source_pdf)} · local p.{local_page}</p>'
        )

    # debug 라인
    if range_label:
        chunk.append(
            f'<p class="debug-range">배정 라인: {html.escape(range_label)}</p>'
        )
    else:
        chunk.append(
            '<p class="debug-range">배정 라인: (없음 — 교수가 빠르게 넘김)</p>'
        )

    # 본문
    body_md = _strip_leading_header(section_md) if section_md else ""
    if body_md:
        chunk.append(_md_to_html(body_md))
    else:
        chunk.append('<p><em>(compose 실패 — 섹션 본문 없음)</em></p>')

    chunk.append("</section>")
    return chunk


def build_note_html(note_data: dict, pdf_images: list[str]) -> str:
    """note.json 구조 + 페이지 이미지 → self-contained HTML.

    Multi-PDF 시: TOC 와 본문을 PDF 별 그룹으로 구분.
    """
    pages = note_data.get("pages", [])
    unassigned = note_data.get("unassigned", [])
    glossary_md = note_data.get("glossary_markdown", "")
    run_id = note_data.get("run_id", "")
    generated_at = note_data.get("generated_at", "")
    sources = note_data.get("sources", []) or []
    is_multi_pdf = len(sources) > 1

    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="ko">')
    parts.append("<head>")
    parts.append('<meta charset="UTF-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append("<title>강의 노트</title>")
    parts.append(f"<style>{_DEFAULT_CSS}</style>")
    parts.append("</head>")
    parts.append("<body>")
    parts.append("<h1>강의 노트</h1>")
    meta_line = (
        f'<p class="meta">run_id: <code>{html.escape(run_id)}</code>'
        f' · generated: {html.escape(generated_at)}'
        f' · {len(pages)}페이지'
    )
    if is_multi_pdf:
        meta_line += f' · PDF {len(sources)}개'
    meta_line += "</p>"
    parts.append(meta_line)

    # pages 를 source_pdf_order 기준으로 그룹핑
    pages_by_order: dict[int, list[dict]] = {}
    for p in pages:
        order = p.get("source_pdf_order", 0)
        pages_by_order.setdefault(order, []).append(p)

    # 목차
    parts.append('<nav class="toc">')
    parts.append("<h2>목차</h2>")
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
    parts.append("</nav>")

    # 용어집
    if glossary_md.strip():
        parts.append('<section class="glossary">')
        parts.append("<h2>용어집</h2>")
        parts.append(_md_to_html(glossary_md))
        parts.append("</section>")

    # 페이지별 섹션
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
                parts.extend(_render_slide_section(p, pdf_images, True))
    else:
        for p in pages:
            parts.extend(_render_slide_section(p, pdf_images, False))

    # Unassigned 섹션
    if unassigned:
        chatter = [u for u in unassigned if u.get("type") == "chatter"]
        other = [u for u in unassigned if u.get("type") == "other"]

        parts.append('<section class="unassigned">')
        parts.append("<h2>할당되지 않은 녹취 구간</h2>")

        def _render_bucket(title: str, items: list[dict]) -> None:
            parts.append(f"<h3>{title}</h3>")
            for u in items:
                src = u.get("source", "")
                s = u.get("start_line")
                e = u.get("end_line")
                reason = u.get("reason", "")
                text = u.get("text", "")
                header = f"{html.escape(src)} lines {s}-{e}"
                if reason:
                    header += f' — <em>{html.escape(reason)}</em>'
                parts.append(f"<h4>{header}</h4>")
                if text:
                    parts.append(f"<pre>{html.escape(text)}</pre>")

        if chatter:
            _render_bucket("수업과 무관한 잡담 / 행정", chatter)
        if other:
            _render_bucket("기타 / 미분류", other)

        parts.append("</section>")

    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)
