"""Step 2: 페이지별 alignment를 배치 순차 처리.

각 배치:
- 공통 prefix: 전체 slide anchors + 전체 numbered transcript(s) + 규칙
- 가변 suffix: prior_assignments (이전 배치까지의 결과) + 이번 배치 작업 지시

배치 간 overlap으로 앞 페이지 경계 미세 조정 허용.
고정 prefix가 매 호출 동일해 OpenAI 자동 프롬프트 캐싱이 작동.
"""

import json
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, run_codex_task

from _lib.batching import batch_ranges
from _lib.prompt_utils import (
    build_grouped_slide_summary,
    build_multi_source_rules,
    build_source_context_block,
    build_source_whitelist_rule,
)
from _lib.schemas import build_alignment_schema


def build_batch_prompt(
    slides_data: dict,
    numbered_txts: dict[str, dict],
    prior_assignments: dict[int, list],
    target_start: int,
    target_end: int,
    overlap_start: int | None,
) -> str:
    """배치용 prompt 생성.

    Args:
        slides_data: {"pages": [{"index", "title", "anchors", "brief"}, ...]}
        numbered_txts: {src_name: {"path": Path, "line_count": int}}
        prior_assignments: {page_idx: [{"source","start_line","end_line"}, ...]}
        target_start, target_end: 이번 배치의 페이지 번호 범위 (inclusive)
        overlap_start: overlap이 시작되는 페이지 번호 (prior에 존재하는 첫 번째).
            None이면 첫 배치라 overlap 없음.
    """
    lines: list[str] = []

    # ── 고정 prefix 시작 ──
    lines.append("# 작업: 강의 슬라이드와 녹취록 alignment")
    lines.append("")
    lines.append(
        "당신은 각 슬라이드 페이지가 녹취록의 어느 부분에서 논의됐는지를 "
        "**절대 라인 번호**로 배정하는 작업을 합니다."
    )
    lines.append("")
    lines.append("## 규칙")
    lines.append("")
    lines.append("- 각 페이지는 녹취록의 **직접 관련된** 부분만 claim합니다.")
    lines.append("- **가급적 한 구간**, 최대 두 구간까지 허용.")
    lines.append("- 페이지 번호가 커질수록 녹취록 뒤쪽으로 **단조 증가**가 기본.")
    lines.append("- 이전 배치가 배정한 결과는 prior_assignments로 제공됩니다. "
                 "target 범위 중 overlap 페이지의 경계는 필요 시 미세 조정 가능.")
    lines.append("- `source`는 녹취록 파일의 basename, `start_line`/`end_line`은 "
                 "numbered 녹취록의 `[NNNN] ` prefix 번호 그대로.")
    lines.append("- 페이지 내용과 직접 매칭이 안 되는 경우(교수가 슬라이드를 "
                 "넘기며 다음으로 빨리 지나간 경우 등) 빈 배열로 두는 것도 허용.")
    lines.append("- `inputs/` 에 PDF 원본이 있음 (multi-PDF 시 파일명별로 보존). "
                 "필요 시 shell 로 확인 가능 (필수 아님).")
    lines.append("")

    # ── multi-source 컨텍스트 + 규칙 + source 화이트리스트 ──
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

    # ── 고정 prefix 끝, 가변 suffix 시작 ──
    lines.append("---")
    lines.append("")
    lines.append("## 현재 배치")
    lines.append("")
    lines.append(f"- **target 페이지**: {target_start} ~ {target_end}")
    if overlap_start is not None and overlap_start <= target_end:
        lines.append(
            f"- **overlap** (재검토 가능): {overlap_start} ~ "
            f"{min(target_end, max(prior_assignments.keys()) if prior_assignments else 0)}"
        )
    lines.append("")

    if prior_assignments:
        lines.append("### 이전 배치까지의 배정 결과 (prior_assignments)")
        lines.append("")
        lines.append("```json")
        # 정수 페이지 순 정렬 후 array 구조로 직렬화.
        prior_array = [
            {"page": k, "ranges": v}
            for k, v in sorted(prior_assignments.items())
        ]
        lines.append(json.dumps(
            {"assignments": prior_array}, ensure_ascii=False, indent=2
        ))
        lines.append("```")
        lines.append("")

    lines.append("## 출력")
    lines.append("")
    lines.append(
        f"페이지 {target_start}~{target_end} 각각에 대해 라인 구간을 배정해 "
        "`outputs/assignments.json`에 저장. 구조 (array of {page, ranges}):"
    )
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps({
        "assignments": [
            {
                "page": target_start,
                "ranges": [
                    {"source": "<file>", "start_line": 1, "end_line": 1}
                ]
            },
            {
                "page": target_start + 1,
                "ranges": []
            }
        ]
    }, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append(
        f"**target 범위 내 모든 페이지** ({target_start}~{target_end})에 대한 "
        "엔트리를 `assignments` array에 포함해야 함. 배정할 라인이 없는 페이지는 "
        "`ranges`를 빈 배열로. overlap 페이지(이전 배치에서 배정된 앞 페이지들)는 "
        "prior_assignments를 참고해 경계를 미세 조정하거나 그대로 둘 수 있음. "
        "`ranges`의 각 엔트리는 가급적 1구간, 최대 2구간까지만."
    )
    return "\n".join(lines) + "\n"


def align_pages_batched(
    slides_data: dict,
    numbered_txts: dict[str, dict],
    pdf_paths: list[Path],
    batch_ckpt_dir: Path,
    batch_size: int,
    overlap: int,
    model: str,
    reasoning_effort: str,
    service_tier: str,
    timeout: int,
    log_callback: Callable[[str], None] | None,
) -> dict[int, list[dict]]:
    """페이지들을 배치 순차로 alignment (multi-PDF 지원).

    반환: `{page_idx: [{"source","start_line","end_line"}, ...]}`
    page_idx 는 global index.
    """
    num_pages = len(slides_data.get("pages", []))
    if num_pages == 0:
        raise CodexRunError("slides_data.pages가 비어있음")

    batches = batch_ranges(num_pages, batch_size, overlap)
    _emit(log_callback, f"[align] {num_pages}페이지 → {len(batches)}배치")

    # Codex inputs: 모든 PDF 를 원본 파일명 그대로 전달
    pdf_inputs: dict[str, Path] = {}
    for pdf_path in pdf_paths:
        pdf_inputs[pdf_path.name] = pdf_path

    # 동적 schema: source 필드를 numbered_txts 화이트리스트로 강제
    allowed_sources = list(numbered_txts.keys())
    schema = build_alignment_schema(allowed_sources)
    allowed_set = set(allowed_sources)

    all_assignments: dict[int, list[dict]] = {}

    for i, (bstart, bend) in enumerate(batches, start=1):
        ckpt = batch_ckpt_dir / f"batch_{i:02d}.json"
        if ckpt.exists():
            batch_result = json.loads(ckpt.read_text(encoding="utf-8"))
            _emit(log_callback, f"[align] batch {i}/{len(batches)} checkpoint 로드 (pages {bstart}-{bend})")
        else:
            overlap_start = bstart if (i > 1 and bstart in all_assignments) else None
            prompt = build_batch_prompt(
                slides_data=slides_data,
                numbered_txts=numbered_txts,
                prior_assignments=all_assignments,
                target_start=bstart,
                target_end=bend,
                overlap_start=overlap_start,
            )
            _emit(
                log_callback,
                f"[align] batch {i}/{len(batches)} — pages {bstart}-{bend} 실행 중...",
            )
            result = run_codex_task(
                prompt=prompt,
                inputs=pdf_inputs,
                expected_outputs=["assignments.json"],
                output_schema=schema,
                model=model,
                reasoning_effort=reasoning_effort,
                service_tier=service_tier,
                timeout=timeout,
            )
            raw = result.get("assignments.json", b"").decode("utf-8")
            try:
                batch_result = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CodexRunError(
                    f"[align] batch {i} assignments.json 파싱 실패: {exc}\n"
                    f"내용: {raw[:300]}"
                ) from exc
            # source 화이트리스트 validation + auto-remap
            _validate_assignments_sources(
                batch_result.get("assignments", []),
                allowed_set,
                f"[align] batch {i}",
                log_callback,
            )
            ckpt.write_text(
                json.dumps(batch_result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # 병합: assignments는 [{"page", "ranges"}, ...] 배열
        raw_assigns = batch_result.get("assignments", [])
        if not isinstance(raw_assigns, list):
            _emit(
                log_callback,
                f"[align] 경고: batch {i} assignments가 array가 아님 (type={type(raw_assigns).__name__})",
            )
            raw_assigns = []
        for item in raw_assigns:
            if not isinstance(item, dict):
                continue
            page = item.get("page")
            ranges = item.get("ranges")
            if not isinstance(page, int) or not isinstance(ranges, list):
                _emit(log_callback, f"[align] 경고: 잘못된 entry 형식: {item!r}")
                continue
            all_assignments[page] = ranges
        _emit(
            log_callback,
            f"[align] batch {i}/{len(batches)} 완료 — 누적 {len(all_assignments)}페이지 배정",
        )

    return all_assignments


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
    """assignments array 의 각 range source 가 화이트리스트에 있는지 검증.

    스키마 enum 이 1차 방어선이지만, fallback (inline 답변 복구 등) 경로로 들어온
    결과에 대비해 Python 쪽에서도 확인.

    - 허용되는 값 → 통과
    - 허용되지 않고 `allowed_sources` 크기 1 → 그 하나로 auto-remap + 경고
    - 허용되지 않고 크기 ≥ 2 → `CodexRunError`
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
