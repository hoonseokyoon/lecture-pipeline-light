"""Tier 1 최종 선택 래퍼 (Gemini Flash 매칭기)."""

from __future__ import annotations

from typing import Callable

from codex_runner import CodexRunError

from _lib.gemini_embed import embed_text, match_final
from _lib.retrieve import top_k_by_cosine
from _lib.schema import AssetMatch

LogCb = Callable[[str], None]


def _emit(log, msg):
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


def match_figure_for_slide(
    *,
    slide_title: str,
    slide_bullets: list[str],
    figure_hint: str,
    asset_embeddings,
    asset_names: list[str],
    captions: dict[str, str],
    top_k: int,
    max_refs: int,
    embed_output_dim: int = 1536,
    log_cb: LogCb | None = None,
) -> AssetMatch:
    """Tier 1: hint → query embed → cosine top-K → Flash 최종 선택.

    반환이 `AssetMatch` 이므로 호출자가 confidence threshold 로 Tier 2 진입 판단.
    """
    # 1. query embed (slide 컨텍스트까지 살짝 섞어 retrieval 품질 향상)
    query_text = figure_hint
    if slide_title:
        query_text = f"{figure_hint} (slide context: {slide_title})"

    try:
        q_vec = embed_text(
            text=query_text,
            task_type="RETRIEVAL_QUERY",
            output_dim=embed_output_dim,
        )
    except Exception as exc:
        raise CodexRunError(f"query embedding 실패: {exc}") from exc

    # 2. cosine top-K
    candidates = top_k_by_cosine(
        query=q_vec,
        asset_embeddings=asset_embeddings,
        asset_names=asset_names,
        captions=captions,
        k=top_k,
    )
    if not candidates:
        return AssetMatch(matches=[], confidence=0.0, reasoning="no candidates")

    _emit(
        log_cb,
        f"[match] top-{len(candidates)}: "
        + ", ".join(f"{c['filename']}({c['score']:.2f})" for c in candidates[:3]),
    )

    # 3. Gemini Flash 최종 선택
    try:
        result = match_final(
            response_model=AssetMatch,
            slide_title=slide_title,
            slide_bullets=slide_bullets,
            figure_hint=figure_hint,
            candidates=candidates,
            max_refs=max_refs,
        )
    except Exception as exc:
        # 폴백: top-1 을 confidence 로 그대로 반환
        _emit(log_cb, f"[match] Flash 실패, top-1 폴백: {exc}")
        top = candidates[0]
        return AssetMatch(
            matches=[top["filename"]] if top["score"] >= 0.5 else [],
            confidence=float(top["score"]),
            reasoning=f"flash fallback (score={top['score']:.2f})",
        )

    # Flash 가 엉뚱한 파일명을 냈을 수 있어 검증
    valid_names = set(asset_names)
    filtered = [m for m in result.matches if m in valid_names]
    if len(filtered) != len(result.matches):
        dropped = [m for m in result.matches if m not in valid_names]
        _emit(log_cb, f"[match] 무효 파일명 제거: {dropped}")

    return AssetMatch(
        matches=filtered[:max_refs],
        confidence=result.confidence,
        reasoning=result.reasoning,
    )
