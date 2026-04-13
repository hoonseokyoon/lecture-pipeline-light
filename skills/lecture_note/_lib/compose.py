"""Step 5: 페이지별 markdown 섹션 생성 (ThreadPoolExecutor 병렬)
Step 6: 전체 merge."""

import json
import re
from concurrent.futures import as_completed
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, ContextThreadPoolExecutor, run_codex_task

from _lib.schemas import COMPOSE_SCHEMA


def _emit(log_callback: Callable[[str], None] | None, msg: str) -> None:
    if log_callback is None:
        return
    try:
        log_callback(msg)
    except Exception:
        pass


def _extract_transcript_excerpt(
    numbered_txts: dict[str, dict],
    ranges: list[dict],
) -> str:
    """페이지에 배정된 line range들의 실제 녹취 텍스트를 추출해 합침."""
    chunks: list[str] = []
    for r in ranges:
        src = r.get("source")
        s = r.get("start_line")
        e = r.get("end_line")
        if not isinstance(src, str) or not isinstance(s, int) or not isinstance(e, int):
            continue
        info = numbered_txts.get(src)
        if info is None:
            continue
        path = Path(info["path"])
        all_lines = path.read_text(encoding="utf-8").splitlines()
        # 1-based line numbers
        excerpt_lines = all_lines[s - 1:e]
        chunks.append(f"[{src} lines {s}-{e}]")
        chunks.extend(excerpt_lines)
        chunks.append("")
    return "\n".join(chunks).strip()


def _build_compose_prompt(
    page: dict,
    transcript_excerpt: str,
    glossary_md: bytes,
    slides_data: dict,
    lecture_summary: dict,
    level: str,
) -> str:
    idx = page.get("index")
    title = page.get("title", "")
    anchors = page.get("anchors", [])
    brief = page.get("brief", "")
    source_pdf = page.get("source_pdf", "")
    local_page = page.get("local_page", 0)
    lines: list[str] = []
    lines.append(
        "당신은 강의 노트 한 페이지 섹션을 작성합니다. "
        "전체 강의 컨텍스트 + 이 슬라이드의 정보 + 배정된 녹취 발췌 + 용어집을 "
        "바탕으로 Markdown 섹션을 만드세요."
    )
    lines.append("")

    # ── 전역 컨텍스트 ──
    lines.append("## 강의 전체 컨텍스트")
    lines.append("")
    theme = lecture_summary.get("overall_theme", "")
    if theme:
        lines.append(f"**핵심 주제**: {theme}")
        lines.append("")
    mechs = lecture_summary.get("key_mechanisms", [])
    if mechs:
        lines.append("**핵심 메커니즘**:")
        for m in mechs:
            lines.append(f"- {m}")
        lines.append("")

    # 입력 PDF 구성 (multi-PDF 시)
    sources = slides_data.get("sources", []) or []
    if len(sources) > 1:
        lines.append("**입력 PDF 구성** (입력 순서대로 강의 진행):")
        for src in sources:
            lines.append(
                f"- [{src.get('order', 0)}] {src.get('pdf', '')} — "
                f"global pages {src.get('global_start', 0)}~{src.get('global_end', 0)} "
                f"({src.get('page_count', 0)} 페이지)"
            )
        lines.append("")

    lines.append("**전체 슬라이드 개요** (현재 페이지는 ⭐ 표시):")
    pages_by_order: dict[int, list[dict]] = {}
    for p in slides_data.get("pages", []):
        order = p.get("source_pdf_order", 0)
        pages_by_order.setdefault(order, []).append(p)
    if len(sources) > 1:
        for src in sources:
            order = src.get("order", 0)
            pdf_name = src.get("pdf", "")
            lines.append(f"- 📄 **[{order}] {pdf_name}**")
            for p in pages_by_order.get(order, []):
                p_idx = p.get("index")
                p_title = p.get("title", "")
                p_local = p.get("local_page", 0)
                marker = " ⭐" if p_idx == idx else ""
                lines.append(
                    f"  - Page {p_idx} (local {p_local}): {p_title}{marker}"
                )
    else:
        for p in slides_data.get("pages", []):
            p_idx = p.get("index")
            p_title = p.get("title", "")
            marker = " ⭐" if p_idx == idx else ""
            lines.append(f"- Page {p_idx}: {p_title}{marker}")
    lines.append("")

    # ── 이 슬라이드 ──
    lines.append("## 이 슬라이드")
    lines.append("")
    lines.append(f"- **index** (global): {idx}")
    lines.append(f"- **title**: {title}")
    if source_pdf:
        lines.append(f"- **source_pdf**: {source_pdf} (local page {local_page})")
    if anchors:
        lines.append(f"- **anchors**: {', '.join(anchors)}")
    if brief:
        lines.append(f"- **brief**: {brief}")
    lines.append(f"- **importance**: `{level}`")
    lines.append("")

    lines.append("## 이 슬라이드에 배정된 녹취 발췌")
    lines.append("")
    if transcript_excerpt:
        lines.append("```")
        lines.append(transcript_excerpt)
        lines.append("```")
    else:
        lines.append("(이 슬라이드에 배정된 녹취 없음)")
    lines.append("")

    # ── 용어집 전체 ──
    lines.append("## 용어집 전체 (참고)")
    lines.append("")
    if glossary_md:
        try:
            gtext = glossary_md.decode("utf-8", errors="replace")
        except Exception:
            gtext = ""
        if gtext.strip():
            lines.append("```")
            lines.append(gtext.rstrip())
            lines.append("```")
        else:
            lines.append("(용어집 비어있음)")
    else:
        lines.append("(용어집 없음)")
    lines.append("")

    # ── 출력 작업 ──
    lines.append("## 작업")
    lines.append("")
    lines.append(
        "이 슬라이드에 대해 다음 **두 부분**을 생성해 `outputs/section.json` 의 "
        "`key_terms` 와 `summary_markdown` 필드에 담아 저장."
    )
    lines.append("")
    lines.append(
        "**참고**: 강의 발화 blockquote, slide 헤더, 핵심 용어 리스트의 markdown "
        "구조는 **Python 이 자동으로 조립**합니다. 당신은 두 필드만 만들면 됨."
    )
    lines.append("")
    lines.append("### 1. `key_terms` (배열)")
    lines.append("")
    lines.append(
        "각 항목 = `{\"term\": \"...\", \"definition\": \"...\"}` 형식의 dict."
    )
    lines.append("")
    lines.append(
        "- 슬라이드 anchors 에 있는 용어 + 녹취 발췌에 등장한 핵심 학술 용어"
    )
    lines.append("- 용어는 한국어 또는 영어 학술 용어 그대로 (e.g., `Reverse transcriptase`)")
    lines.append("- 정의는 한 줄. 슬라이드/녹취 맥락과 부합하게.")
    lines.append("- 보통 2~5개 정도. 너무 많지 않게.")
    lines.append("")
    lines.append("### 2. `summary_markdown` (문자열)")
    lines.append("")
    lines.append(
        "이 슬라이드의 **정리** 본문. 마크다운 inline formatting (**bold**, "
        "`code`, _italic_) 허용. 단 **`###`/`##`/`#` 헤더는 사용 금지** "
        "(상위 구조는 Python 이 박음)."
    )
    lines.append("")
    lines.append(
        "내용: 이 슬라이드가 강의 흐름에서 어떤 역할인지, 핵심 메시지가 무엇인지, "
        "다른 슬라이드/메커니즘과 어떻게 연결되는지. 녹취·슬라이드에 근거 있는 내용만."
    )
    lines.append("")

    # ── 중요도별 차등 지침 (summary_markdown 길이만) ──
    lines.append("## 중요도별 길이 지침 (`summary_markdown` 에만 적용)")
    lines.append("")
    if level == "important":
        lines.append(
            "이 페이지는 **`important`** — 강의 전체에서 핵심이거나 개념적으로 까다로움."
        )
        lines.append("")
        lines.append(
            "- `summary_markdown` 을 **깊이 있게 확장**. 5~10문장 이상 적극 허용."
        )
        lines.append(
            "- 메커니즘의 배경, 작동 원리, 강의 흐름 내 의의를 풀어서 설명."
        )
        lines.append(
            "- 의대생/학부생이 처음 접해도 이해할 수 있도록 **쉽고 길게**. "
            "비유, 단계별 설명, 왜 중요한지도 포함."
        )
        lines.append(
            "- 단, 사실 관계를 창작하지 말 것. 녹취·슬라이드에 근거 있는 내용만."
        )
    elif level == "transitional":
        lines.append(
            "이 페이지는 **`transitional`** — 표지/질문/도입/전환 경유지."
        )
        lines.append("")
        lines.append("- `summary_markdown` 은 **1~2문장**으로 간결히.")
        lines.append(
            "- 이 슬라이드가 강의 흐름에서 어떤 역할의 경유지인지, 다음에 무엇을 "
            "예고하는지만."
        )
    else:
        lines.append("이 페이지는 **`normal`** — 일반 페이지.")
        lines.append("")
        lines.append("- `summary_markdown` 은 **2~5문장**으로 핵심만.")
        lines.append("- 슬라이드가 전달하려는 포인트를 맥락 속에서 요약.")
    lines.append("")

    lines.append("언어는 녹취/슬라이드 언어를 따름 (한국어 권장).")
    return "\n".join(lines) + "\n"


_NUMBER_PREFIX_RE = re.compile(r"^\[\d+\]\s?")


def _format_blockquote(transcript_excerpt: str) -> list[str]:
    """transcript 발췌를 markdown blockquote 라인 리스트로.

    각 라인 prefix `> `, 빈 줄은 `>` 만. 줄 번호 `[NNNN] ` prefix 는 제거 (가독성).
    source 헤더 `[file lines a-b]` 는 이탤릭으로 박음 (multi-source 구분용).
    빈 발췌면 placeholder.
    """
    if not transcript_excerpt.strip():
        return ["> _(녹취 없음 — 교수가 이 슬라이드는 빠르게 넘긴 것으로 보임)_"]
    out: list[str] = []
    for raw_line in transcript_excerpt.splitlines():
        line = raw_line.rstrip()
        if not line:
            out.append(">")
            continue
        # source 헤더 마커 ([file lines a-b]) — 메타 표시
        if line.startswith("[") and line.endswith("]") and "lines" in line:
            out.append(f"> _{line}_")
            continue
        # numbered prefix `[0042] ` 제거
        stripped = _NUMBER_PREFIX_RE.sub("", line)
        if stripped:
            out.append(f"> {stripped}")
        else:
            out.append(">")
    return out


def _assemble_section(
    page: dict,
    transcript_excerpt: str,
    key_terms: list[dict],
    summary_markdown: str,
) -> str:
    """compose LLM 의 부분 결과 + Python 의 deterministic 조립으로 4 섹션 markdown.

    구조:
        ### Slide N: Title

        **강의 발화**:

        > (전사 발췌, blockquote)

        **핵심 용어**:
        - **term**: definition
        ...

        **정리**: summary
    """
    idx = page.get("index")
    title = page.get("title", "")

    out: list[str] = []
    out.append(f"### Slide {idx}: {title}")
    out.append("")
    out.append("**강의 발화**:")
    out.append("")
    out.extend(_format_blockquote(transcript_excerpt))
    out.append("")
    out.append("**핵심 용어**:")
    if key_terms:
        for t in key_terms:
            if not isinstance(t, dict):
                continue
            term = t.get("term", "").strip()
            definition = t.get("definition", "").strip()
            if not term:
                continue
            if definition:
                out.append(f"- **{term}**: {definition}")
            else:
                out.append(f"- **{term}**")
    else:
        out.append("- _(추출된 용어 없음)_")
    out.append("")
    summary = (summary_markdown or "").strip()
    if summary:
        out.append(f"**정리**: {summary}")
    else:
        out.append("**정리**: _(작성 실패 — 슬라이드 단독 해설 누락)_")
    return "\n".join(out) + "\n"


def compose_one_page(
    page: dict,
    mapping: dict[int, list[dict]],
    numbered_txts: dict[str, dict],
    glossary_md: bytes,
    slides_data: dict,
    lecture_summary: dict,
    level: str,
    model: str,
    reasoning_effort: str,
    service_tier: str = "default",
    timeout: int = 600,
) -> str:
    """한 페이지에 대해 Codex 호출로 부분 결과를 받고 Python 조립.

    LLM 은 `key_terms` 와 `summary_markdown` 만 생성. 강의 발화 blockquote 와
    슬라이드 헤더는 Python 이 deterministic 하게 박음 — drift 불가능.
    """
    idx = page.get("index")
    ranges = mapping.get(idx, [])
    transcript_excerpt = _extract_transcript_excerpt(numbered_txts, ranges)
    prompt = _build_compose_prompt(
        page=page,
        transcript_excerpt=transcript_excerpt,
        glossary_md=glossary_md,
        slides_data=slides_data,
        lecture_summary=lecture_summary,
        level=level,
    )

    result = run_codex_task(
        prompt=prompt,
        inputs={"_marker.txt": f"page {idx}"},
        expected_outputs=["section.json"],
        output_schema=COMPOSE_SCHEMA,
        model=model,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
        timeout=timeout,
    )
    raw = result.get("section.json", b"").decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexRunError(
            f"[compose] page {idx} section.json 파싱 실패: {exc}\n내용: {raw[:300]}"
        ) from exc

    key_terms = data.get("key_terms", [])
    summary_markdown = data.get("summary_markdown", "")
    if not isinstance(key_terms, list):
        key_terms = []
    if not isinstance(summary_markdown, str):
        summary_markdown = ""

    return _assemble_section(
        page=page,
        transcript_excerpt=transcript_excerpt,
        key_terms=key_terms,
        summary_markdown=summary_markdown,
    )


def compose_pages_parallel(
    slides_data: dict,
    mapping: dict[int, list[dict]],
    numbered_txts: dict[str, dict],
    glossary_md: bytes,
    lecture_summary: dict,
    pages_dir: Path,
    model: str,
    reasoning_effort: str,
    service_tier: str,
    timeout: int,
    max_workers: int,
    log_callback: Callable[[str], None] | None,
) -> dict[int, str]:
    """모든 페이지를 병렬로 compose. 이미 존재하는 페이지 파일은 재사용.

    각 페이지의 결과는 `pages_dir/page_NN.md`로 저장 + 반환 dict에 담김.
    실패한 페이지는 dict에서 제외 (경고 로그 남김).
    """
    pages_dir.mkdir(parents=True, exist_ok=True)
    results: dict[int, str] = {}
    pages = slides_data.get("pages", [])
    page_by_idx = {p.get("index"): p for p in pages if isinstance(p.get("index"), int)}
    total = len(page_by_idx)

    # importance level 맵
    importance_map: dict[int, str] = {}
    for item in lecture_summary.get("page_importance", []):
        if not isinstance(item, dict):
            continue
        p_idx = item.get("page")
        lvl = item.get("level")
        if isinstance(p_idx, int) and lvl in ("important", "normal", "transitional"):
            importance_map[p_idx] = lvl

    # 캐시된 페이지 우선 로드
    pending: list[int] = []
    for idx in sorted(page_by_idx.keys()):
        ckpt = pages_dir / f"page_{idx:03d}.md"
        if ckpt.exists():
            results[idx] = ckpt.read_text(encoding="utf-8")
        else:
            pending.append(idx)
    imp_count = sum(1 for i in pending if importance_map.get(i) == "important")
    _emit(
        log_callback,
        f"[compose] 총 {total}페이지, 캐시 {total - len(pending)}, "
        f"실행 {len(pending)} (중 important={imp_count})",
    )

    if not pending:
        return results

    def worker(idx: int) -> tuple[int, str | None, str | None]:
        try:
            md = compose_one_page(
                page=page_by_idx[idx],
                mapping=mapping,
                numbered_txts=numbered_txts,
                glossary_md=glossary_md,
                slides_data=slides_data,
                lecture_summary=lecture_summary,
                level=importance_map.get(idx, "normal"),
                model=model,
                reasoning_effort=reasoning_effort,
                service_tier=service_tier,
                timeout=timeout,
            )
            return (idx, md, None)
        except Exception as exc:
            return (idx, None, str(exc))

    with ContextThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(worker, idx): idx for idx in pending}
        done_count = 0
        for fut in as_completed(futures):
            idx, md, err = fut.result()
            done_count += 1
            if md is not None:
                results[idx] = md
                (pages_dir / f"page_{idx:03d}.md").write_text(md, encoding="utf-8")
                _emit(
                    log_callback,
                    f"[compose] {done_count}/{len(pending)} — page {idx} 완료",
                )
            else:
                _emit(
                    log_callback,
                    f"[compose] {done_count}/{len(pending)} — page {idx} 실패: {err}",
                )

    return results


def _format_range_str(ranges: list[dict]) -> str:
    """mapping의 range list를 `40, 56-58` 형태 debug 문자열로."""
    if not ranges:
        return "(없음)"
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
        rng_parts = [f"{s}" if s == e else f"{s}-{e}" for s, e in intervals]
        joined = ", ".join(rng_parts)
        if len(by_src) == 1:
            parts.append(joined)
        else:
            parts.append(f"{src}: {joined}")
    return "; ".join(parts)


def _inject_debug_line(md: str, debug: str) -> str:
    """첫 slide 헤더(`### ` 또는 `#### `) 직후에 `_배정 라인: ..._` 삽입.

    헤더 다음에 빈 줄이 있으면 그 아래에, 없으면 빈 줄 포함해 삽입.
    헤더를 찾지 못하면 원본을 그대로 반환.
    """
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("### ") or line.startswith("#### "):
            insert_at = i + 1
            if insert_at < len(lines) and lines[insert_at].strip() == "":
                insert_at += 1
            debug_lines = [f"_배정 라인: {debug}_", ""]
            lines[insert_at:insert_at] = debug_lines
            return "\n".join(lines)
    return md


def _downgrade_slide_header(md: str) -> str:
    """compose 결과의 첫 `### Slide ...` 을 `#### Slide ...` 로 낮춤.

    multi-PDF merge 시 PDF 그룹 헤더(`### 📄 ...`)가 slide 헤더와
    같은 레벨이 되지 않도록 slide 를 한 단계 내림.
    """
    lines = md.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### ") and not stripped.startswith("#### "):
            lines[i] = line.replace("### ", "#### ", 1)
        return "\n".join(lines)
    return md


def _render_unassigned_bucket(
    entries: list[dict],
    numbered_txts: dict[str, dict],
) -> list[str]:
    """unassigned 하나의 타입(chatter/other) 엔트리들을 markdown 블록으로."""
    out: list[str] = []
    for u in entries:
        src = u.get("source")
        s = u.get("start_line")
        e = u.get("end_line")
        reason = u.get("reason", "")
        info = numbered_txts.get(src) if src else None
        header = f"#### {src} lines {s}-{e}"
        if reason:
            header += f" — _{reason}_"
        out.append(header)
        out.append("")
        if info is not None and isinstance(s, int) and isinstance(e, int):
            all_lines = Path(info["path"]).read_text(encoding="utf-8").splitlines()
            excerpt = "\n".join(all_lines[s - 1:e])
            out.append("```")
            out.append(excerpt)
            out.append("```")
            out.append("")
        else:
            out.append("_(내용 로드 실패)_")
            out.append("")
    return out


def merge_pages(
    slides_data: dict,
    page_notes: dict[int, str],
    glossary_md: bytes,
    unassigned: list[dict],
    numbered_txts: dict[str, dict],
    mapping: dict[int, list[dict]] | None = None,
) -> str:
    """최종 단일 markdown 조립.

    - 각 슬라이드 섹션 헤더 아래에 `_배정 라인: ..._` debug 라인 주입
    - unassigned를 type별(chatter / other)로 나눠 말미 섹션 구성
    - multi-PDF 시 PDF 별 `### 📄 {pdf}` 그룹 헤더 + slide 는 `####` 로 강등
    """
    mapping = mapping or {}
    out: list[str] = []
    out.append("# 강의 노트")
    out.append("")
    out.append("> `lecture_note` skill이 자동 생성.")
    out.append("")

    if glossary_md:
        out.append("## 용어집")
        out.append("")
        out.append(glossary_md.decode("utf-8", errors="replace").rstrip())
        out.append("")
        out.append("---")
        out.append("")

    out.append("## 페이지별 노트")
    out.append("")

    pages = slides_data.get("pages", [])
    sources = slides_data.get("sources", []) or []
    is_multi_pdf = len(sources) > 1

    # source_pdf_order 별로 페이지 그룹핑
    pages_by_order: dict[int, list[dict]] = {}
    for page in pages:
        order = page.get("source_pdf_order", 0)
        pages_by_order.setdefault(order, []).append(page)

    def _emit_page(page: dict, level: int) -> None:
        idx = page.get("index")
        debug = _format_range_str(mapping.get(idx, []))
        md = page_notes.get(idx)
        header_prefix = "#" * level
        if not md:
            title = page.get("title", "")
            out.append(f"{header_prefix} Slide {idx}: {title}")
            out.append("")
            out.append(f"_배정 라인: {debug}_")
            out.append("")
            out.append("*(compose 실패 — slide 정보만 표시)*")
            out.append("")
            if page.get("brief"):
                out.append(f"- brief: {page['brief']}")
            if page.get("anchors"):
                out.append(f"- anchors: {', '.join(page['anchors'])}")
            out.append("")
        else:
            body = md.rstrip()
            if level == 4:
                body = _downgrade_slide_header(body)
            injected = _inject_debug_line(body, debug)
            out.append(injected)
            out.append("")

    if is_multi_pdf:
        for src in sources:
            order = src.get("order", 0)
            pdf_name = src.get("pdf", "")
            g_start = src.get("global_start", 0)
            g_end = src.get("global_end", 0)
            out.append(f"### 📄 {pdf_name} (global pages {g_start}~{g_end})")
            out.append("")
            for page in pages_by_order.get(order, []):
                _emit_page(page, level=4)
    else:
        for page in pages:
            _emit_page(page, level=3)

    if unassigned:
        chatter = [u for u in unassigned if u.get("type") == "chatter"]
        other = [u for u in unassigned if u.get("type") == "other"]
        legacy = [
            u for u in unassigned
            if u.get("type") not in ("chatter", "other")
        ]

        out.append("---")
        out.append("")
        out.append("## 할당되지 않은 녹취 구간")
        out.append("")

        if chatter:
            out.append("### 수업과 무관한 잡담 / 행정")
            out.append("")
            out.extend(_render_unassigned_bucket(chatter, numbered_txts))

        if other:
            out.append("### 기타 / 미분류")
            out.append("")
            out.extend(_render_unassigned_bucket(other, numbered_txts))

        if legacy:
            out.append("### (구버전) 미분류")
            out.append("")
            out.extend(_render_unassigned_bucket(legacy, numbered_txts))

    return "\n".join(out).rstrip() + "\n"
