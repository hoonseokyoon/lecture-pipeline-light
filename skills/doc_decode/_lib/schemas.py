"""Gemini response schemas (호스트 측 — profile 전용).

detect/annotate 스크립트의 schema 는 scripts/_schemas.py 에 따로 있다.
"""


PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_ko": {
            "type": "string",
            "description": (
                "문서의 성격을 3~5문장의 한국어로 묘사. "
                "어떤 종류 (논문/보고서/슬라이드/매뉴얼/법률/교재/서적/양식/기타), "
                "언어, 주요 content 타입 (수식·표·그림·코드 여부), 레이아웃 특이사항."
            ),
        }
    },
    "required": ["summary_ko"],
}
