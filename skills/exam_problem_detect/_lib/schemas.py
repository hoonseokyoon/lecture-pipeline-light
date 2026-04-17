"""exam_problem_detect의 JSON 스키마.

OpenAI strict mode 관례 (additionalProperties: false, 모든 prop required).
Gemini에 보낼 땐 gemini_backend._sanitize_schema_for_gemini가 자동 정리.
"""

from __future__ import annotations


# ───────── 페이지당 multi-box 탐지 결과 ─────────
PAGE_MULTIBOX_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "has_problems": {
            "type": "boolean",
            "description": (
                "이 페이지에 독립 문제/소문제/발문/보기가 하나라도 있으면 true. "
                "표지, 목차, 완전 빈 페이지면 false (problems는 빈 배열)."
            ),
        },
        "problems": {
            "type": "array",
            "description": (
                "페이지 내 독립 읽기 단위들의 bounding box. "
                "좌표는 정규화 [0, 1000] float. x1<x2, y1<y2. "
                "같은 대문제의 발문/보기/소문제들도 각각 별개 박스로 분리."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "x1": {"type": "number"},
                    "y1": {"type": "number"},
                    "x2": {"type": "number"},
                    "y2": {"type": "number"},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "main_prompt",
                            "shared_passage",
                            "sub_problem",
                            "figure_caption",
                            "choice_block",
                            "other",
                        ],
                        "description": (
                            "main_prompt=대문제 발문, shared_passage=공유 지문, "
                            "sub_problem=소문제/단독 문제, figure_caption=그림/도표 설명, "
                            "choice_block=선택지 블록, other=기타"
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": "이 박스가 왜 독립 단위인지 1문장 이유",
                    },
                },
                "required": ["x1", "y1", "x2", "y2", "kind", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["has_problems", "problems"],
    "additionalProperties": False,
}


# ───────── Cluster 대표 선출 (3-후보 중 best) ─────────
# zero_shot_rec의 STAGE0_PICK_SCHEMA와 동일 형태.
PICK_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "best_id": {
            "type": "integer",
            "description": "1, 2, 또는 3 — 이미지에 그려진 후보 번호",
        },
        "reason": {
            "type": "string",
            "description": "선택 근거 1문장",
        },
    },
    "required": ["best_id", "reason"],
    "additionalProperties": False,
}
