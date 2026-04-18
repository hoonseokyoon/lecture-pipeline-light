"""Gemini 응답 JSON schemas — detect/annotate 전용."""


DETECT_SCHEMA = {
    "type": "object",
    "properties": {
        "objects": {
            "type": "array",
            "description": (
                "페이지 내 탐지된 content 객체들. bbox 는 정규화 좌표 [0, 1000] "
                "(x1, y1, x2, y2), 원점 좌상단. reading order 대로 정렬."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "text", "figure", "table",
                            "equation", "code", "form", "unknown",
                        ],
                    },
                    "bbox": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                    "hint": {
                        "type": "string",
                        "description": "선택적 — 이 객체의 세부 힌트 (예: 'section header', 'display equation').",
                    },
                },
                "required": ["type", "bbox"],
            },
        }
    },
    "required": ["objects"],
}


ANNOTATE_TEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": [
                "text", "figure", "table",
                "equation", "code", "form", "unknown",
            ],
        },
        "content": {
            "type": "string",
            "description": (
                "type 에 맞는 구조화된 텍스트. text: OCR 결과. "
                "equation: LaTeX (순수 수식, $ 없이). "
                "table: HTML (<table>...</table>). "
                "figure: alt-text 설명. code: 코드 원문."
            ),
        },
    },
    "required": ["type", "content"],
}


VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {
            "type": "boolean",
            "description": "렌더 결과가 원본 crop 의 의미·시각 구조를 충분히 재현하면 true.",
        },
        "reason": {
            "type": "string",
            "description": "거부 시 구체적 이유 (무엇이 틀렸는지). 승인 시 한 줄 요약.",
        },
    },
    "required": ["approved", "reason"],
}
