"""Pydantic schema for image_generation skill.

FigureSpec: 입력 명세 (JSON 파일 또는 라이브러리 호출 kwarg).
RouteDecision: router (auto mode) 출력.
FigureMeta: 산출물 옆에 저장되는 메타데이터.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class FigureSpec(BaseModel):
    """한 장의 figure 생성 명세."""
    hint: str = Field(min_length=1)
    context: str = ""
    style: str = ""
    format: Literal["png", "tex"] = "png"
    mode: Literal["script", "model", "auto"] = "auto"
    seed: int = 42


class RouteDecision(BaseModel):
    """Router classifier 출력."""
    route: Literal["script", "model"]
    reason: str = ""


class FigureMeta(BaseModel):
    """산출물 옆 meta.json 내용."""
    mode_used: Literal["script", "model"]
    format: Literal["png", "tex"]
    backend: str = ""
    prompt_snapshot: str = ""
    retries: int = 0
    duration_s: float = 0.0
    cache_hit: bool = False
    cache_key: str = ""
