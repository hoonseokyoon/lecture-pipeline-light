"""Step 2b: Step 2 alignment 결과의 LLM 리뷰 pass (최대 1회).

흐름:
1. Python: monotonicity 위반 count. 0이면 LLM 호출 skip.
2. LLM: 전체 slides + 녹취 + 현재 alignment를 보고 **최소 수정** 원칙으로
   배정 조정 후 완전한 alignment array 반환.
3. Python: LLM이 누락한 페이지는 원본에서 복구.
"""

import json
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, run_codex_task

from _lib.prompt_utils import (
    build_grouped_slide_summary,
    build_multi_source_rules,
    build_source_context_block,
    build_source_whitelist_rule,
)
from _lib.schemas import build_alignment_schema


def _emit(log_callback: Callable[[str], None] | None, msg: str) -> None:
    if log_callback is None:
        return
    try:
        log_callback(msg)
    except Exception:
        pass


def _validate_assignments_sources(
    assignments: list,
    allowed_sources: set[str],
    tag: str,
    log_callback: Callable[[str], None] | None,
) -> None:
    """review 결과의 source 필드를 화이트리스트로 검증 + auto-remap.

    align.py 의 동명 함수와 같은 의미. 스키마 enum 이 1차 방어선이지만
    fallback 경로 대비 Python 쪽 이중 체크.
    """
    if not isinstance(assignments, list):
        return
    for item in assignments:
        if not isinstance(item, dict):
            continue
        ranges = item.get("ranges")
        if not isinstance(ranges, list):
            continue
        for r in ranges:
            if not isinstance(r, dict):
                continue
            src = r.get("source")
            if src in allowed_sources:
                continue
            if len(allowed_sources) == 1:
                fixed = next(iter(allowed_sources))
                _emit(
                    log_callback,
                    f"{tag} page {item.get('page')} source "
                    f"{src!r} → {fixed!r} (auto-remap, single-transcript)",
                )
                r["source"] = fixed
            else:
                raise CodexRunError(
                    f"{tag} page {item.get('page')}: invalid source {src!r}, "
                    f"allowed: {sorted(allowed_sources)}"
                )


def count_monotonicity_violations(
    assignments: dict[int, list[dict]],
    tolerance: int = 3,
) -> int:
    """페이지 번호 순으로 읽을 때 start_line이 유의미하게 후퇴하는 케이스 수.

    tolerance 이내의 역전은 경계 미세조정으로 간주해 무시.
    """
    pages_sorted = sorted(assignments.keys())
    violations = 0
    seen_max_start = -1
    for p in pages_sorted:
        ranges = assignments.get(p, [])
        if not ranges:
            continue
        starts = [
            r.get("start_line", 0)
            for r in ranges
            if isinstance(r.get("start_line"), int)
        ]
        if not starts:
            continue
        cur_min = min(starts)
        cur_max = max(starts)
        if seen_max_start > 0 and cur_min < seen_max_start - tolerance:
            violations += 1
        seen_max_start = max(seen_max_start, cur_max)
    return violations


def _build_review_prompt(
    slides_data: dict,
    numbered_txts: dict[str, dict],
    assignments: dict[int, list[dict]],
) -> str:
    lines: list[str] = []
    lines.append("# 작업: Alignment 리뷰 및 경계 미세조정")
    lines.append("")
    lines.append(
        "Step 2가 생성한 slide–녹취록 alignment를 **한 차례** 리뷰하세요. "
        "대부분의 배정은 그대로 두고, 분명히 잘못된 것만 수정하는 것이 목표입니다."
    )
    lines.append("")
    lines.append("## 리뷰 규칙")
    lines.append("")
    lines.append(
        "- **monotonicity**: 페이지 번호가 증가하면 라인 번호도 대체로 증가해야 함. "
        "역전은 교수가 도입부에 해당 슬라이드를 **명백히 미리 언급**한 경우에만 허용."
    )
    lines.append(
        "- **경계 조정**: ±2~3 라인 정도의 경계 조정은 자유롭게. 과도한 재배치 금지."
    )
    lines.append(
        "- **overlap 허용**: 같은 라인을 여러 페이지가 공유하는 것은 교수가 "
        "빠르게 슬라이드를 훑었을 때 정상. 다만 3페이지 이상이 같은 **단일** 라인에 "
        "몰려 있으면 의심하고, 가장 핵심적인 페이지에 남기거나 인접 라인으로 "
        "분산 시도."
    )
    lines.append(
        "- **빈 배열 유지**: `ranges: []`인 페이지는 기본 유지 (교수가 해당 슬라이드를 "
        "빠르게 넘겼을 수 있음). 꼭 필요한 경우에만 배정 추가."
    )
    lines.append(
        "- **불확실하면 건드리지 말 것**. 과잉 수정은 더 나쁨."
    )
    lines.append(
        "- 결과는 **모든 페이지에 대한 완전한 alignment**를 출력. 수정 안 한 페이지도 "
        "그대로 포함."
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
    lines.append("## 현재 alignment (리뷰 대상)")
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
    lines.append("## 출력")
    lines.append("")
    lines.append(
        "리뷰·수정이 반영된 완전한 alignment를 `outputs/assignments.json`에 저장. "
        f"**{len(assignments)}개 페이지 전부에 대한 엔트리**를 `assignments` array에 "
        "포함. 구조 (array of `{page, ranges}`):"
    )
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps({
        "assignments": [
            {
                "page": 1,
                "ranges": [
                    {"source": "<file>", "start_line": 1, "end_line": 1}
                ],
            },
            {"page": 2, "ranges": []},
        ]
    }, ensure_ascii=False, indent=2))
    lines.append("```")
    return "\n".join(lines) + "\n"


def review_alignment(
    assignments: dict[int, list[dict]],
    slides_data: dict,
    numbered_txts: dict[str, dict],
    model: str,
    reasoning_effort: str,
    timeout: int,
    log_callback: Callable[[str], None] | None = None,
) -> dict[int, list[dict]]:
    """LLM 리뷰 pass 1회. 위반 0개면 원본 그대로 반환."""
    violations = count_monotonicity_violations(assignments)
    _emit(log_callback, f"[review] monotonicity 위반 {violations}개")

    if violations == 0:
        _emit(log_callback, "[review] 위반 없음 — 리뷰 skip")
        return assignments

    _emit(log_callback, "[review] LLM 리뷰 호출 중...")
    prompt = _build_review_prompt(slides_data, numbered_txts, assignments)

    allowed_sources = list(numbered_txts.keys())
    schema = build_alignment_schema(allowed_sources)
    allowed_set = set(allowed_sources)

    result = run_codex_task(
        prompt=prompt,
        inputs={"_marker.txt": "review"},
        expected_outputs=["assignments.json"],
        output_schema=schema,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )
    raw = result.get("assignments.json", b"").decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexRunError(
            f"[review] assignments.json 파싱 실패: {exc}\n내용: {raw[:300]}"
        ) from exc

    # source 화이트리스트 validation + auto-remap
    raw_list = data.get("assignments", [])
    _validate_assignments_sources(
        raw_list, allowed_set, "[review]", log_callback,
    )

    reviewed: dict[int, list[dict]] = {}
    if isinstance(raw_list, list):
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            page = item.get("page")
            ranges = item.get("ranges")
            if isinstance(page, int) and isinstance(ranges, list):
                reviewed[page] = ranges

    recovered = 0
    for page, ranges in assignments.items():
        if page not in reviewed:
            reviewed[page] = ranges
            recovered += 1
    if recovered:
        _emit(log_callback, f"[review] 누락된 {recovered}페이지 원본에서 복구")

    new_violations = count_monotonicity_violations(reviewed)
    _emit(
        log_callback,
        f"[review] 완료 — {len(reviewed)}페이지, "
        f"위반 {violations} → {new_violations}",
    )
    return reviewed
