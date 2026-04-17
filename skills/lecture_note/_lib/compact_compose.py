"""Step 10: 페이지별 compact 재작성 (병렬).

각 페이지에 대해 기존 polished note + 녹취 발췌 + exam cues를 입력으로,
'처음 접하는 독자에게 가르쳐주듯' 재작성. footnotes로 교수 인사이트 분리.

Per-page 결과는 step10_compact_pages/page_NNN.json 으로 캐시.
병렬화는 compose_pages_parallel 패턴과 동일.
"""

import json
from concurrent.futures import as_completed
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, ContextThreadPoolExecutor, run_codex_task

from _lib.compact_schemas import COMPACT_PAGE_SCHEMA


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
    """페이지 배정 range들의 녹취 텍스트."""
    chunks: list[str] = []
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
        try:
            all_lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        chunks.append(f"[{src} lines {s}-{e}]")
        chunks.extend(all_lines[s - 1:e])
        chunks.append("")
    return "\n".join(chunks).strip()


def _exam_cues_for_page(
    exam_cues: dict | None,
    lecture_summary: dict | None,
    page_idx: int,
) -> tuple[str, list[dict]]:
    """해당 페이지의 emphasis level + 관련 professor_exam_comments 반환.

    exam_cues가 None이면 lecture_summary.page_importance에서 fallback.
    """
    level = "medium"
    comments: list[dict] = []

    if exam_cues is not None:
        for item in exam_cues.get("page_emphasis", []) or []:
            if isinstance(item, dict) and item.get("page") == page_idx:
                lvl = item.get("level")
                if lvl in ("high", "medium", "low"):
                    level = lvl
                break
        comments = [
            c for c in (exam_cues.get("professor_exam_comments", []) or [])
            if isinstance(c, dict) and c.get("slide_page") == page_idx
        ]
    elif lecture_summary is not None:
        for item in lecture_summary.get("page_importance", []) or []:
            if isinstance(item, dict) and item.get("page") == page_idx:
                raw = item.get("level", "")
                level = {"important": "high", "transitional": "low"}.get(
                    raw, "medium",
                )
                break

    return level, comments


def _build_compact_prompt(
    page: dict,
    transcript_excerpt: str,
    existing_note_md: str,
    emphasis_level: str,
    exam_comments_for_page: list[dict],
    lecture_summary: dict,
    slides_data: dict,
) -> str:
    idx = page.get("index")
    title = page.get("title", "")
    source_pdf = page.get("source_pdf", "")
    local_page = page.get("local_page", 0)

    lines: list[str] = []
    lines.append(
        "당신은 강의 노트의 한 페이지를 **처음 접하는 독자에게 가르쳐주듯** "
        "재작성합니다. 독자는 이 한 페이지로 해당 슬라이드의 내용을 이해하고 "
        "넘어갈 수 있어야 합니다."
    )
    lines.append("")

    # 전역 컨텍스트
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

    # 현재 슬라이드
    lines.append("## 이 슬라이드")
    lines.append("")
    lines.append(f"- **index**: {idx}")
    lines.append(f"- **title**: {title}")
    if source_pdf:
        lines.append(f"- **source_pdf**: {source_pdf} (local p.{local_page})")
    anchors = page.get("anchors", []) or []
    if anchors:
        lines.append(f"- **anchors**: {', '.join(anchors)}")
    brief = page.get("brief", "")
    if brief:
        lines.append(f"- **brief**: {brief}")
    lines.append(f"- **emphasis_level**: `{emphasis_level}`")
    lines.append("")

    # 기존 정리본
    lines.append("## 기존 note 섹션 (이미 다듬어진 버전)")
    lines.append("")
    if existing_note_md.strip():
        lines.append("```markdown")
        lines.append(existing_note_md.rstrip())
        lines.append("```")
    else:
        lines.append("(기존 note 섹션 없음)")
    lines.append("")

    # 녹취 발췌
    lines.append("## 원본 녹취 발췌")
    lines.append("")
    if transcript_excerpt:
        lines.append("```")
        lines.append(transcript_excerpt)
        lines.append("```")
    else:
        lines.append("(이 슬라이드에 배정된 녹취 없음)")
    lines.append("")

    # 이 페이지의 시험 코멘트
    if exam_comments_for_page:
        lines.append("## 이 슬라이드에 연관된 교수자의 시험 코멘트")
        lines.append("")
        for c in exam_comments_for_page:
            src = c.get("source", "")
            s = c.get("start_line", "")
            e = c.get("end_line", "")
            ph = c.get("paraphrase", "")
            lines.append(f"- [{src}:{s}-{e}] {ph}")
        lines.append("")

    # 작업 지시
    lines.append("## 작업")
    lines.append("")
    lines.append(
        "`outputs/compact.json`에 세 필드를 담아 저장: `narrative_markdown`, "
        "`footnotes`, `referenced_terms`."
    )
    lines.append("")
    lines.append("### `narrative_markdown`")
    lines.append("")
    lines.append(
        "- **독자는 이 슬라이드를 처음 본다**고 가정. 맥락 → 핵심 → 의의 "
        "순으로 풀어서 설명."
    )
    lines.append(
        "- 기존 note 섹션의 `**정리**` 본문을 그대로 베끼지 말고, **가르치는 "
        "톤**으로 재구성. 필요한 기본 개념은 짧게 복습."
    )
    lines.append(
        "- 분량: **페이지 당 10줄 내외를 목표**. 강제 아님 — 자연스러운 "
        "길이가 우선. 너무 축약하면 의미 손실. 복잡한 메커니즘이면 더 써도 됨."
    )
    lines.append(
        "- Markdown inline formatting 허용 (**bold**, `code`, _italic_). "
        "**##/### 헤더는 사용 금지** (섹션 구조는 Python이 감쌈)."
    )
    lines.append(
        "- 핵심 용어가 등장할 때 **굵게** 표기 (용어집 크로스 참조에 사용)."
    )
    lines.append(
        "- 각주: 교수자가 덧붙인 직관·경험담·'실제 임상에서는…' 같은 insight, "
        "그리고 **시험 관련 언급**은 `[^1]`, `[^2]` 마커로 본문에 삽입."
    )
    lines.append("")
    lines.append("### `footnotes`")
    lines.append("")
    lines.append(
        "각 엔트리는 `{marker: '^1', text: '...'}`. narrative의 [^N] 마커와 "
        "1:1 대응. 각주가 없으면 **빈 배열**."
    )
    lines.append("")
    lines.append("### `referenced_terms`")
    lines.append("")
    lines.append(
        "이 페이지에서 독자가 알아야 할 핵심 용어 목록 (재작성본에 등장한 것 + "
        "이해에 필요한 것). 나중에 용어집에 크로스 참조됨."
    )
    lines.append("")

    # emphasis 별 가이드
    lines.append("## 강조도별 길이 가이드")
    lines.append("")
    if emphasis_level == "high":
        lines.append(
            "`high` — 교수가 강조한 페이지. 핵심 메시지를 더 정성껏 풀어 씀. "
            "시험 관련성이 있으면 각주로 명시."
        )
    elif emphasis_level == "low":
        lines.append(
            "`low` — 교수가 빠르게 넘긴 페이지. narrative는 **1~3줄로 간결히**. "
            "의의·위치만 짚고 넘어감."
        )
    else:
        lines.append("`medium` — 일반 페이지. 10줄 내외 목표로 자연스럽게.")
    lines.append("")
    lines.append(
        "언어: 기존 note와 동일 (보통 한국어)."
    )
    return "\n".join(lines) + "\n"


def compact_compose_one_page(
    page: dict,
    mapping: dict[int, list[dict]],
    numbered_txts: dict[str, dict],
    existing_note_md: str,
    exam_cues: dict | None,
    lecture_summary: dict,
    slides_data: dict,
    model: str,
    reasoning_effort: str,
    service_tier: str = "default",
    timeout: int = 1200,
) -> dict:
    """한 페이지 compact 재작성. returns {narrative_markdown, footnotes, referenced_terms}."""
    idx = page.get("index")
    ranges = mapping.get(idx, []) if isinstance(idx, int) else []
    transcript_excerpt = _extract_transcript_excerpt(numbered_txts, ranges)
    emphasis_level, comments_for_page = _exam_cues_for_page(
        exam_cues, lecture_summary, idx if isinstance(idx, int) else -1,
    )
    prompt = _build_compact_prompt(
        page=page,
        transcript_excerpt=transcript_excerpt,
        existing_note_md=existing_note_md,
        emphasis_level=emphasis_level,
        exam_comments_for_page=comments_for_page,
        lecture_summary=lecture_summary,
        slides_data=slides_data,
    )
    result = run_codex_task(
        prompt=prompt,
        inputs={"_marker.txt": f"compact page {idx}"},
        expected_outputs=["compact.json"],
        output_schema=COMPACT_PAGE_SCHEMA,
        model=model,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
        timeout=timeout,
    )
    raw = result.get("compact.json", b"").decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexRunError(
            f"[compact_compose] page {idx} JSON 파싱 실패: {exc}\n"
            f"내용 앞 300: {raw[:300]}"
        ) from exc

    narrative = data.get("narrative_markdown", "")
    footnotes = data.get("footnotes", [])
    referenced = data.get("referenced_terms", [])
    if not isinstance(narrative, str):
        narrative = ""
    if not isinstance(footnotes, list):
        footnotes = []
    if not isinstance(referenced, list):
        referenced = []
    return {
        "narrative_markdown": narrative,
        "footnotes": footnotes,
        "referenced_terms": referenced,
    }


def compact_compose_parallel(
    slides_data: dict,
    mapping: dict[int, list[dict]],
    numbered_txts: dict[str, dict],
    page_notes: dict[int, str],
    exam_cues: dict | None,
    lecture_summary: dict,
    pages_dir: Path,
    model: str,
    reasoning_effort: str,
    service_tier: str,
    timeout: int,
    max_workers: int,
    log_callback: Callable[[str], None] | None,
) -> dict[int, dict]:
    """모든 페이지를 병렬로 compact 재작성. 이미 존재하는 페이지 json은 재사용."""
    pages_dir.mkdir(parents=True, exist_ok=True)
    results: dict[int, dict] = {}
    pages = slides_data.get("pages", []) or []
    page_by_idx = {
        p.get("index"): p for p in pages if isinstance(p.get("index"), int)
    }
    total = len(page_by_idx)

    pending: list[int] = []
    for idx in sorted(page_by_idx.keys()):
        ckpt = pages_dir / f"page_{idx:03d}.json"
        if ckpt.exists():
            try:
                results[idx] = json.loads(ckpt.read_text(encoding="utf-8"))
            except Exception:
                pending.append(idx)
        else:
            pending.append(idx)

    _emit(
        log_callback,
        f"[compact_compose] 총 {total}페이지, 캐시 {total - len(pending)}, "
        f"실행 {len(pending)}",
    )

    if not pending:
        return results

    def worker(idx: int) -> tuple[int, dict | None, str | None]:
        try:
            data = compact_compose_one_page(
                page=page_by_idx[idx],
                mapping=mapping,
                numbered_txts=numbered_txts,
                existing_note_md=page_notes.get(idx, ""),
                exam_cues=exam_cues,
                lecture_summary=lecture_summary,
                slides_data=slides_data,
                model=model,
                reasoning_effort=reasoning_effort,
                service_tier=service_tier,
                timeout=timeout,
            )
            return (idx, data, None)
        except Exception as exc:
            return (idx, None, str(exc))

    with ContextThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(worker, idx): idx for idx in pending}
        done_count = 0
        for fut in as_completed(futures):
            idx, data, err = fut.result()
            done_count += 1
            if data is not None:
                results[idx] = data
                (pages_dir / f"page_{idx:03d}.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                _emit(
                    log_callback,
                    f"[compact_compose] {done_count}/{len(pending)} — page {idx} 완료",
                )
            else:
                _emit(
                    log_callback,
                    f"[compact_compose] {done_count}/{len(pending)} — "
                    f"page {idx} 실패: {err}",
                )

    return results
