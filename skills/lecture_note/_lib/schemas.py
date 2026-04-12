"""lecture_note pipeline에서 사용하는 JSON schemas.

OpenAI Structured Outputs strict mode 규격을 따름:
- 모든 object는 `additionalProperties: false`
- 모든 property는 `required`에 포함
- `minimum`, `maxItems`, `pattern` 등 비지원 키워드 사용 금지
- 동적 dict key 금지 → 대신 array of fixed-shape objects

제약 (ex. "최대 2 구간", "page index >= 1")은 프롬프트에서 자연어로 강제.

일부 스키마는 **런타임 팩토리**로 제공 (`build_alignment_schema`,
`build_reconcile_schema`). `source` 필드를 녹취록 파일 basename 화이트리스트로
강제해 LLM hallucination(예: "prompt.txt" 출력) 방지.
"""

from copy import deepcopy


# Step 2: 배치 alignment 결과.
# 동적 dict key 금지로 인해 {page → ranges} dict 대신
# [{page, ranges}] array 구조.
ALIGNMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "description": (
                "target 범위 내 모든 페이지에 대한 라인 구간 배정. "
                "각 엔트리는 한 페이지의 결과."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "page": {
                        "type": "integer",
                        "description": "페이지 번호 (1-based)",
                    },
                    "ranges": {
                        "type": "array",
                        "description": (
                            "이 페이지에 배정된 녹취 라인 구간들. "
                            "직접 관련된 부분만, 가급적 1구간, 최대 2구간. "
                            "배정할 구간이 없으면 빈 배열."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {
                                    "type": "string",
                                    "description": "녹취록 파일 basename (예: lec1.txt)",
                                },
                                "start_line": {
                                    "type": "integer",
                                    "description": "구간 시작 라인 번호 (1-based)",
                                },
                                "end_line": {
                                    "type": "integer",
                                    "description": "구간 끝 라인 번호 (inclusive)",
                                },
                            },
                            "required": ["source", "start_line", "end_line"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["page", "ranges"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["assignments"],
    "additionalProperties": False,
}


# Step 1b: lecture_summary — 강의 전체 요약 + 페이지 중요도.
LECTURE_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_theme": {
            "type": "string",
            "description": "강의 전체가 전달하려는 핵심 주제. 1~2문장.",
        },
        "key_mechanisms": {
            "type": "array",
            "description": (
                "강의가 설명하는 핵심 메커니즘 또는 중요 개념 3~7개. "
                "각 항목은 간결한 구문."
            ),
            "items": {"type": "string"},
        },
        "page_importance": {
            "type": "array",
            "description": (
                "모든 슬라이드에 대한 중요도 평가. 슬라이드 총 개수만큼 엔트리 필수."
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
                        "enum": ["important", "normal", "transitional"],
                        "description": (
                            "important: 강의 핵심/어려운 개념. "
                            "normal: 일반. "
                            "transitional: 표지/질문/도입/전환 경유지."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "해당 레벨로 평가한 한 줄 근거",
                    },
                },
                "required": ["page", "level", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overall_theme", "key_mechanisms", "page_importance"],
    "additionalProperties": False,
}


# Step 3: reconcile의 orphan candidate 분류 결정.
RECONCILE_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "description": (
                "각 orphan candidate에 대한 분류 결정. "
                "입력으로 준 candidate 전부에 대한 엔트리가 있어야 함."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "candidate의 녹취록 파일 basename",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "candidate 시작 라인 (1-based)",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "candidate 끝 라인 (inclusive)",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["merge_to_page", "chatter", "other"],
                        "description": (
                            "merge_to_page: 특정 슬라이드 설명에 속함. "
                            "chatter: 수업과 무관한 잡담/행정. "
                            "other: 수업 내용이지만 슬라이드 귀속 애매."
                        ),
                    },
                    "target_page": {
                        "type": "integer",
                        "description": (
                            "action=merge_to_page일 때 slide index. "
                            "그 외는 0."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "분류 근거 한 줄 요약",
                    },
                },
                "required": [
                    "source",
                    "start_line",
                    "end_line",
                    "action",
                    "target_page",
                    "reason",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}


# Step 5: compose 한 페이지의 부분 결과 (Python 측에서 조립).
#
# LLM 은 강의 발화 blockquote 를 직접 생성하지 않음 — Python 이 mapping 의
# transcript 발췌를 deterministic 하게 `> ` blockquote 로 감싸서 조립.
# LLM 의 책임은 `key_terms` 와 `summary_markdown` 두 부분만.
COMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "key_terms": {
            "type": "array",
            "description": (
                "이 슬라이드의 핵심 용어 + 정의. 슬라이드 anchors 와 녹취에 "
                "실제 등장한 용어를 포함."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "term": {
                        "type": "string",
                        "description": "용어 (한국어 또는 영어 학술 용어)",
                    },
                    "definition": {
                        "type": "string",
                        "description": "한 줄 정의",
                    },
                },
                "required": ["term", "definition"],
                "additionalProperties": False,
            },
        },
        "summary_markdown": {
            "type": "string",
            "description": (
                "정리 섹션 본문 (markdown 허용, 단 ### 헤더 사용 금지). "
                "importance 레벨에 따른 길이/깊이로 작성."
            ),
        },
    },
    "required": ["key_terms", "summary_markdown"],
    "additionalProperties": False,
}


# ─────────────────────────── 런타임 스키마 팩토리 ───────────────────────────


def build_alignment_schema(allowed_sources: list[str]) -> dict:
    """ALIGNMENT_SCHEMA 에 `source` enum 을 주입한 변형 반환.

    `allowed_sources` 는 `numbered_txts.keys()` 목록. LLM이 이 집합 밖의
    값을 `source` 로 출력하면 OpenAI structured output strict 레벨에서 거부됨.
    """
    if not allowed_sources:
        raise ValueError("allowed_sources 가 비어 있음")
    schema = deepcopy(ALIGNMENT_SCHEMA)
    enum_values = sorted(set(allowed_sources))
    schema["properties"]["assignments"]["items"]["properties"]["ranges"][
        "items"
    ]["properties"]["source"] = {
        "type": "string",
        "enum": enum_values,
        "description": (
            "녹취록 파일 basename. 반드시 enum 목록 중 하나."
        ),
    }
    return schema


def build_reconcile_schema(allowed_sources: list[str]) -> dict:
    """RECONCILE_SCHEMA 에 `source` enum 을 주입한 변형 반환.

    Python 이 candidate 를 Python 측에서 넘기지만, LLM echoed decision 의
    source 가 변조되거나 hallucinate 되는 것을 enum 으로 차단.
    """
    if not allowed_sources:
        raise ValueError("allowed_sources 가 비어 있음")
    schema = deepcopy(RECONCILE_SCHEMA)
    enum_values = sorted(set(allowed_sources))
    schema["properties"]["decisions"]["items"]["properties"]["source"] = {
        "type": "string",
        "enum": enum_values,
        "description": (
            "candidate 녹취록 basename. 반드시 enum 목록 중 하나 "
            "(Python이 candidate 로 준 값과 일치시켜야 함)."
        ),
    }
    return schema


# slides_textify 결과 (현재 slides_textify는 output_schema 미사용이지만
# 필요 시 활성화할 수 있게 strict 호환으로 정의).
TEXTIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "title": {"type": "string"},
                    "anchors": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "brief": {"type": "string"},
                },
                "required": ["index", "title", "anchors", "brief"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["pages"],
    "additionalProperties": False,
}
