"""Gemini Flash 기반 per-paper 구조화 요약.

출력 JSON schema:
{
  "title": "...",
  "paper_id": "...",
  "problem": "...",
  "method": "...",
  "findings": ["...", ...],
  "limitations": ["...", ...],
  "relevance_to_rfi": "...",
  "relevance_score": 0.0~1.0,
  "key_quotes": [{"text": "...", "reason": "..."}],
  "tags": ["..."]
}
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, current_cancel_event, load_skill

logger = logging.getLogger("lecture_pipeline.lit_summarize")

LogCb = Callable[[str], None]

_JSON_INSTRUCTION = """출력은 반드시 아래 JSON schema 에 맞는 유효한 JSON 객체
한 개만 반환하세요. 설명 텍스트나 코드블록 마커 없이 JSON 만. 한국어로 작성.

{
  "title": "논문 제목 (원문 또는 번역)",
  "problem": "논문이 다루는 문제 / 질문 (2~3문장)",
  "method": "핵심 방법론 (3~5문장, 수식/알고리즘 핵심 용어 포함)",
  "findings": ["주요 발견 1", "주요 발견 2", "..."],
  "limitations": ["저자가 명시한 한계 또는 평가자 관점의 한계"],
  "relevance_to_rfi": "현재 RFI 질문에 어떻게 관련되는지 (3~5문장)",
  "relevance_score": 0.0,
  "key_quotes": [
    {"text": "원문 인용 (짧게)", "reason": "왜 중요한지"}
  ],
  "key_figures": [
    {"ref": "원문 Markdown 이미지 참조 (예: ![Figure 2](...))",
     "caption": "그림/표가 무엇을 보여주는지",
     "why_important": "이 그림이 왜 핵심인지"}
  ],
  "abstract_only": false,
  "tags": ["주제태그1", "주제태그2"]
}

relevance_score 는 0 (무관) ~ 1 (정면 답변) 실수.
key_quotes 는 최대 5개, 원문을 그대로 (번역하지 말 것).
key_figures 는 본문 마크다운에서 `![](assets/...)` 형태로 나타나는 것 중 핵심
2~4개만. ref 필드는 원문 그대로 — 경로 변형 금지 (리뷰 작성 시 상대경로 재계산).
abstract_only 는 전체 본문이 아닌 초록만 보고 요약한 경우 true.
"""


def _emit(log: LogCb | None, msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


def _is_cancelled() -> bool:
    ev = current_cancel_event.get()
    return ev is not None and ev.is_set()


def _raise_if_cancelled() -> None:
    if _is_cancelled():
        raise CodexRunError("lit_summarize: 사용자 취소")


def _find_rfi_context() -> tuple[str, str]:
    """RFI + PIR 텍스트를 맥락으로 구성.

    우선순위:
    1. env LIT_RFI_FILE / LIT_PIR_FILE
    2. cwd 와 그 부모에서 REQUEST_FOR_INFORMATION.md / PRIORITY_OF_INTELLIGENCE.md
    """
    def _read(path: Path | None) -> str:
        if path and path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                return ""
        return ""

    rfi_env = os.environ.get("LIT_RFI_FILE")
    pir_env = os.environ.get("LIT_PIR_FILE")
    rfi_text = _read(Path(rfi_env)) if rfi_env else ""
    pir_text = _read(Path(pir_env)) if pir_env else ""

    if not rfi_text:
        for d in [Path.cwd(), *Path.cwd().parents]:
            cand = d / "REQUEST_FOR_INFORMATION.md"
            if cand.exists():
                rfi_text = _read(cand)
                break
    if not pir_text:
        for d in [Path.cwd(), *Path.cwd().parents]:
            cand = d / "PRIORITY_OF_INTELLIGENCE.md"
            if cand.exists():
                pir_text = _read(cand)
                break
    return rfi_text, pir_text


def _build_prompt(
    doc_text: str,
    rfi_text: str,
    pir_text: str,
    max_chars: int,
) -> str:
    if len(doc_text) > max_chars:
        head = doc_text[: max_chars // 2]
        tail = doc_text[-max_chars // 2:]
        doc_text = head + "\n\n[...중략 (길이 제한)...]\n\n" + tail

    parts = [_JSON_INSTRUCTION]
    if pir_text:
        parts.append("\n## 프로젝트 PIR (전략 맥락)\n" + pir_text[:4000])
    if rfi_text:
        parts.append("\n## 현재 RFI (답할 질문)\n" + rfi_text[:6000])
    parts.append("\n## 논문 본문 (Markdown)\n" + doc_text)
    return "\n".join(parts)


def _call_gemini(prompt: str, model: str, timeout: int) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise CodexRunError("GEMINI_API_KEY 환경변수 미설정")

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise CodexRunError(
            "google-genai 패키지 없음. `pip install google-genai`"
        ) from exc

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=timeout * 1000),
    )
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    return resp.text or ""


def _parse_json_lenient(text: str) -> dict:
    """Gemini 가 코드블록·프리앰블을 섞어도 JSON 추출."""
    text = text.strip()
    # ```json ... ``` 제거
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text)
    if m:
        text = m.group(1)
    # 첫 `{` 부터 마지막 `}` 까지
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CodexRunError(
            f"Gemini 응답 JSON 파싱 실패: {exc}\n앞부분: {text[:300]}"
        )


def _to_markdown(summary: dict, source_stem: str) -> str:
    lines = [
        f"# {summary.get('title') or source_stem}",
        "",
        f"_source: `{source_stem}.md` · generated: "
        f"{datetime.now(timezone.utc).isoformat()}_",
        "",
        "## Problem",
        summary.get("problem", "").strip() or "_n/a_",
        "",
        "## Method",
        summary.get("method", "").strip() or "_n/a_",
        "",
        "## Findings",
    ]
    for f in summary.get("findings") or []:
        lines.append(f"- {f}")
    if not summary.get("findings"):
        lines.append("_n/a_")

    lines += ["", "## Limitations"]
    for f in summary.get("limitations") or []:
        lines.append(f"- {f}")
    if not summary.get("limitations"):
        lines.append("_n/a_")

    lines += [
        "",
        "## Relevance to RFI",
        f"**Score**: {summary.get('relevance_score', 'n/a')}",
        "",
        summary.get("relevance_to_rfi", "").strip() or "_n/a_",
        "",
        "## Key Quotes",
    ]
    for q in summary.get("key_quotes") or []:
        txt = (q.get("text") or "").strip()
        reason = (q.get("reason") or "").strip()
        lines.append(f"> {txt}")
        if reason:
            lines.append(f"_({reason})_")
        lines.append("")

    figures = summary.get("key_figures") or []
    if figures:
        lines += ["", "## Key Figures"]
        for fg in figures:
            ref = (fg.get("ref") or "").strip()
            caption = (fg.get("caption") or "").strip()
            why = (fg.get("why_important") or "").strip()
            if ref:
                lines.append(ref)
            if caption:
                lines.append(f"_Caption_: {caption}")
            if why:
                lines.append(f"_Why_: {why}")
            lines.append("")
        lines.append(
            "_(참고) ref 의 상대경로는 이 summary 파일 기준이 아니라 원본 "
            "`extracted/<slug>/doc.md` 기준입니다. 리뷰에서 재인용 시 경로 "
            "다시 계산하세요._"
        )

    if summary.get("abstract_only"):
        lines += ["", "> ⚠️ **Abstract-only** — 전체 본문 미획득, 초록 기반 요약."]

    tags = summary.get("tags") or []
    if tags:
        lines += ["", "## Tags", " ".join(f"`{t}`" for t in tags)]

    return "\n".join(lines) + "\n"


def _candidate_to_stub_markdown(cand: dict) -> str:
    """candidate JSON (lit_search 출력 1개 항목) → abstract-only 의사 Markdown."""
    authors = cand.get("authors") or []
    if isinstance(authors, list):
        authors_str = ", ".join(authors)
    else:
        authors_str = str(authors)
    parts = [
        f"# {cand.get('title') or '(untitled)'}",
        "",
        f"**Authors**: {authors_str or '-'}",
        f"**Year**: {cand.get('year') or '-'}",
        f"**Venue**: {cand.get('venue') or '-'}",
    ]
    if cand.get("doi"):
        parts.append(f"**DOI**: {cand['doi']}")
    if cand.get("arxiv_id"):
        parts.append(f"**arXiv**: {cand['arxiv_id']}")
    parts += [
        "",
        "## Abstract (only)",
        cand.get("abstract") or "_(abstract 없음)_",
        "",
        "> ⚠️ 전체 본문 미획득. 초록만 기반으로 요약됨.",
    ]
    return "\n".join(parts)


def run_lit_summarize(
    skill_dir: Path,
    input_paths: list[Path],
    log_callback: LogCb | None = None,
) -> dict[str, bytes]:
    log = log_callback or (lambda _m: None)

    candidate_inputs = [p for p in input_paths if p.suffix.lower() == ".json"]
    md_inputs = [p for p in input_paths if p.suffix.lower() == ".md"]

    if not md_inputs and not candidate_inputs:
        raise CodexRunError(
            "lit_summarize: .md 또는 candidate .json 필요"
        )

    abstract_only = False
    if md_inputs:
        src = md_inputs[0]
        _emit(log, f"[lit_summarize] 시작 (full text): {src.name}")
        try:
            doc_text = src.read_text(encoding="utf-8")
        except OSError as exc:
            raise CodexRunError(f"lit_summarize: 파일 읽기 실패: {exc}")
    else:
        src = candidate_inputs[0]
        _emit(log, f"[lit_summarize] 시작 (abstract-only): {src.name}")
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexRunError(f"lit_summarize: candidate JSON 파싱 실패: {exc}")
        # 단일 candidate 또는 {candidates:[...]} 중 하나 허용 — 전자 권장
        if "candidates" in data and isinstance(data["candidates"], list):
            if not data["candidates"]:
                raise CodexRunError("lit_summarize: candidates 비어있음")
            cand = data["candidates"][0]
        else:
            cand = data
        if not cand.get("abstract"):
            raise CodexRunError(
                "lit_summarize: abstract 없음 — 요약 생성 불가 "
                "(수동 noto 필요)"
            )
        doc_text = _candidate_to_stub_markdown(cand)
        abstract_only = True

    cfg = load_skill(skill_dir).config
    model = cfg.get("gemini_model", "gemini-flash-latest")
    timeout = int(cfg.get("timeout", 180))
    max_chars = int(cfg.get("max_input_chars", 180000))

    rfi_text, pir_text = _find_rfi_context()
    if rfi_text:
        _emit(log, "[lit_summarize] RFI 맥락 로드됨")
    if pir_text:
        _emit(log, "[lit_summarize] PIR 맥락 로드됨")

    prompt = _build_prompt(doc_text, rfi_text, pir_text, max_chars)

    _raise_if_cancelled()
    _emit(log, f"[lit_summarize] Gemini 호출 (model={model})")
    text = _call_gemini(prompt, model, timeout)
    summary = _parse_json_lenient(text)

    # paper_id 기본값 = src 파일 stem
    summary.setdefault("paper_id", src.stem)
    if abstract_only:
        summary["abstract_only"] = True

    # 산출
    json_bytes = json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")
    md_bytes = _to_markdown(summary, src.stem).encode("utf-8")

    _emit(
        log,
        f"[lit_summarize] 완료 — relevance={summary.get('relevance_score')}"
        f" tags={summary.get('tags')}",
    )
    return {
        "summary.json": json_bytes,
        "summary.md": md_bytes,
    }
