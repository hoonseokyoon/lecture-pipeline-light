"""Step 5c: compact narrative sliding-window polish.

Step 5a compact compose가 페이지별 독립 생성한 narrative를 전체 흐름 관점에서 다듬음.
원리는 step5b(compose polish)와 동일:
- 15페이지 배치, 2페이지 overlap, 순차 worker
- 각 worker는 전체 compact narrative를 inputs/에서 열람 가능
- 이전 worker 결과는 done/에서 참조
- 출력은 polished narrative_markdown (footnotes/referenced_terms는 원본 유지)
"""

import json
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, run_codex_task

from _lib.polish import _compute_batches


def _emit(log_callback: Callable[[str], None] | None, msg: str) -> None:
    if log_callback is None:
        return
    try:
        log_callback(msg)
    except Exception:
        pass


def _build_compact_polish_prompt(
    batch_indices: list[int],
    all_sorted_idxs: list[int],
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
        f"# 작업: compact 강의 노트 polish (Worker {worker_num}/{total_workers})"
    )
    lines.append("")
    lines.append(
        f"당신은 compact 강의 노트의 **page {first}~{last}** ({num}페이지)을 "
        f"polish합니다. 전체 강의는 {len(all_sorted_idxs)}페이지입니다."
    )
    lines.append("")
    lines.append(
        "이 compact 노트는 '처음 접하는 독자에게 가르쳐���듯' 재작성된 "
        "슬라이드별 narrative입니다. 각 페이지는 독립 생성되어 인접 페이지와 "
        "중복·단절이 있을 수 있습니다."
    )
    lines.append("")

    # 강의 메타 컨텍스트
    lines.append("## 강의 메타 ���텍스트")
    lines.append("")
    theme = lecture_summary.get("overall_theme", "")
    if theme:
        lines.append(f"**주제**: {theme}")
    mechs = lecture_summary.get("key_mechanisms", []) or []
    if mechs:
        lines.append("**핵심 메커니즘**:")
        for m in mechs:
            lines.append(f"  - {m}")
    lines.append("")

    # 파일 구조
    lines.append("## 파일 구조")
    lines.append("")
    lines.append(
        f"- `inputs/page_NNN.md` : 전체 {len(all_sorted_idxs)}페이지 compact "
        "narrative 원본. 필요하면 자유롭게 열람."
    )
    if has_prior_outputs:
        lines.append(
            "- `inputs/done/page_NNN.md` : 이전 worker가 polish한 결과. "
            "흐름 연결을 위해 참고."
        )
    lines.append(
        f"- `outputs/page_{first:03d}.md` ~ `outputs/page_{last:03d}.md` : "
        f"polish 결과 저장 위치. **{num}개 파일 모두 필수**."
    )
    lines.append("")

    # Polish 원칙
    lines.append("## Polish 원칙")
    lines.append("")
    lines.append(
        "- **인접 페이지 중복 축소**: 같은 개념을 여러 페이지가 반복 도입하면, "
        "첫 등장 이후 중복 설명을 간결화."
    )
    lines.append(
        "- **자연스러운 전환**: \"앞 슬라이드에서 본 X를 바탕으로\" 같은 "
        "연결 문구를 자연스러운 곳에만."
    )
    lines.append("- **어색한 한국어 교정**: 문장 흐름이 매끄럽지 않은 부분만.")
    lines.append(
        "- **교육적 톤 유지**: '가르쳐주듯'이라는 기조는 그대로. "
        "딱딱한 나열체로 바꾸지 말 것."
    )
    lines.append("")
    lines.append("## 건드리지 말 것")
    lines.append("")
    lines.append(
        "- `[^N]` 각주 마커 — 위치와 번호를 변경하지 말 것 "
        "(대응 footnote와 매핑됨)"
    )
    lines.append("- 사실/정의/숫자/고유명사 — 학술 용어 변경 금지")
    lines.append(
        "- **bold** 처리된 핵심 용어 — 용어집 크로스 참조에 사용됨"
    )
    lines.append("")
    lines.append("## 자유 수정 가능")
    lines.append("")
    lines.append("- 인접 페이지와 전환 문구 추가/��정")
    lines.append("- 중복 배경 설명 trim")
    lines.append("- 어색한 표현 다듬기, 가독성 개선")
    lines.append("- 한 문장 정도의 보충 해설 (근거 있는 내용에 한��)")
    lines.append("")
    lines.append("## 출력 (엄격)")
    lines.append("")
    lines.append(
        f"- `outputs/page_{first:03d}.md` ~ `outputs/page_{last:03d}.md` "
        f"**모두 {num}개** 저장."
    )
    lines.append(
        "- 수정 불필요한 페이지도 원본 내용 그대로 복사. **누락 = 실패**."
    )
    lines.append("- 출력은 narrative markdown만 (JSON 아님). 원본 형식 유지.")
    lines.append("- 언어는 원본(한국어) 유지.")
    return "\n".join(lines) + "\n"


def polish_compact_pages(
    compact_pages: dict[int, dict],
    lecture_summary: dict,
    polish_cache_dir: Path,
    model: str,
    reasoning_effort: str,
    service_tier: str = "default",
    timeout: int = 1200,
    batch_size: int = 15,
    overlap: int = 2,
    log_callback: Callable[[str], None] | None = None,
) -> dict[int, dict]:
    """Sliding-window polish for compact narratives.

    compact_pages: {idx: {narrative_markdown, footnotes, referenced_terms}}
    Returns: same structure with polished narrative_markdown.
    """
    if not compact_pages:
        _emit(log_callback, "[step5c] compact 페이지 없음 — skip")
        return {}

    sorted_idxs = sorted(compact_pages.keys())
    num_pages = len(sorted_idxs)
    polish_cache_dir.mkdir(parents=True, exist_ok=True)

    # 전체 캐시 히트
    cached_all = True
    for idx in sorted_idxs:
        if not (polish_cache_dir / f"page_{idx:03d}.json").exists():
            cached_all = False
            break
    if cached_all:
        _emit(log_callback, f"[step5c] cache hit — {num_pages}페이지 전부 load")
        result: dict[int, dict] = {}
        for idx in sorted_idxs:
            try:
                result[idx] = json.loads(
                    (polish_cache_dir / f"page_{idx:03d}.json").read_text(
                        encoding="utf-8",
                    )
                )
            except Exception:
                result[idx] = compact_pages[idx]
        return result

    batches = _compute_batches(sorted_idxs, batch_size, overlap)
    _emit(
        log_callback,
        f"[step5c] compact {num_pages}페이지 → {len(batches)}배치 "
        f"(batch_size={batch_size}, overlap={overlap})",
    )

    # narrative_markdown만 추출 (polish 대상)
    narratives: dict[int, str] = {
        idx: compact_pages[idx].get("narrative_markdown", "")
        for idx in sorted_idxs
    }

    polished_narratives: dict[int, str] = {}

    for wi, batch in enumerate(batches):
        worker_num = wi + 1
        first_idx = batch[0]
        last_idx = batch[-1]
        marker = polish_cache_dir / f"_worker_{worker_num:02d}_done"

        if marker.exists():
            for idx in batch:
                ckpt = polish_cache_dir / f"page_{idx:03d}.json"
                if ckpt.exists():
                    try:
                        data = json.loads(ckpt.read_text(encoding="utf-8"))
                        polished_narratives[idx] = data.get(
                            "narrative_markdown", narratives.get(idx, ""),
                        )
                    except Exception:
                        polished_narratives[idx] = narratives.get(idx, "")
            _emit(
                log_callback,
                f"[step5c] worker {worker_num}/{len(batches)} cache hit "
                f"(pages {first_idx}-{last_idx})",
            )
            continue

        _emit(
            log_callback,
            f"[step5c] worker {worker_num}/{len(batches)} "
            f"— pages {first_idx}-{last_idx} ({len(batch)}p) 호출 중...",
        )

        # inputs: 전체 narrative + done/ 누적
        inputs: dict[str, str] = {}
        for idx in sorted_idxs:
            inputs[f"page_{idx:03d}.md"] = narratives[idx]
        for idx in sorted(polished_narratives.keys()):
            inputs[f"done/page_{idx:03d}.md"] = polished_narratives[idx]
        inputs["_lecture_summary.json"] = json.dumps(
            lecture_summary, ensure_ascii=False, indent=2,
        )

        expected_outputs = [f"page_{idx:03d}.md" for idx in batch]

        prompt = _build_compact_polish_prompt(
            batch_indices=batch,
            all_sorted_idxs=sorted_idxs,
            lecture_summary=lecture_summary,
            has_prior_outputs=bool(polished_narratives),
            worker_num=worker_num,
            total_workers=len(batches),
        )

        try:
            result_raw = run_codex_task(
                prompt=prompt,
                inputs=inputs,
                expected_outputs=expected_outputs,
                model=model,
                reasoning_effort=reasoning_effort,
                service_tier=service_tier,
                timeout=timeout,
            )
        except CodexRunError as exc:
            _emit(
                log_callback,
                f"[step5c] worker {worker_num} 실패, 원본 유지: {exc}",
            )
            for idx in batch:
                if idx not in polished_narratives:
                    polished_narratives[idx] = narratives.get(idx, "")
                    _save_compact_page(
                        polish_cache_dir, idx, compact_pages[idx],
                        polished_narratives[idx],
                    )
            marker.write_text(f"failed — pages {first_idx}-{last_idx}")
            continue

        changed = 0
        missing = 0
        for idx in batch:
            key = f"page_{idx:03d}.md"
            raw = result_raw.get(key, b"")
            if raw:
                try:
                    text = raw.decode("utf-8")
                except Exception:
                    text = raw.decode("utf-8", errors="replace")
                if not text.strip():
                    text = narratives.get(idx, "")
                    missing += 1
                elif text.strip() != narratives.get(idx, "").strip():
                    changed += 1
            else:
                text = narratives.get(idx, "")
                missing += 1
            polished_narratives[idx] = text
            _save_compact_page(
                polish_cache_dir, idx, compact_pages[idx], text,
            )

        marker.write_text(
            f"pages {first_idx}-{last_idx} — changed={changed} missing={missing}",
            encoding="utf-8",
        )
        _emit(
            log_callback,
            f"[step5c] worker {worker_num}/{len(batches)} 완료 "
            f"(변경 {changed}, 누락→원본 {missing})",
        )

    # 최종 조립: polished narrative + 원본 footnotes/referenced_terms
    final: dict[int, dict] = {}
    for idx in sorted_idxs:
        original = compact_pages[idx]
        final[idx] = {
            "narrative_markdown": polished_narratives.get(
                idx, original.get("narrative_markdown", ""),
            ),
            "footnotes": original.get("footnotes", []),
            "referenced_terms": original.get("referenced_terms", []),
        }

    _emit(log_callback, f"[step5c] 전체 완료 — {num_pages}페이지 compact polish 반영")
    return final


def _save_compact_page(
    cache_dir: Path,
    idx: int,
    original: dict,
    polished_narrative: str,
) -> None:
    """polished narrative + 원본 footnotes/referenced_terms를 JSON으로 저장."""
    data = {
        "narrative_markdown": polished_narrative,
        "footnotes": original.get("footnotes", []),
        "referenced_terms": original.get("referenced_terms", []),
    }
    (cache_dir / f"page_{idx:03d}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
