"""compact_lecture_note 확장 단계(step 9~10c)용 JSON schemas.

모든 schema는 OpenAI Structured Outputs strict mode를 따름:
- additionalProperties: false
- 모든 property는 required
- 비지원 키워드(minimum/maxItems/pattern 등) 금지
"""

from copy import deepcopy


# ───────────────────────── Step 9: exam cues ─────────────────────────

EXAM_CUES_SCHEMA = {
    "type": "object",
    "properties": {
        "professor_exam_comments": {
            "type": "array",
            "description": (
                "교수자가 시험/평가/출제/반드시 외울 것 등을 명시적으로 언급한 "
                "발화. 언급이 없으면 빈 배열. 뉘앙스 추정이 아닌 명백한 진술만."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "녹취록 파일 basename",
                    },
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "paraphrase": {
                        "type": "string",
                        "description": "발화 요지 한 줄 요약 (한국어)",
                    },
                    "slide_page": {
                        "type": "integer",
                        "description": (
                            "관련 슬라이드 index (1-based). 특정하기 어려우면 0."
                        ),
                    },
                },
                "required": [
                    "source", "start_line", "end_line", "paraphrase", "slide_page",
                ],
                "additionalProperties": False,
            },
        },
        "page_emphasis": {
            "type": "array",
            "description": (
                "각 슬라이드의 교수자 강조 수준. 모든 슬라이드에 대한 엔트리 필수."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "page": {
                        "type": "integer",
                        "description": "슬라이드 index (1-based)",
                    },
                    "level": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": (
                            "high: 교수가 반복/강조/시험 언급한 슬라이드. "
                            "medium: 일반적으로 설명한 슬라이드. "
                            "low: 빠르게 넘긴 슬라이드."
                        ),
                    },
                    "reason": {"type": "string"},
                },
                "required": ["page", "level", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["professor_exam_comments", "page_emphasis"],
    "additionalProperties": False,
}


def build_exam_cues_schema(allowed_sources: list[str]) -> dict:
    """EXAM_CUES_SCHEMA의 source 필드에 enum 주입."""
    if not allowed_sources:
        raise ValueError("allowed_sources 가 비어 있음")
    schema = deepcopy(EXAM_CUES_SCHEMA)
    enum_values = sorted(set(allowed_sources))
    schema["properties"]["professor_exam_comments"]["items"]["properties"]["source"] = {
        "type": "string",
        "enum": enum_values,
        "description": "녹취록 파일 basename. enum 중 하나.",
    }
    return schema


# ───────────────────────── Step 10: per-page rewrite ─────────────────────────

COMPACT_PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "narrative_markdown": {
            "type": "string",
            "description": (
                "'처음 접하는 독자에게 가르쳐주듯' 재작성한 페이지 본문. "
                "markdown inline formatting 허용 (**bold**, `code`, _italic_). "
                "단 ##/### 헤더는 금지. 페이지 당 ~10줄 내외를 목표 "
                "(강제 아님 — 자연스러운 길이 우선). 각주는 [^1], [^2] 마커로 삽입."
            ),
        },
        "footnotes": {
            "type": "array",
            "description": (
                "교수자의 인사이트/강조/경험담/시험 언급을 각주로 분리. "
                "narrative_markdown 안의 [^N] 마커와 대응. 없으면 빈 배열."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "marker": {
                        "type": "string",
                        "description": "각주 마커 (예: '^1', '^2'). [^1]에서 ^1 부분만.",
                    },
                    "text": {
                        "type": "string",
                        "description": "각주 본문. 교수자 인사이트·강조 내용.",
                    },
                },
                "required": ["marker", "text"],
                "additionalProperties": False,
            },
        },
        "referenced_terms": {
            "type": "array",
            "description": (
                "narrative에서 언급되거나 이해에 필요한 핵심 용어. "
                "용어집 크로스 참조에 사용. 없으면 빈 배열."
            ),
            "items": {"type": "string"},
        },
    },
    "required": ["narrative_markdown", "footnotes", "referenced_terms"],
    "additionalProperties": False,
}


# ───────────────────────── Step 10b: glossary reorganization ─────────────────────────

COMPACT_GLOSSARY_SCHEMA = {
    "type": "object",
    "properties": {
        "categories": {
            "type": "array",
            "description": (
                "용어들을 주제 단위 카테고리로 묶음. 강의 맥락에 맞게 LLM이 결정. "
                "예: '기본 개념', '핵심 메커니즘', '세부 용어', '등장 인물·연도', "
                "'약어·기호'. 3~7개 카테고리 권장."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "카테고리 이름",
                    },
                    "description": {
                        "type": "string",
                        "description": "이 카테고리가 다루는 내용 한 줄 설명",
                    },
                    "terms": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "term": {"type": "string"},
                                "definition": {
                                    "type": "string",
                                    "description": "한 줄 정의. 원 glossary에 근거.",
                                },
                                "mentioned_on_slides": {
                                    "type": "array",
                                    "description": (
                                        "이 용어가 언급된 슬라이드 index (1-based). "
                                        "알 수 없으면 빈 배열."
                                    ),
                                    "items": {"type": "integer"},
                                },
                            },
                            "required": [
                                "term", "definition", "mentioned_on_slides",
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "description", "terms"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["categories"],
    "additionalProperties": False,
}


# ───────────────────────── Step 10c: pre-exam summary ─────────────────────────

COMPACT_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "professor_exam_notes_md": {
            "type": "string",
            "description": (
                "교수자의 시험 관련 코멘트를 markdown 섹션으로 정리. "
                "professor_exam_comments가 비어있으면 빈 문자열."
            ),
        },
        "compressed_prose_md": {
            "type": "string",
            "description": (
                "시험 직전에 훑어볼 수 있는 압축 요약 prose (한 페이지 치트시트 수준). "
                "강의 전체의 핵심 흐름·메커니즘·결론을 간결히. markdown 허용, 헤더 금지."
            ),
        },
        "summary_table_md": {
            "type": "string",
            "description": (
                "핵심 포인트 markdown table (파이프 문법). 컬럼은 강의 맥락에 맞게 "
                "LLM이 결정 (예: 주제 / 메커니즘 / 키워드 / 시험 관련성)."
            ),
        },
    },
    "required": [
        "professor_exam_notes_md", "compressed_prose_md", "summary_table_md",
    ],
    "additionalProperties": False,
}
