"""Step 5b: 전체 페이지 polish (단일 Codex 호출, 파일 편집 기반).

Step 5 compose가 페이지별로 병렬 생성한 결과를 Codex 한 세션에 전부 입력으로
주고, 전체 강의 흐름을 고려한 polish를 지시. 출력은 원본과 동일한 파일명의
페이지별 markdown.

- 수정 필요한 페이지 → 편집한 결과를 `outputs/page_NNN.md`에 저장
- 수정 불필요한 페이지 → **원본을 복사-붙여넣기**하여 같은 경로에 저장 (빠뜨리면 실패)
- 전체 호출 실패 시 원본 page_notes 그대로 반환 (pipeline 계속 진행)
- 일부 페이지 누락 시 해당 페이지만 원본 유지
- Cache: polish_cache_dir에 페이지별로 저장; 재실행 시 전부 있으면 skip.
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


def _build_polish_prompt(
    num_pages: int,
    first_idx: int,
    last_idx: int,
    lecture_summary: dict,
) -> str:
    lines: list[str] = []
    lines.append("# 작업: 강의 노트 페이지별 polish (전체 흐름 조정)")
    lines.append("")
    lines.append(
        f"`inputs/page_{first_idx:03d}.md` ~ `page_{last_idx:03d}.md`에 Step 5가 "
        f"생성한 **{num_pages}개 페이지 분량**의 강의 노트 섹션이 있습니다. "
        f"각 페이지는 독립적으로 생성돼 인접 페이지 간 연결성이 부족할 수 있습니다."
    )
    lines.append("")
    lines.append("## 전체 절차")
    lines.append("")
    lines.append(
        f"1. `inputs/page_{first_idx:03d}.md` 부터 `page_{last_idx:03d}.md` 까지 "
        "**순서대로 모두 읽어** 전체 강의 흐름을 파악하세요."
    )
    lines.append(
        "2. `inputs/_lecture_summary.json` 의 `overall_theme`, `key_mechanisms`, "
        "`page_importance` 를 참고해 각 페이지의 강의 전체 내 위치·중요도를 인지하세요."
    )
    lines.append(
        "3. 각 페이지에 대해 polish 필요 여부를 판단한 뒤, 다음 중 하나를 "
        "`outputs/page_NNN.md` 에 저장:"
    )
    lines.append("")
    lines.append(
        "   - **수정 필요**: 원본을 아래 원칙으로 다듬은 결과를 저장"
    )
    lines.append(
        "   - **수정 불필요**: `inputs/page_NNN.md` 내용을 **그대로 복사-붙여넣기** "
        "하여 `outputs/page_NNN.md` 에 저장 (원본과 byte 단위 동일해도 OK)"
    )
    lines.append("")
    lines.append("## Polish 원칙 (이것만 수정)")
    lines.append("")
    lines.append(
        "- **인접 페이지와 중복되는 배경 설명 축소**: 같은 개념·인물·실험을 여러 "
        "페이지가 반복 도입하고 있으면, 첫 등장 이후의 중복 설명을 간결화 "
        "(단, 해당 슬라이드의 고유 정보는 보존)."
    )
    lines.append(
        "- **자연스러운 전환 언어 추가**: \"앞 슬라이드에서 본 X를 바탕으로\", "
        "\"여기서 이어지는 질문은\", \"이제 ... 를 살펴보자\" 같은 연결 문구. "
        "단, 모든 페이지에 억지로 넣지 말고 **실제로 자연스러운 곳에만**."
    )
    lines.append(
        "- **어색한 한국어 다듬기**: 문장 흐름이 매끄럽지 않은 부분만 교정."
    )
    lines.append("")
    lines.append("## 절대 건드리지 말 것 (구조 보존)")
    lines.append("")
    lines.append(
        "- **`> ` 로 시작하는 모든 라인** — 강의 발화 blockquote 는 Python 이 "
        "deterministic 하게 박은 verbatim 인용. 한 글자도 바꾸지 말고 삭제도 금지."
    )
    lines.append(
        "- **`### Slide` 로 시작하는 헤더 라인** — 슬라이드 번호와 제목은 고정."
    )
    lines.append(
        "- **`**핵심 용어**:` 섹션의 불릿 라인** — 용어/정의는 schema 로 검증된 값."
    )
    lines.append(
        "- **사실/정의/숫자/고유명사**: `reverse transcriptase`, `Rous sarcoma virus`, "
        "`Y527` 같은 학술 용어와 사실 관계는 변경 금지."
    )
    lines.append("")
    lines.append("## 자유롭게 수정 가능 (`**정리**:` 섹션 본문)")
    lines.append("")
    lines.append(
        "- 인접 페이지와 자연스러운 전환 문구 추가 (\"앞 슬라이드의 X 위에서\", "
        "\"이는 곧 다음 슬라이드의 Y 로 이어진다\" 등)"
    )
    lines.append(
        "- 강의 흐름 맥락 보강 — 단, 녹취·슬라이드에 근거가 있는 내용에 한정 "
        "(완전 창작은 금지하되 합리적 연결은 허용)"
    )
    lines.append(
        "- 인접 페이지와 중복되는 배경 설명을 짧게 trim 하거나 한 문장으로 압축"
    )
    lines.append(
        "- 어색한 한국어 다듬기, 가독성 개선을 위한 문장 재구성"
    )
    lines.append(
        "- 필요시 한 문장 정도의 추가 해설은 OK (이 슬라이드를 처음 보는 사람이 "
        "이해할 수 있게)"
    )
    lines.append("")
    lines.append(
        "**판단 기준**: 의대생/학부생이 노트만 보고 강의 흐름을 따라갈 수 있게 "
        "만드는 것이 목표. 사실 관계는 보존하되, 가독성과 흐름은 적극적으로 개선."
    )
    lines.append("")
    lines.append("## 출력 (엄격)")
    lines.append("")
    lines.append(
        f"- `outputs/page_{first_idx:03d}.md` ~ `outputs/page_{last_idx:03d}.md` "
        f"**모두** 저장. 총 **{num_pages}개 파일**이 `outputs/` 에 있어야 함."
    )
    lines.append(
        "- **한 개라도 누락되면 실패로 처리됨**. 수정 불필요한 페이지도 **반드시 "
        "원본 내용 그대로 복사해서 저장**."
    )
    lines.append(
        "- 파일 내용은 원본 markdown 그대로 (프론트매터/메타데이터 없이)."
    )
    lines.append("- 언어는 원본(한국어) 그대로 유지."
    )
    lines.append("")
    lines.append("## 강의 컨텍스트 요약 (참고)")
    lines.append("")
    theme = lecture_summary.get("overall_theme", "")
    if theme:
        lines.append(f"- **주제**: {theme}")
    mechs = lecture_summary.get("key_mechanisms", [])
    if mechs:
        lines.append("- **핵심 메커니즘**:")
        for m in mechs:
            lines.append(f"  - {m}")
    # importance 요약 (페이지별 level만 간단히)
    imp = lecture_summary.get("page_importance", [])
    imp_items = [
        item for item in imp
        if isinstance(item, dict) and item.get("level") == "important"
    ]
    if imp_items:
        lines.append("- **중요 페이지 (깊이 보존 우선)**:")
        for item in imp_items:
            p = item.get("page")
            r = item.get("reason", "")
            lines.append(f"  - Page {p}: {r}")
    lines.append("")
    lines.append(
        "위 요약은 polish 판단용 참고일 뿐이며, "
        f"`inputs/_lecture_summary.json` 에 전체 데이터가 있음."
    )
    return "\n".join(lines) + "\n"


def polish_pages(
    page_notes: dict[int, str],
    slides_data: dict,
    lecture_summary: dict,
    polish_cache_dir: Path,
    model: str,
    reasoning_effort: str,
    timeout: int,
    log_callback: Callable[[str], None] | None = None,
) -> dict[int, str]:
    """전체 페이지를 단일 Codex 호출로 polish.

    파일 편집 기반: 원본 page 파일을 inputs/로 전달, Codex가 outputs/로 저장.

    Returns:
        polished {page_idx: markdown}. 전체 실패 시 원본 page_notes 그대로 반환.
    """
    if not page_notes:
        _emit(log_callback, "[step5b] 페이지 없음 — polish skip")
        return {}

    num_pages = len(page_notes)
    sorted_idxs = sorted(page_notes.keys())
    first_idx = sorted_idxs[0]
    last_idx = sorted_idxs[-1]

    # Cache check: 전부 있으면 load
    polish_cache_dir.mkdir(parents=True, exist_ok=True)
    cached: dict[int, str] = {}
    for idx in sorted_idxs:
        ckpt = polish_cache_dir / f"page_{idx:03d}.md"
        if ckpt.exists():
            cached[idx] = ckpt.read_text(encoding="utf-8")
    if len(cached) == num_pages:
        _emit(
            log_callback,
            f"[step5b] cache hit — {num_pages}페이지 전부 load",
        )
        return cached

    # 기존 캐시 부분 사용 금지 (단일 호출이 전체 파일을 일관 처리해야 함).
    # 누락이 하나라도 있으면 전체 재호출.
    _emit(
        log_callback,
        f"[step5b] polish 호출 준비 — {num_pages}페이지 입력 "
        f"(pages {first_idx:03d}~{last_idx:03d})",
    )

    # inputs 구성: 각 페이지를 파일로 + context
    inputs: dict[str, str] = {}
    for idx in sorted_idxs:
        inputs[f"page_{idx:03d}.md"] = page_notes[idx]
    inputs["_lecture_summary.json"] = json.dumps(
        lecture_summary, ensure_ascii=False, indent=2
    )
    inputs["_slides.json"] = json.dumps(
        slides_data, ensure_ascii=False, indent=2
    )

    expected_outputs = [f"page_{idx:03d}.md" for idx in sorted_idxs]

    prompt = _build_polish_prompt(
        num_pages=num_pages,
        first_idx=first_idx,
        last_idx=last_idx,
        lecture_summary=lecture_summary,
    )

    try:
        result = run_codex_task(
            prompt=prompt,
            inputs=inputs,
            expected_outputs=expected_outputs,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout=timeout,
        )
    except CodexRunError as exc:
        _emit(
            log_callback,
            f"[step5b] polish 호출 실패, 원본 page_notes 유지: {exc}",
        )
        return dict(page_notes)

    # 결과 수집 + 캐시 저장
    polished: dict[int, str] = {}
    changed = 0
    missing = 0
    for idx in sorted_idxs:
        key = f"page_{idx:03d}.md"
        raw = result.get(key, b"")
        if not raw:
            _emit(log_callback, f"[step5b] page {idx:03d} 누락 — 원본 유지")
            polished[idx] = page_notes[idx]
            missing += 1
            # 캐시 저장은 원본으로 (재실행 시 load 가능)
            (polish_cache_dir / key).write_text(
                page_notes[idx], encoding="utf-8",
            )
            continue
        try:
            text = raw.decode("utf-8")
        except Exception:
            text = raw.decode("utf-8", errors="replace")
        if not text.strip():
            _emit(log_callback, f"[step5b] page {idx:03d} 빈 응답 — 원본 유지")
            polished[idx] = page_notes[idx]
            missing += 1
            (polish_cache_dir / key).write_text(
                page_notes[idx], encoding="utf-8",
            )
            continue
        polished[idx] = text
        (polish_cache_dir / key).write_text(text, encoding="utf-8")
        if text.strip() != page_notes[idx].strip():
            changed += 1

    _emit(
        log_callback,
        f"[step5b] 완료 — {len(polished)}페이지 반영 "
        f"(변경 {changed}, 원본 유지 {num_pages - changed - missing}, "
        f"누락 {missing})",
    )
    return polished
