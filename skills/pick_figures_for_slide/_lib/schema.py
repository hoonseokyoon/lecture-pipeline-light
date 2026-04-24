"""pick_figures_for_slide 내부·보고용 Pydantic 모델."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AssetMatch(BaseModel):
    """Tier 1 Gemini Flash 최종 선택 응답."""
    matches: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""


class SlidePickResult(BaseModel):
    """한 슬라이드 처리 결과 (per_slide 리포트 항목)."""
    slide_number: int
    tier: Literal["skip_no_hint", "tier1_match", "tier2_generate", "failed"]
    figure_refs: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    reasoning: str = ""
    duration_s: float = 0.0
    error: str = ""


class PickReport(BaseModel):
    """pick 전체 요약."""
    total_slides: int
    with_figure_hint: int
    tier1_matched: int
    tier2_generated: int
    skipped: int
    failed: int
    per_slide: list[SlidePickResult] = Field(default_factory=list)
    preprocess: dict = Field(default_factory=dict)
