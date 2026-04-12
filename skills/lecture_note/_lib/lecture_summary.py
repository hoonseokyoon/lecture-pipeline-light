"""Step 1b: 강의 전체 요약 + 페이지 중요도 평가.

Step 5 compose가 각 페이지를 만들 때 전역 context로 쓰이는 요약을 생성.
- overall_theme: 강의의 핵심 주제
- key_mechanisms: 핵심 메커니즘 3~7개
- page_importance: 각 페이지의 {important, normal, transitional} 레벨
"""

import json
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, run_codex_task

from _lib.prompt_utils import (
    build_grouped_slide_summary,
    build_multi_source_rules,
    build_source_context_block,
)
from _lib.schemas import LECTURE_SUMMARY_SCHEMA


def _emit(log_callback: Callable[[str], None] | None, msg: str) -> None:
    if log_callback is None:
        return
    try:
        log_callback(msg)
    except Exception:
        pass


def _build_summary_prompt(
    slides_data: dict,
    numbered_txts: dict[str, dict],
) -> str:
    num_pages = len(slides_data.get("pages", []))
    lines: list[str] = []
    lines.append("# 작업: 강의 전체 요약 + 페이지 중요도 평가")
    lines.append("")
    lines.append(
        "이 강의의 슬라이드 메타데이터와 녹취록을 읽고, 강의 전체의 핵심 주제, "
        "핵심 메커니즘, 각 슬라이드의 상대적 중요도를 판단하세요."
    )
    lines.append("")
    lines.append("## 필드 규칙")
    lines.append("")
    lines.append("- `overall_theme`: 강의 전체가 전달하려는 핵심 메시지. 1~2문장.")
    lines.append(
        "- `key_mechanisms`: 이 강의의 핵심 메커니즘 또는 중요 개념 3~7개. "
        "각 항목은 간결한 구문 (예: \"RNA → DNA 역전사의 분자적 증명\")."
    )
    lines.append(
        f"- `page_importance`: **모든 {num_pages}개 슬라이드에 대한 평가**. "
        f"`page`는 1부터 {num_pages}까지 전부 포함."
    )
    lines.append("")
    lines.append("## 중요도 레벨 정의")
    lines.append("")
    lines.append(
        "- `important`: 강의 전체에서 핵심이거나 개념적으로 까다로운 슬라이드. "
        "나중 compose 단계에서 **깊이 있고 길게** 해설할 대상."
    )
    lines.append(
        "- `transitional`: 표지/질문 던지기/섹션 전환 같은 경유지 페이지. "
        "**짧게 다룰** 대상."
    )
    lines.append("- `normal`: 나머지 일반 페이지 (대다수가 여기 해당).")
    lines.append("")
    lines.append("## 분포 가이드 (중요)")
    lines.append("")
    lines.append(
        "- `important` 는 **전체의 약 20~30%** — 진짜 핵심인 슬라이드만. 남발 금지."
    )
    lines.append(
        "- `transitional` 은 전체의 약 10~20% — 표지·질문·도입·요약 등."
    )
    lines.append("- 나머지 약 50~70% 는 `normal`.")
    lines.append(
        "- 한쪽으로 치우치지 않게. 모든 페이지를 `important`로 찍거나 "
        "모든 페이지를 `normal`로 찍는 것은 실패로 간주."
    )
    lines.append("")

    lines.extend(build_source_context_block(slides_data, numbered_txts))
    lines.extend(build_multi_source_rules(slides_data, numbered_txts))

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
    lines.append("## 출력")
    lines.append("")
    lines.append(
        f"`outputs/summary.json`에 저장. `page_importance`는 정확히 {num_pages}개 "
        "엔트리를 포함해야 함. 예시 구조:"
    )
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps({
        "overall_theme": "센트럴 도그마의 역전(역전사) 발견과 암 유전자 이해의 시작",
        "key_mechanisms": [
            "RNA → DNA 역전사 증명 (RNase 실험)",
            "v-src의 Y527 결실로 인한 상시 활성화",
            "proto-oncogene → oncogene 전환 기전",
        ],
        "page_importance": [
            {"page": 1, "level": "transitional", "reason": "강의 표지"},
            {"page": 11, "level": "important", "reason": "RNA-DNA 가설의 핵심 논리"},
        ],
    }, ensure_ascii=False, indent=2))
    lines.append("```")
    return "\n".join(lines) + "\n"


def generate_lecture_summary(
    slides_data: dict,
    numbered_txts: dict[str, dict],
    model: str,
    reasoning_effort: str,
    timeout: int,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """Step 1b 실행. 실패 시 빈 요약 dict 반환 (pipeline 계속 진행).

    Returns:
        {"overall_theme": str, "key_mechanisms": [str], "page_importance": [dict]}
    """
    _emit(log_callback, "[step1b] lecture_summary 생성 중...")
    prompt = _build_summary_prompt(slides_data, numbered_txts)

    try:
        result = run_codex_task(
            prompt=prompt,
            inputs={"_marker.txt": "lecture_summary"},
            expected_outputs=["summary.json"],
            output_schema=LECTURE_SUMMARY_SCHEMA,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout=timeout,
        )
    except CodexRunError as exc:
        _emit(
            log_callback,
            f"[step1b] 실패, 빈 요약으로 진행: {exc}",
        )
        return _empty_summary(slides_data)

    raw = result.get("summary.json", b"").decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _emit(
            log_callback,
            f"[step1b] summary.json 파싱 실패, 빈 요약으로 진행: {exc}",
        )
        return _empty_summary(slides_data)

    # 분포 검증 + 로그
    importance = data.get("page_importance", [])
    dist = {"important": 0, "normal": 0, "transitional": 0}
    for item in importance:
        lvl = item.get("level") if isinstance(item, dict) else None
        if lvl in dist:
            dist[lvl] += 1
    total = sum(dist.values())
    _emit(
        log_callback,
        f"[step1b] 완료 — {len(data.get('key_mechanisms', []))} mechanisms, "
        f"중요도 분포 important={dist['important']}, normal={dist['normal']}, "
        f"transitional={dist['transitional']} (total={total})",
    )

    # 누락된 페이지는 normal로 fallback
    present_pages = {
        item.get("page") for item in importance
        if isinstance(item, dict) and isinstance(item.get("page"), int)
    }
    all_pages = {
        p.get("index") for p in slides_data.get("pages", [])
        if isinstance(p.get("index"), int)
    }
    missing = all_pages - present_pages
    if missing:
        _emit(
            log_callback,
            f"[step1b] 누락된 {len(missing)}페이지 normal로 채움",
        )
        for p in missing:
            importance.append({
                "page": p,
                "level": "normal",
                "reason": "fallback (누락)",
            })
        data["page_importance"] = importance

    return data


def _empty_summary(slides_data: dict) -> dict:
    pages = slides_data.get("pages", [])
    return {
        "overall_theme": "",
        "key_mechanisms": [],
        "page_importance": [
            {
                "page": p.get("index"),
                "level": "normal",
                "reason": "summary 생략 (fallback)",
            }
            for p in pages
            if isinstance(p.get("index"), int)
        ],
    }
