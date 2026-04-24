"""Pydantic schema for md_to_handout outline JSON.

원칙: **스키마는 downstream 처리에서 타입·존재 에러가 나지 않을 정도의 양식만
보장** 한다. 자연어 품질 (종결어미, 문장 형태, 길이 감, 밀도) 은 여기서 강제
하지 않음 — `prompt.txt` 의 자연어 가이드로 Gemini 를 유도하고, 어색한 표현은
다음 단계에서 postprocessing 으로 다듬는다.

구체적으로 유지하는 제약:
- 타입 (str/int/list/Literal)
- Required 필드
- 렌더가 깨지는 공백 필드 차단 (`min_length=1` on title/text/chapter_title 등)
- `slide_number`, `estimated_minutes` 는 양수

제거한 제약 (이전 버전 대비):
- 모든 `max_length` — 분량은 내용에 따라 어느 정도 늘어날 수 있어야 함
- `@field_validator` 로 한국어 종결어미 검사 — false positive 많고, NL 품질은
  Pydantic 의 일이 아님
- `slides.max_length`, `estimated_minutes` 상한
- `ConfigDict(extra="forbid")` — 스키마 drift 허용 (무해한 extra 는 ignore)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


SlideLayout = Literal[
    "bullets", "figure_focus", "equation_focus", "title_only"
]


class SlideBullet(BaseModel):
    """슬라이드 bullet. `text` 가 빈 문자열이면 Beamer itemize 가 깨지므로
    non-empty 만 강제."""

    text: str = Field(min_length=1)


class Slide(BaseModel):
    slide_number: int = Field(ge=1)
    title: str = Field(min_length=1)
    layout: SlideLayout  # enum — 렌더러가 이 값으로 분기하므로 타입 강제 필수

    bullets: list[SlideBullet] = Field(default_factory=list)
    equations: list[str] = Field(default_factory=list)

    # 렌더링 단계에서 Codex 가 채운다. Outline 단계에서는 빈 배열 유지.
    figure_refs: list[str] = Field(default_factory=list)
    # figure 가 필요한 슬라이드에 대한 자연어 힌트 (파일명 아님).
    figure_hint: str = Field(default="")
    # 대본 생성기용 방향성 한 줄.
    speaker_hint: str = Field(default="")


class LectureOutline(BaseModel):
    """한 챕터/강의 단위의 outline."""

    chapter_title: str = Field(min_length=1)
    source_stem: str = Field(default="")  # 오케스트레이터가 후주입
    estimated_minutes: int = Field(ge=1)
    slides: list[Slide] = Field(min_length=1)

    def to_preview_md(self) -> str:
        """사람이 빠르게 훑기 위한 Markdown 프리뷰."""
        lines: list[str] = []
        lines.append(f"# {self.chapter_title}")
        lines.append("")
        lines.append(
            f"_예상 강의 시간: {self.estimated_minutes}분 · "
            f"{len(self.slides)}장 슬라이드_"
        )
        if self.source_stem:
            lines.append(f"_출처: `{self.source_stem}`_")
        lines.append("")
        for slide in self.slides:
            lines.append(f"## Slide {slide.slide_number} — {slide.title}")
            lines.append(f"_layout: `{slide.layout}`_")
            for bullet in slide.bullets:
                lines.append(f"- {bullet.text}")
            for eq in slide.equations:
                lines.append("")
                lines.append(f"$$ {eq} $$")
            for ref in slide.figure_refs:
                lines.append("")
                lines.append(f"![]({ref})")
            if slide.figure_hint:
                lines.append("")
                lines.append(f"> _figure:_ {slide.figure_hint}")
            if slide.speaker_hint:
                lines.append("")
                lines.append(f"> _speak:_ {slide.speaker_hint}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def bullet_stats(self) -> dict[str, float | int]:
        """밀도 체감용 집계 (강제 규칙 아님 — 튜닝 피드백용)."""
        counts = [len(s.bullets) for s in self.slides]
        lengths = [len(b.text) for s in self.slides for b in s.bullets]
        empty = sum(1 for c in counts if c == 0)
        return {
            "slides": len(self.slides),
            "bullets_total": sum(counts),
            "bullets_mean": (sum(counts) / len(counts)) if counts else 0.0,
            "bullet_chars_mean": (
                sum(lengths) / len(lengths) if lengths else 0.0
            ),
            "empty_slides": empty,
        }
