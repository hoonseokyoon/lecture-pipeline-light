"""Step 5b: sliding-window sequential polish.

Step 5 compose가 페이지별로 독립 생성한 markdown을 전체 흐름 관점에서 다듬는 단계.

기존(단일 Codex call × 67페이지)에서 → 15페이지 배치, 2페이지 overlap, 순차 호출.

각 worker는:
- inputs/page_NNN.md : 전체 원본 compose 결과 (모두 열람 가능)
- inputs/done/page_NNN.md : 이전 worker가 polish한 결과 (누적)
- 프롬프트에 compact narrative 전문 포함 (전체 강의 맥락)
- outputs/page_NNN.md : 자기 배치 범위만 출력

Overlap 2페이지: worker N+1은 worker N의 마지막 2페이지를 다시 polish.
이전 worker 결과를 볼 수 있으므로 흐름 연결 유지.
"""

import json
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, run_codex_task


def _emit(log_callback: Callable[[str], None] | None, msg: str) -> None:
    if log_callback is None:
        return
    try:
        log_callback(msg)
    except Exception:
        pass


def _purge_failed_worker_markers(
    cache_dir: Path,
    log_callback: Callable[[str], None] | None,
    step_tag: str,
) -> None:
    """과거 버그 버전이 남긴 'failed — ...' marker 들을 제거.

    marker 파일명은 `_worker_NN_done`, 내용이 'failed' 로 시작하면 제거.
    해당 범위(pages X-Y) 의 page_NNN.md 캐시도 함께 삭제해서 재시도를 유도한다.
    """
    import re
    for marker in cache_dir.glob("_worker_*_done"):
        try:
            content = marker.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not content.lstrip().lower().startswith("failed"):
            continue
        m = re.search(r"pages\s+(\d+)\s*-\s*(\d+)", content)
        purged_pages: list[int] = []
        if m:
            first, last = int(m.group(1)), int(m.group(2))
            for idx in range(first, last + 1):
                # .md 와 .json 양쪽 모두 대응 (step5b / step5c 공용)
                for ext in ("md", "json"):
                    p = cache_dir / f"page_{idx:03d}.{ext}"
                    if p.exists():
                        try:
                            p.unlink()
                            purged_pages.append(idx)
                        except OSError:
                            pass
        try:
            marker.unlink()
        except OSError:
            pass
        _emit(
            log_callback,
            f"[{step_tag}] 과거 failed marker {marker.name} 정리 "
            f"(재시도 대상 페이지 {sorted(set(purged_pages))})",
        )


def _compute_batches(
    sorted_idxs: list[int],
    batch_size: int,
    overlap: int,
) -> list[list[int]]:
    """sliding window로 배치 분할. 반환: [[idx, ...], ...]"""
    stride = batch_size - overlap
    batches: list[list[int]] = []
    start = 0
    while start < len(sorted_idxs):
        end = min(start + batch_size, len(sorted_idxs))
        batches.append(sorted_idxs[start:end])
        if end >= len(sorted_idxs):
            break
        start += stride
    return batches


def _format_compact_overview(
    compact_pages: dict[int, dict],
    sorted_idxs: list[int],
) -> str:
    """compact narrative 전문을 프롬프트용 텍스트로 포매팅."""
    parts: list[str] = []
    for idx in sorted_idxs:
        data = compact_pages.get(idx)
        if data is None:
            parts.append(f"### Page {idx}\n(compact 없음)\n")
            continue
        narrative = data.get("narrative_markdown", "").strip()
        footnotes = data.get("footnotes", []) or []
        parts.append(f"### Page {idx}")
        if narrative:
            parts.append(narrative)
        else:
            parts.append("(compact 없음)")
        if footnotes:
            fn_lines = []
            for fn in footnotes:
                marker = fn.get("marker", "")
                text = fn.get("text", "")
                fn_lines.append(f"  [^{marker}] {text}")
            parts.append("\n".join(fn_lines))
        parts.append("")
    return "\n".join(parts)


def _build_sliding_polish_prompt(
    batch_indices: list[int],
    all_sorted_idxs: list[int],
    compact_overview: str,
    lecture_summary: dict,
    has_prior_outputs: bool,
    worker_num: int,
    total_workers: int,
) -> str:
    first = batch_indices[0]
    last = batch_indices[-1]
    num = len(batch_indices)

    lines: list[str] = []
    lines.append(
        f"# 작업: 강의 노트 polish (Worker {worker_num}/{total_workers})"
    )
    lines.append("")
    lines.append(
        f"당신은 강의 노트의 **page {first}~{last}** ({num}페이지)을 polish합니다. "
        f"전체 강의는 {len(all_sorted_idxs)}페이지입니다."
    )
    lines.append("")

    # 강의 컨텍스트 (compact 요약)
    lines.append("## 전체 강의 compact 요약 (참고)")
    lines.append("")
    lines.append(
        "아래는 전 페이지를 10줄 내외로 재작성한 요약입니다. "
        "전체 흐름 파악 및 중복 판단에 참고하세요."
    )
    lines.append("")
    lines.append(compact_overview)
    lines.append("")

    # lecture_summary
    lines.append("## 강의 메타 컨텍스트")
    lines.append("")
    theme = lecture_summary.get("overall_theme", "")
    if theme:
        lines.append(f"**주제**: {theme}")
    mechs = lecture_summary.get("key_mechanisms", []) or []
    if mechs:
        lines.append("**핵심 메커니즘**:")
        for m in mechs:
            lines.append(f"  - {m}")
    imp = lecture_summary.get("page_importance", []) or []
    imp_items = [
        item for item in imp
        if isinstance(item, dict) and item.get("level") == "important"
        and isinstance(item.get("page"), int)
        and item["page"] in batch_indices
    ]
    if imp_items:
        lines.append("**이번 범위 내 중요 페이지** (깊이 보존 우선):")
        for item in imp_items:
            lines.append(f"  - Page {item['page']}: {item.get('reason', '')}")
    lines.append("")

    # 파일 구조 설명
    lines.append("## 파일 구조")
    lines.append("")
    lines.append(
        f"- `inputs/page_NNN.md` : 전체 {len(all_sorted_idxs)}페이지 원본 "
        "(Step 5 compose 결과). 필요하면 자유롭게 열람 가능."
    )
    if has_prior_outputs:
        lines.append(
            "- `inputs/done/page_NNN.md` : 이전 worker가 polish한 결과. "
            "흐름 연결을 위해 참고하세요."
        )
    lines.append(
        f"- `outputs/page_{first:03d}.md` ~ `outputs/page_{last:03d}.md` : "
        f"당신이 polish한 결과를 저장할 위치. **{num}개 파일 모두 필수**."
    )
    lines.append("")

    # Polish 원칙
    lines.append("## Polish 원칙 (수정 범위)")
    lines.append("")
    lines.append(
        "- **인접 페이지 중복 축소**: 같은 개념을 여러 페이지가 반복 도입하면, "
        "첫 등장 이후 중복 설명을 간결화 (고유 정보는 보존)."
    )
    lines.append(
        "- **자연스러운 전환 언어**: \"앞 슬라이드에서 본 X를 바탕으로\" 같은 "
        "연결 문구. 억지로 넣지 말고 자연스러운 곳에만."
    )
    lines.append("- **어색한 한국어 교정**: 문장 흐름이 매끄럽지 않은 부분만.")
    lines.append("")
    lines.append("## 건드리지 말 것 (구조 보존)")
    lines.append("")
    lines.append(
        "- `> ` blockquote (강의 발화 verbatim 인용) — 한 글자도 변경/삭제 금지"
    )
    lines.append("- `### Slide` 헤더 — 번호·제목 고정")
    lines.append("- `**핵심 용어**:` 불릿 — 용어/정의는 schema 검증 값")
    lines.append("- 사실/정의/숫자/고유명사 — 학술 용어 변경 금지")
    lines.append("")
    lines.append("## 자유 수정 가능 (`**정리**:` 본문)")
    lines.append("")
    lines.append("- 인접 페이지와 전환 문구 추가")
    lines.append("- 중복 배경 설명 trim")
    lines.append("- 어색한 한국어 다듬기, 가독성 개선")
    lines.append("- 한 문장 정도의 보충 해설 OK (근거 있는 내용에 한정)")
    lines.append("")
    lines.append("## 출력 (엄격)")
    lines.append("")
    lines.append(
        f"- `outputs/page_{first:03d}.md` ~ `outputs/page_{last:03d}.md` "
        f"**모두 {num}개** 저장."
    )
    lines.append(
        "- 수정 불필요한 페이지도 원본 내용 그대로 복사해서 저장. "
        "**누락 = 실패**."
    )
    lines.append("- 원본 markdown 형식 유지 (프론트매터 없이).")
    lines.append("- 언어는 원본(한국어) 유지.")
    return "\n".join(lines) + "\n"


def polish_pages(
    page_notes: dict[int, str],
    compact_pages: dict[int, dict],
    slides_data: dict,
    lecture_summary: dict,
    polish_cache_dir: Path,
    model: str,
    reasoning_effort: str,
    service_tier: str = "default",
    timeout: int = 1200,
    batch_size: int = 15,
    overlap: int = 2,
    log_callback: Callable[[str], None] | None = None,
) -> dict[int, str]:
    """Sliding-window sequential polish.

    Returns:
        polished {page_idx: markdown}. 전체 실패 시 원본 page_notes 반환.
    """
    if not page_notes:
        _emit(log_callback, "[step5b] 페이지 없음 — polish skip")
        return {}

    sorted_idxs = sorted(page_notes.keys())
    num_pages = len(sorted_idxs)
    polish_cache_dir.mkdir(parents=True, exist_ok=True)

    # 과거 버그 버전에서 실패 시 marker 를 "failed — ..." 로 남기고 원본을
    # page_NNN.md 로 저장했던 유물을 정리. 해당 worker 의 marker 와 그 범위의
    # 페이지 캐시를 지워 재시도시킨다.
    _purge_failed_worker_markers(polish_cache_dir, log_callback, "step5b")

    # 전체 캐시 히트 체크
    cached_all = True
    for idx in sorted_idxs:
        if not (polish_cache_dir / f"page_{idx:03d}.md").exists():
            cached_all = False
            break
    if cached_all:
        _emit(log_callback, f"[step5b] cache hit — {num_pages}페이지 전부 load")
        return {
            idx: (polish_cache_dir / f"page_{idx:03d}.md").read_text(
                encoding="utf-8",
            )
            for idx in sorted_idxs
        }

    # 배치 분할
    batches = _compute_batches(sorted_idxs, batch_size, overlap)
    _emit(
        log_callback,
        f"[step5b] {num_pages}페이지 → {len(batches)}배치 "
        f"(batch_size={batch_size}, overlap={overlap})",
    )

    # compact 요약 프롬프트 텍스트
    compact_overview = _format_compact_overview(compact_pages, sorted_idxs)

    # 누적 polish 결과
    polished: dict[int, str] = {}

    # worker별 캐시 + 순차 실행
    for wi, batch in enumerate(batches):
        worker_num = wi + 1
        first_idx = batch[0]
        last_idx = batch[-1]
        marker = polish_cache_dir / f"_worker_{worker_num:02d}_done"

        # worker 캐시 체크
        if marker.exists():
            for idx in batch:
                ckpt = polish_cache_dir / f"page_{idx:03d}.md"
                if ckpt.exists():
                    polished[idx] = ckpt.read_text(encoding="utf-8")
            _emit(
                log_callback,
                f"[step5b] worker {worker_num}/{len(batches)} cache hit "
                f"(pages {first_idx}-{last_idx})",
            )
            continue

        _emit(
            log_callback,
            f"[step5b] worker {worker_num}/{len(batches)} "
            f"— pages {first_idx}-{last_idx} ({len(batch)}p) 호출 중...",
        )

        # inputs 구성
        inputs: dict[str, str] = {}
        # 원본 전체
        for idx in sorted_idxs:
            inputs[f"page_{idx:03d}.md"] = page_notes[idx]
        # 이전 worker 결과 (누적)
        for idx in sorted(polished.keys()):
            inputs[f"done/page_{idx:03d}.md"] = polished[idx]
        # context
        inputs["_lecture_summary.json"] = json.dumps(
            lecture_summary, ensure_ascii=False, indent=2,
        )

        expected_outputs = [f"page_{idx:03d}.md" for idx in batch]

        prompt = _build_sliding_polish_prompt(
            batch_indices=batch,
            all_sorted_idxs=sorted_idxs,
            compact_overview=compact_overview,
            lecture_summary=lecture_summary,
            has_prior_outputs=bool(polished),
            worker_num=worker_num,
            total_workers=len(batches),
        )

        try:
            result = run_codex_task(
                prompt=prompt,
                inputs=inputs,
                expected_outputs=expected_outputs,
                model=model,
                reasoning_effort=reasoning_effort,
                service_tier=service_tier,
                timeout=timeout,
            )
        except CodexRunError as exc:
            # 실패 시 cache 에 원본을 쓰지 않고, marker 도 남기지 않는다.
            # 이전 worker 들의 cache 는 유지되므로 재실행 시 이 worker 부터 재시도.
            # raise 로 즉시 중단해서 unpolished 데이터가 step5c 이후로 누수되는 것 차단.
            _emit(
                log_callback,
                f"[step5b] worker {worker_num} 실패 — pipeline 중단 "
                f"(pages {first_idx}-{last_idx}): {exc}",
            )
            raise

        # 결과 수집
        changed = 0
        missing = 0
        for idx in batch:
            key = f"page_{idx:03d}.md"
            raw = result.get(key, b"")
            if raw:
                try:
                    text = raw.decode("utf-8")
                except Exception:
                    text = raw.decode("utf-8", errors="replace")
                if not text.strip():
                    text = page_notes[idx]
                    missing += 1
                elif text.strip() != page_notes[idx].strip():
                    changed += 1
            else:
                text = page_notes[idx]
                missing += 1
            polished[idx] = text
            (polish_cache_dir / f"page_{idx:03d}.md").write_text(
                text, encoding="utf-8",
            )

        marker.write_text(
            f"pages {first_idx}-{last_idx} — changed={changed} missing={missing}",
            encoding="utf-8",
        )
        _emit(
            log_callback,
            f"[step5b] worker {worker_num}/{len(batches)} 완료 "
            f"(변경 {changed}, 누락→원본 {missing})",
        )

    # 혹시 polished에 빠진 페이지 있으면 원본으로 채움
    for idx in sorted_idxs:
        if idx not in polished:
            polished[idx] = page_notes[idx]

    _emit(
        log_callback,
        f"[step5b] 전체 완료 — {num_pages}페이지 polish 반영",
    )
    return polished
