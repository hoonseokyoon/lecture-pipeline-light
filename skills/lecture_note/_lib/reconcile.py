"""Step 3: Python + LLM 하이브리드 orphan reconcile.

흐름:
1. Python: claim map → orphan range 식별 → 자동 분류
   - blank range (모든 라인이 공백) → 즉시 drop
   - length ≤ 2 AND prev_page == next_page → 즉시 auto merge
2. LLM: 나머지 candidate를 {merge_to_page(target) | chatter | other}로 분류
3. Python 후처리:
   - 누락된 candidate는 heuristic fallback
   - merge_to_page → mapping 업데이트
   - chatter / other → unassigned 바구니 (type 필드 포함)
"""

import json
import re
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, run_codex_task

from _lib.prompt_utils import (
    build_grouped_slide_summary,
    build_multi_source_rules,
    build_source_context_block,
    build_source_whitelist_rule,
)
from _lib.schemas import build_reconcile_schema


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
    if m:
        return line[m.end():]
    return line


def _claimed_lines_by_page(
    assignments: dict[int, list[dict]],
) -> dict[tuple[str, int], int]:
    """(source, line_num) → page_idx 맵. 같은 라인을 여러 페이지가 claim하면
    가장 큰 page_idx가 이김 (sorted 순회로 인한 덮어쓰기)."""
    claimed: dict[tuple[str, int], int] = {}
    for page_idx, ranges in sorted(assignments.items()):
        for r in ranges:
            src = r.get("source")
            start = r.get("start_line")
            end = r.get("end_line")
            if not isinstance(src, str) or not isinstance(start, int) or not isinstance(end, int):
                continue
            for ln in range(start, end + 1):
                claimed[(src, ln)] = page_idx
    return claimed


def _find_orphan_ranges(
    claimed: dict[tuple[str, int], int],
    source_line_counts: dict[str, int],
) -> list[tuple[str, int, int]]:
    """각 source별로 claim되지 않은 contiguous 라인 범위 찾기."""
    orphans: list[tuple[str, int, int]] = []
    for src, total in source_line_counts.items():
        in_run = False
        run_start = 0
        for ln in range(1, total + 1):
            if (src, ln) not in claimed:
                if not in_run:
                    run_start = ln
                    in_run = True
            else:
                if in_run:
                    orphans.append((src, run_start, ln - 1))
                    in_run = False
        if in_run:
            orphans.append((src, run_start, total))
    return orphans


def _is_blank_range(path: Path, start: int, end: int) -> bool:
    """range 내 모든 라인이 (numbered prefix 제거 후) 공백인지 확인."""
    try:
        all_lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return False
    for ln in range(start, end + 1):
        idx = ln - 1
        if idx < 0 or idx >= len(all_lines):
            continue
        content = _strip_number_prefix(all_lines[idx])
        if content.strip():
            return False
    return True


def _page_at(
    claimed: dict[tuple[str, int], int],
    src: str,
    line: int,
    total: int,
) -> int | None:
    if line < 1 or line > total:
        return None
    return claimed.get((src, line))


def _append_range(
    assignments: dict[int, list[dict]],
    page_idx: int,
    src: str,
    start: int,
    end: int,
) -> None:
    """페이지의 기존 range 중 해당 source와 인접/겹치는 것이 있으면 extend,
    없으면 새 range를 append."""
    ranges = assignments.setdefault(page_idx, [])
    for r in ranges:
        if r.get("source") != src:
            continue
        r_start = r.get("start_line")
        r_end = r.get("end_line")
        if not isinstance(r_start, int) or not isinstance(r_end, int):
            continue
        if end + 1 >= r_start and start <= r_end + 1:
            r["start_line"] = min(r_start, start)
            r["end_line"] = max(r_end, end)
            return
    ranges.append({"source": src, "start_line": start, "end_line": end})


def _merge_adjacent_ranges(ranges: list[dict]) -> list[dict]:
    """같은 source의 인접/겹치는 range를 합침."""
    if not ranges:
        return ranges
    by_src: dict[str, list[tuple[int, int]]] = {}
    for r in ranges:
        src = r.get("source")
        s = r.get("start_line")
        e = r.get("end_line")
        if not isinstance(src, str) or not isinstance(s, int) or not isinstance(e, int):
            continue
        by_src.setdefault(src, []).append((s, e))
    merged: list[dict] = []
    for src in sorted(by_src.keys()):
        intervals = sorted(by_src[src])
        cur_start, cur_end = intervals[0]
        for s, e in intervals[1:]:
            if s <= cur_end + 1:
                cur_end = max(cur_end, e)
            else:
                merged.append({"source": src, "start_line": cur_start, "end_line": cur_end})
                cur_start, cur_end = s, e
        merged.append({"source": src, "start_line": cur_start, "end_line": cur_end})
    return merged


def _build_reconcile_prompt(
    slides_data: dict,
    numbered_txts: dict[str, dict],
    assignments: dict[int, list[dict]],
    candidates: list[dict],
    auto_summary: dict,
) -> str:
    lines: list[str] = []
    lines.append("# 작업: Orphan 라인 구간 분류")
    lines.append("")
    lines.append(
        "Step 2 alignment가 어떤 슬라이드에도 배정하지 못한 녹취 라인 구간을 "
        "다음 셋 중 하나로 분류하세요."
    )
    lines.append("")
    lines.append("## 분류 action")
    lines.append("")
    lines.append(
        "- `merge_to_page`: 해당 구간이 특정 슬라이드의 설명에 속함. "
        "`target_page`에 해당 slide index."
    )
    lines.append(
        "- `chatter`: 수업 내용과 무관한 잡담/행정/출석/시스템 문제/개인 일화/"
        "작별 인사 등. `target_page=0`."
    )
    lines.append(
        "- `other`: 수업 내용이지만 특정 슬라이드 귀속이 애매하거나 "
        "전반 개요/여담성 설명. `target_page=0`."
    )
    lines.append("")
    lines.append("## 판단 기준")
    lines.append("")
    lines.append(
        "- 출석 호명, PPT 업로드 사고, \"여러분 힘내세요\"류 마무리 격려, 농담 등은 "
        "`chatter`로 분류."
    )
    lines.append(
        "- 슬라이드의 anchor/title/brief와 연관된 발화는 가장 가까운 슬라이드에 "
        "`merge_to_page`."
    )
    lines.append(
        "- `target_page`는 반드시 현재 존재하는 slide index여야 함. "
        "없는 페이지를 target으로 하면 `other` 처리됨."
    )
    lines.append(
        "- 분류가 정말 애매한 경우에만 `other`. 남발 금지."
    )
    lines.append(
        f"- **입력으로 준 {len(candidates)}개 candidate 각각에 대해 정확히 하나의 "
        "결정**을 출력해야 함."
    )
    lines.append("")
    lines.extend(build_source_context_block(slides_data, numbered_txts))
    lines.extend(build_multi_source_rules(slides_data, numbered_txts))
    lines.extend(build_source_whitelist_rule(numbered_txts))
    lines.extend(build_grouped_slide_summary(slides_data))

    lines.append("## 전체 녹취록")
    lines.append("")
    for src_name, info in numbered_txts.items():
        path = info["path"]
        line_count = info["line_count"]
        lines.append(f"### {src_name} (총 {line_count} lines)")
        lines.append("")
        lines.append("```")
        lines.append(Path(path).read_text(encoding="utf-8").rstrip("\n"))
        lines.append("```")
        lines.append("")
    lines.append("## 현재 slide ↔ line 배정 (Step 2 결과, 참고)")
    lines.append("")
    assigned_array = [
        {"page": k, "ranges": v}
        for k, v in sorted(assignments.items())
    ]
    lines.append("```json")
    lines.append(json.dumps(
        {"assignments": assigned_array}, ensure_ascii=False, indent=2
    ))
    lines.append("```")
    lines.append("")
    lines.append("## Python 자동 처리 요약 (참고)")
    lines.append("")
    lines.append(f"- 공백 구간 {auto_summary.get('blank_count', 0)}개 자동 drop")
    lines.append(
        f"- 짧은 same-page 구간 {auto_summary.get('auto_merge_count', 0)}개 자동 merge"
    )
    lines.append("")
    lines.append(f"## 분류할 candidate ({len(candidates)}개)")
    lines.append("")
    lines.append(
        "각 candidate의 `prev_page`/`next_page`는 해당 구간 바로 앞/뒤 라인을 "
        "claim한 슬라이드 번호 (0이면 없음)."
    )
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(
        {"candidates": candidates}, ensure_ascii=False, indent=2
    ))
    lines.append("```")
    lines.append("")
    lines.append("## 출력")
    lines.append("")
    lines.append(
        "각 candidate에 대해 `outputs/decisions.json`에 결정을 담아 저장. "
        f"정확히 {len(candidates)}개 엔트리가 `decisions` array에 있어야 함. 구조:"
    )
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps({
        "decisions": [
            {
                "source": "example.txt",
                "start_line": 10,
                "end_line": 15,
                "action": "chatter",
                "target_page": 0,
                "reason": "출석 확인 발화",
            },
            {
                "source": "example.txt",
                "start_line": 20,
                "end_line": 22,
                "action": "merge_to_page",
                "target_page": 3,
                "reason": "슬라이드 3의 도입 설명",
            },
        ]
    }, ensure_ascii=False, indent=2))
    lines.append("```")
    return "\n".join(lines) + "\n"


def _fallback_decision(cand: dict) -> dict:
    """LLM이 누락한 candidate에 대한 heuristic 결정."""
    prev = cand.get("prev_page") or 0
    nxt = cand.get("next_page") or 0
    if prev > 0 and nxt > 0 and prev == nxt:
        return {
            "action": "merge_to_page",
            "target_page": prev,
            "reason": "fallback: same neighbor",
        }
    if prev > 0 and nxt == 0:
        return {
            "action": "merge_to_page",
            "target_page": prev,
            "reason": "fallback: trailing",
        }
    if prev == 0 and nxt > 0:
        return {
            "action": "merge_to_page",
            "target_page": nxt,
            "reason": "fallback: leading",
        }
    return {
        "action": "other",
        "target_page": 0,
        "reason": "fallback: isolated",
    }


def _call_llm_classify(
    slides_data: dict,
    numbered_txts: dict[str, dict],
    assignments: dict[int, list[dict]],
    candidates: list[dict],
    auto_summary: dict,
    model: str,
    reasoning_effort: str,
    timeout: int,
    log_callback: Callable[[str], None] | None = None,
) -> list[dict]:
    prompt = _build_reconcile_prompt(
        slides_data, numbered_txts, assignments, candidates, auto_summary,
    )

    allowed_sources = list(numbered_txts.keys())
    schema = build_reconcile_schema(allowed_sources)
    allowed_set = set(allowed_sources)

    result = run_codex_task(
        prompt=prompt,
        inputs={"_marker.txt": "reconcile"},
        expected_outputs=["decisions.json"],
        output_schema=schema,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )
    raw = result.get("decisions.json", b"").decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexRunError(
            f"[reconcile] decisions.json 파싱 실패: {exc}\n내용: {raw[:300]}"
        ) from exc
    decisions = data.get("decisions", [])
    if not isinstance(decisions, list):
        return []
    _validate_decisions_sources(
        decisions, allowed_set, "[reconcile]", log_callback,
    )
    return decisions


def _validate_decisions_sources(
    decisions: list,
    allowed_sources: set[str],
    tag: str,
    log_callback: Callable[[str], None] | None,
) -> None:
    """reconcile decisions 의 source 필드 검증 + auto-remap.

    LLM 은 Python 이 candidate 로 넘긴 (source, start, end) 를 그대로 echoed
    back 해야 함. enum 이 1차 방어선이지만 fallback 대비.
    """
    if not isinstance(decisions, list):
        return
    for d in decisions:
        if not isinstance(d, dict):
            continue
        src = d.get("source")
        if src in allowed_sources:
            continue
        if len(allowed_sources) == 1:
            fixed = next(iter(allowed_sources))
            _emit(
                log_callback,
                f"{tag} decision source {src!r} → {fixed!r} "
                f"(auto-remap, single-transcript)",
            )
            d["source"] = fixed
        else:
            raise CodexRunError(
                f"{tag} decision: invalid source {src!r}, "
                f"allowed: {sorted(allowed_sources)}"
            )


def reconcile_orphans(
    assignments: dict[int, list[dict]],
    numbered_txts: dict[str, dict],
    slides_data: dict,
    model: str,
    reasoning_effort: str,
    timeout: int,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """Orphan 구간을 Python + LLM 하이브리드로 분류·병합.

    Returns:
        {
            "mapping": {page_idx: [range, ...]} — 최종 slide → line 배정,
            "unassigned": [
                {"source","start_line","end_line","type","reason"}, ...
            ] — type은 "chatter" 또는 "other"
        }
    """
    norm_assignments: dict[int, list[dict]] = {}
    for k, v in assignments.items():
        try:
            norm_assignments[int(k)] = list(v)
        except (TypeError, ValueError):
            continue

    source_line_counts = {
        name: info["line_count"] for name, info in numbered_txts.items()
    }

    claimed = _claimed_lines_by_page(norm_assignments)
    orphans = _find_orphan_ranges(claimed, source_line_counts)
    _emit(log_callback, f"[reconcile] orphan 구간 {len(orphans)}개 식별")

    unassigned: list[dict] = []
    candidates: list[dict] = []
    blank_count = 0
    auto_merge_count = 0

    for src, os_, oe in orphans:
        info = numbered_txts.get(src)
        if info is None:
            continue
        path = Path(info["path"])
        total = source_line_counts.get(src, 0)

        if _is_blank_range(path, os_, oe):
            blank_count += 1
            continue

        prev_page = _page_at(claimed, src, os_ - 1, total)
        next_page = _page_at(claimed, src, oe + 1, total)
        length = oe - os_ + 1

        if length <= 2 and prev_page is not None and prev_page == next_page:
            _append_range(norm_assignments, prev_page, src, os_, oe)
            auto_merge_count += 1
            continue

        candidates.append({
            "source": src,
            "start_line": os_,
            "end_line": oe,
            "length": length,
            "prev_page": prev_page if prev_page is not None else 0,
            "next_page": next_page if next_page is not None else 0,
        })

    _emit(
        log_callback,
        f"[reconcile] 자동 처리: blank={blank_count}, "
        f"auto_merge={auto_merge_count}, LLM 분류 대상={len(candidates)}",
    )

    if candidates:
        _emit(log_callback, "[reconcile] LLM 분류 호출 중...")
        decisions = _call_llm_classify(
            slides_data=slides_data,
            numbered_txts=numbered_txts,
            assignments=norm_assignments,
            candidates=candidates,
            auto_summary={
                "blank_count": blank_count,
                "auto_merge_count": auto_merge_count,
            },
            model=model,
            reasoning_effort=reasoning_effort,
            timeout=timeout,
            log_callback=log_callback,
        )

        decision_map: dict[tuple[str, int, int], dict] = {}
        for d in decisions:
            if not isinstance(d, dict):
                continue
            key = (d.get("source"), d.get("start_line"), d.get("end_line"))
            decision_map[key] = d

        existing_pages = set(norm_assignments.keys())

        for cand in candidates:
            key = (cand["source"], cand["start_line"], cand["end_line"])
            decision = decision_map.get(key)

            if decision is None:
                decision = _fallback_decision(cand)
                _emit(
                    log_callback,
                    f"[reconcile] 누락 candidate fallback: {cand['source']}:"
                    f"{cand['start_line']}-{cand['end_line']} → {decision['action']}",
                )

            action = decision.get("action", "other")
            target_page = decision.get("target_page", 0)
            reason = decision.get("reason", "")

            if (
                action == "merge_to_page"
                and isinstance(target_page, int)
                and target_page > 0
                and target_page in existing_pages
            ):
                _append_range(
                    norm_assignments, target_page,
                    cand["source"], cand["start_line"], cand["end_line"],
                )
            elif action == "chatter":
                unassigned.append({
                    "source": cand["source"],
                    "start_line": cand["start_line"],
                    "end_line": cand["end_line"],
                    "type": "chatter",
                    "reason": reason,
                })
            else:
                note = reason
                if action == "merge_to_page":
                    note = f"invalid target_page={target_page}; {reason}".strip("; ")
                unassigned.append({
                    "source": cand["source"],
                    "start_line": cand["start_line"],
                    "end_line": cand["end_line"],
                    "type": "other",
                    "reason": note,
                })

    for page_idx in list(norm_assignments.keys()):
        norm_assignments[page_idx] = _merge_adjacent_ranges(
            norm_assignments[page_idx]
        )

    chatter_n = sum(1 for u in unassigned if u.get("type") == "chatter")
    other_n = sum(1 for u in unassigned if u.get("type") == "other")
    _emit(
        log_callback,
        f"[reconcile] 완료 — mapping {len(norm_assignments)}페이지, "
        f"unassigned chatter={chatter_n}, other={other_n}",
    )

    return {"mapping": norm_assignments, "unassigned": unassigned}
