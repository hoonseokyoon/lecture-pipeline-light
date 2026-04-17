"""exam_problem_detect의 프롬프트 빌더."""

from __future__ import annotations


_COMMON_TAIL = (
    "\n\n최종 출력은 JSON 하나만. outputs/result.json 파일로 저장하고, "
    "응답 말미에도 동일한 JSON을 그대로 담아라. 스키마(_schema.json)를 "
    "엄격히 준수. 설명/markdown/추가 텍스트 금지."
)


def build_multibox_prompt() -> str:
    """페이지당 multi-box 탐지 프롬프트.

    이미지(inputs/image.png)는 시험지/교과서의 한 페이지. 목표는 이 페이지의
    **모든 독립 읽기 단위**를 flat하게 bounding box로 나열하는 것.
    """
    return (
        '이미지(inputs/image.png)는 시험지 또는 교과서의 한 페이지다. '
        '이 페이지에 있는 **모든 독립적인 읽기 단위**를 bounding box로 나열하라.\n\n'
        '[분리 원칙 — 중요]\n'
        '- **최대한 분할**한다. 대문제 전체를 하나의 거대 박스로 감싸지 말 것.\n'
        '- 대문제 구조가 있다면: 발문(main_prompt) / 공유 지문(shared_passage) / '
        '소문제 1, 2, ...(sub_problem) / 보기 블록(choice_block) — 각각 **별개 박스**.\n'
        '- 독립된 단문 문제는 sub_problem 하나로.\n'
        '- 여러 문제/여러 단위를 한 박스에 묶지 말 것.\n'
        '- 페이지 번호, 헤더/푸터, 학교명 같은 메타 텍스트는 박스 만들지 않음.\n'
        '- 이 페이지에 문제가 아예 없으면(표지/목차/빈 페이지 등) has_problems=false + problems=[].\n\n'
        '[좌표 규약]\n'
        '- 정규화 [0, 1000] float.\n'
        '- (x1, y1)=좌상단, (x2, y2)=우하단. 이미지 좌상단 원점.\n'
        '- x1<x2, y1<y2 엄수.\n'
        '- 박스는 해당 단위를 여유 없이 타이트하게 감쌀 것 (과도한 여백 금지).\n\n'
        '[kind 분류]\n'
        '- main_prompt: 여러 소문제가 공유하는 큰 발문/지시문\n'
        '- shared_passage: 여러 소문제가 공유하는 지문(본문 글)\n'
        '- sub_problem: 실제 문제 (소문제 또는 단독 문제)\n'
        '- figure_caption: 그림/도표 + 설명 블록\n'
        '- choice_block: 선택지(① ② ③ ④ ⑤ 등) 블록\n'
        '- other: 위 분류 어디에도 안 맞는 경우\n\n'
        '[출력 필드]\n'
        '각 박스에 x1, y1, x2, y2, kind, rationale(왜 독립 단위인지 1문장).'
        + _COMMON_TAIL
    )


def build_pick_prompt() -> str:
    """Cluster 3-후보(번호 1/2/3) 중 최적 박스 선택 프롬프트."""
    return (
        '이미지(inputs/image.png)에는 **동일 문제 영역**에 대한 3개의 bounding box '
        '후보가 번호(1, 2, 3)와 함께 컬러로 그려져 있다.\n\n'
        '세 후보는 이 문제를 느슨하게/중간으로/타이트하게 감싸는 변형이다. '
        '이 **문제 단위를 가장 정확하게 타이트하게 감싸는** 박스 번호를 고르라.\n\n'
        '판단 기준:\n'
        '- 문제의 모든 구성요소(발문/지문/보기/답란 등)가 박스 안에 완전히 들어옴\n'
        '- 문제 외 불필요한 영역(인접 다른 문제, 여백 과다)이 박스 밖에 있음\n'
        '- 박스가 문제 경계에 딱 맞음\n\n'
        'best_id (1|2|3) + reason 1문장 반환.'
        + _COMMON_TAIL
    )
