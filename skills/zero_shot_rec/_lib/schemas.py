"""zero_shot_rec의 stage별 JSON 스키마.

OpenAI Structured Outputs strict mode 규약 (lecture_note/_lib/schemas.py와 동일):
- object는 `additionalProperties: false`
- 모든 property는 `required`
- `minimum/maximum/pattern/enum 외 제약` 금지 → 자연어 description에 기재
"""

from __future__ import annotations


# ───────── Stage 0 pre: target 존재 판정 (voting 대상) ─────────
STAGE0_PRESENCE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "target_present": {
            "type": "boolean",
            "description": (
                "이미지에 지시어에 해당하는 target이 실제로 존재하면 true. "
                "이미지에 target 자체가 없으면 false."
            ),
        },
        "reasoning": {
            "type": "string",
            "description": "판단 근거 1문장 (이미지에서 본 것, 조건과의 매칭 여부)",
        },
    },
    "required": ["target_present", "reasoning"],
    "additionalProperties": False,
}


# ───────── Stage 0a: 후보 박스 top-3 제안 (presence 확정 후 1회) ─────────
STAGE0_PROPOSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "description": (
                "후보 박스 정확히 3개. 좌표는 정규화 [0, 1000] float. "
                "x1<x2, y1<y2."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "description": "1, 2, 또는 3 — 제안 내 고유 번호",
                    },
                    "x1": {"type": "number"},
                    "y1": {"type": "number"},
                    "x2": {"type": "number"},
                    "y2": {"type": "number"},
                    "rationale": {
                        "type": "string",
                        "description": "이 박스가 왜 target에 해당할 수 있는지 1~2문장",
                    },
                },
                "required": ["id", "x1", "y1", "x2", "y2", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["candidates"],
    "additionalProperties": False,
}


# ───────── Stage 0b: 번호 매긴 후보 중 best 선택 ─────────
STAGE0_PICK_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "best_id": {
            "type": "integer",
            "description": "이미지에 그려진 후보 번호 (1, 2, 또는 3)",
        },
        "reason": {
            "type": "string",
            "description": "선택 근거 1~2문장",
        },
    },
    "required": ["best_id", "reason"],
    "additionalProperties": False,
}


# ───────── Stage 1: 박스-대상 containment 판정 ─────────
# Stage 0 pick이 이미 target 존재와 후보 선택을 검증했으므로 "부재(D)" 판정은
# 불가. Stage 1의 유일한 역할은 fit 품질 판정.
STAGE1_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["A", "B", "C", "E"],
            "description": (
                "A=박스가 target에 정확히 맞음. "
                "B=박스 안에 target이 있으나 여백 과다 (축소 필요). "
                "C=target 일부가 박스 밖으로 잘림 (확장 필요). "
                "E=박스 안에 여러 target 후보가 포함되어 모호함. "
                "부재(없음) 판정은 불가 — Stage 0에서 이미 검증됨."
            ),
        },
        "loose_sides": {
            "type": "array",
            "description": (
                "verdict=B일 때 여백이 큰 변들. 그 외는 빈 배열."
            ),
            "items": {
                "type": "string",
                "enum": ["top", "bottom", "left", "right"],
            },
        },
        "clipped_sides": {
            "type": "array",
            "description": (
                "verdict=C일 때 target이 잘리는 변들. 그 외는 빈 배열."
            ),
            "items": {
                "type": "string",
                "enum": ["top", "bottom", "left", "right"],
            },
        },
        "notes": {
            "type": "string",
            "description": "판단 근거 1~2문장. target의 실제 경계 위치를 간단히 서술.",
        },
    },
    "required": ["verdict", "loose_sides", "clipped_sides", "notes"],
    "additionalProperties": False,
}


# ───────── Stage 2: 단일 edge refinement ─────────
def build_stage2_edge_schema(dot_labels: list[str]) -> dict:
    """nearest_point를 허용 라벨 enum으로 강제."""
    if not dot_labels:
        raise ValueError("dot_labels가 비어있음")
    return {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["outward", "same", "inward"],
                "description": (
                    "현재 빨간 edge 선 대비 target의 실제 경계 방향. "
                    "outward=박스 바깥쪽 (확장 필요 — top edge라면 위로, "
                    "bottom edge라면 아래로, left edge라면 왼쪽으로, "
                    "right edge라면 오른쪽으로). "
                    "same=현재 선이 실제 경계와 일치. "
                    "inward=박스 안쪽 (축소 필요)."
                ),
            },
            "region": {
                "type": "string",
                "enum": ["L", "C", "R", "uniform"],
                "description": (
                    "수평 edge는 좌/중/우 3분할. 수직 edge는 상/중/하 3분할로 간주 "
                    "(L=상, C=중, R=하). uniform=어긋남이 균일."
                ),
            },
            "nearest_point": {
                "type": "string",
                "enum": list(dot_labels),
                "description": (
                    "실제 target 경계에 가장 가까운 점의 라벨. "
                    "반드시 enum 목록 중 하나."
                ),
            },
            "confidence": {
                "type": "number",
                "description": "0.0 ~ 1.0 판단 자신도. 낮으면 변경 없음으로 처리.",
            },
            "notes": {
                "type": "string",
                "description": "판단 근거 한 줄 요약.",
            },
        },
        "required": ["direction", "region", "nearest_point", "confidence", "notes"],
        "additionalProperties": False,
    }
