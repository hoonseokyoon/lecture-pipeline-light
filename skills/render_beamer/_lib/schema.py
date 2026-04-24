"""render_beamer 내부 스키마.

outline.with-figures.json 은 md_to_handout 의 `LectureOutline` 과 같은 형태지만
cross-skill import 를 피하기 위해 필요한 부분만 자체 Pydantic 으로 재선언한다.
Downstream 타입 안전성 확보 + 렌더러가 의존하는 최소 필드만 검증.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


SlideLayout = Literal[
    "bullets", "figure_focus", "equation_focus", "title_only"
]


class SlideBullet(BaseModel):
    text: str = Field(min_length=1)


class Slide(BaseModel):
    slide_number: int = Field(ge=1)
    title: str = Field(min_length=1)
    layout: SlideLayout

    bullets: list[SlideBullet] = Field(default_factory=list)
    equations: list[str] = Field(default_factory=list)

    figure_refs: list[str] = Field(default_factory=list)
    figure_hint: str = Field(default="")
    speaker_hint: str = Field(default="")


class LectureOutline(BaseModel):
    chapter_title: str = Field(min_length=1)
    source_stem: str = Field(default="")
    estimated_minutes: int = Field(ge=1)
    slides: list[Slide] = Field(min_length=1)


# ─── Render 리포트 ───────────────────────────────────────────


class LatexError(BaseModel):
    """latexmk 로그에서 추출한 한 에러."""

    line: int | None = None
    message: str
    context: str = ""
    kind: Literal["frame", "preamble", "unknown"]
    frame_index: int | None = None  # 1-based slide_number


class RepairAttempt(BaseModel):
    """Codex repair 한 회 기록."""

    frame_index: int | None = None  # None = preamble repair
    iteration: int
    error_excerpt: str
    fixed: bool
    duration_s: float = 0.0
    note: str = ""


class RenderReport(BaseModel):
    total_frames: int
    compiled_frames: int
    skipped_frames: list[int] = Field(default_factory=list)
    repair_attempts: list[RepairAttempt] = Field(default_factory=list)
    final_status: Literal["success", "partial", "failed"]
    engine: str = ""
    pdf_size_bytes: int = 0
    duration_s: float = 0.0
    missing_figures: list[str] = Field(default_factory=list)
