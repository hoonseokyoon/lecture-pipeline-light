"""Gemini 프롬프트 — detect/annotate 스크립트 전용."""


_DETECT_BASE = """\
첨부된 페이지 이미지에서 {prompt_body} 에 해당하는 영역을 **한 번에 모두**
찾아 `objects` 배열에 담아 반환하라. (한 응답에 여러 bbox 를 리스트로 반환 —
한 호출에 객체 하나만 넣지 말 것.)

# 분할 단위 — 가장 중요

bbox 는 **문단(paragraph) 수준** 으로 잘게 쪼개라. 아래는 각각 별도의 객체여야 한다:
- 논문/기사 제목, 부제, 저자, 소속, 학술지 헤더 (각각 분리)
- 섹션/서브섹션 헤더 (예: "Introduction", "Methods")
- 본문 **문단 하나하나** (한 문단이 하나의 text 객체)
- 그림(figure), 그림 캡션, 표, 표 캡션 (그림과 캡션은 별도 객체)
- 독립 수식 (별도), 인용 블록, 각주, 페이지 번호

하지 말 것 (절대):
- 컬럼 전체를 하나의 text 객체로 묶기 금지 — 반드시 문단별로 분리.
- 페이지의 절반 이상을 덮는 text bbox 금지 (figure/table 은 예외).
- 그림과 캡션을 하나의 figure 객체로 묶기 금지 — 각각 별도.

# 출력 규칙

- 좌표는 정규화 [0, 1000] 의 float. (x1, y1, x2, y2). 원점 좌상단.
- type 은 다음 중 하나로 엄격히 분류:
  - text: 본문 문단 / 제목 / 캡션 / 각주 / 페이지 번호 등 텍스트 블록
  - figure: 사진, 다이어그램, 차트, 스크린샷, 그림 (캡션은 별도 text 로)
  - table: 데이터 테이블 (행·열 구조)
  - equation: 독립된 수식 (inline 은 주변 text 에 포함시킬 것)
  - code: 코드 / 의사코드 / 쉘 블록
  - form: 양식 필드 (입력란, 체크박스, 서명란)
  - unknown: 위 어느 것에도 해당 안 되는 경우
- bbox 는 해당 영역을 **자연스럽게 감싸도록**. 경계 여유는 시스템에서 기계적으로
  주입하므로 여기선 **의미 단위(문단·헤더·그림 전체) 를 정확히 담는 것** 에만
  집중하라. 주변의 빈 공백을 과도하게 끌어안지는 말 것.
- reading order (사람이 읽는 순서) 대로 `objects` 배열을 정렬. 2단 레이아웃이면
  왼쪽 컬럼 위→아래 다 읽은 뒤 오른쪽 컬럼 위→아래.
- 같은 영역을 2번 이상 넣지 말 것.
- 서로 다른 객체끼리 과도하게 중첩되지 않도록 (IoU > 0.5 인 쌍은 한 쪽만).

{exclusion_clause}
{focus_clause}
"""


def build_detect_prompt(
    user_prompt: str,
    *,
    focus_bbox_norm: tuple[float, float, float, float] | None = None,
    existing_bboxes_norm: list[tuple[float, float, float, float]] | None = None,
) -> str:
    """detect 호출 프롬프트.

    existing_bboxes_norm 이 주어지면 "이 영역들은 이미 탐지됨 — 겹치지 말 것" 문구 추가.
    focus_bbox_norm 이 주어지면 해당 영역 내부만 focus detection.
    """
    exclusion = ""
    if existing_bboxes_norm:
        lines = []
        for b in existing_bboxes_norm:
            lines.append(f"  - [{b[0]:.1f}, {b[1]:.1f}, {b[2]:.1f}, {b[3]:.1f}]")
        exclusion = (
            "\n# 이미 탐지된 영역 (이들과 겹치지 않는 새 객체만 반환):\n"
            + "\n".join(lines) + "\n"
        )

    focus = ""
    if focus_bbox_norm is not None:
        b = focus_bbox_norm
        focus = (
            f"\n# Focus 영역 (bbox 내부만 탐지):\n"
            f"  [{b[0]:.1f}, {b[1]:.1f}, {b[2]:.1f}, {b[3]:.1f}]\n"
            "이 영역 바깥의 객체는 무시. 영역 내부를 세밀하게 sub-객체로 쪼개라.\n"
        )

    return _DETECT_BASE.format(
        prompt_body=user_prompt.strip(),
        exclusion_clause=exclusion,
        focus_clause=focus,
    )


def build_annotate_prompt(
    type_hint: str | None = None,
    previous_attempt: str | None = None,
    previous_error: str | None = None,
) -> str:
    """annotate 호출 프롬프트 — crop 이미지 하나를 받아 structured 출력.

    이전 시도 + 실패 이유가 있으면 재시도 hint 로 주입.
    """
    lines = [
        "첨부된 crop 이미지 하나를 분석하라. 이 이미지는 한 페이지에서 잘라낸 "
        "content 영역의 일부다.",
        "",
        "# 매우 중요",
        "",
        "이것은 **OCR / 구조화 추출** 작업이다. 이미지가 '무엇인지 설명(describe)'",
        "하라는 요청이 아니다. 이미지 안의 **실제 내용을 그대로 옮겨라**. 특히:",
        "",
        "- text: 이미지에 보이는 **온전한 줄의 모든 글자를 원문 그대로** 옮겨 쓴다.",
        "  단 경계에서의 처리 규칙이 있다:",
        "   · 좌/우 경계에서 **글자 일부가 잘린 경우** (예: 단어 끝 몇 글자만 보임)",
        "     → 문맥으로 복원해 원문 단어로 적는다.",
        "   · 위/아래 경계에서 **줄 전체가 온전히 보이면 포함**. 그러나 줄의",
        "     위·아래 끄트머리 (예: ascender/descender 일부, 글자 높이의 30% 미만)",
        "     만 살짝 걸쳐 보이는 경우엔 **그 줄을 포함하지 말 것** — 그건 인접",
        "     bbox 의 content 가 살짝 새어든 것이므로 무시.",
        "   · 판단 기준: 한 줄의 **대부분(50%+)이 보이면** 포함, **끄트머리 조금만**",
        "     보이면 제외.",
        "  한 줄도 빠뜨리지 말 것은 온전히 보이는 줄에만 적용된다. 생략·요약·",
        "  paraphrase 금지. 설명형 문장 (\"This is a paragraph about...\",",
        "  \"Header region for...\") 금지 — **반드시 원문 텍스트**.",
        "- equation: 이미지의 수식을 LaTeX 로 **정확히** 옮김. 수식 설명 금지.",
        "- table: 이미지의 표를 HTML 로 **있는 그대로** 재구성. 표 설명 금지.",
        "- code: 이미지의 코드 한 줄 한 줄을 그대로.",
        "- figure: **이때만** 설명이 허용된다 — 그림 내용을 2~4 문장으로 alt-text.",
        "",
        "원문 언어가 영어면 영어 그대로, 한국어면 한국어 그대로. 번역 금지.",
        "",
        "# 작업",
        "",
        "1. 이 객체의 type 을 다음 중 하나로 분류: "
        "text / figure / table / equation / code / form / unknown",
        "2. type 에 맞춰 `content` 필드에 구조화 텍스트 저장:",
        "   - text      → 이미지의 글자를 **원문 그대로** (OCR). 줄바꿈 보존.",
        "                  합자(ﬁ, ﬂ 등) 는 일반 글자로 (fi, fl) 펼쳐도 OK.",
        "   - equation  → LaTeX 수식 (순수 math, 앞뒤 $ 없이).",
        "   - table     → HTML <table>...</table>. 셀 병합은 rowspan/colspan.",
        "   - figure    → 2~4 문장의 한국어 alt-text 설명 (무엇을 보여주는지).",
        "   - code      → 원문 코드. 언어 추정되면 첫 줄에 `// lang: py` 주석.",
        "   - form      → 필드 레이블과 값을 'label: value' 형식으로.",
        "   - unknown   → 가능한 한 plain text 로.",
    ]
    if type_hint:
        lines.append("")
        lines.append(f"# 힌트: 이 객체의 type 은 `{type_hint}` 로 사전 추정됨. 검증해서 맞으면 그대로, 아니면 실제 type 으로 분류.")
    if previous_attempt:
        lines.append("")
        lines.append("# 이전 시도 결과 (거부됨 — 다시 하라):")
        lines.append("```")
        lines.append(previous_attempt[:1500])
        lines.append("```")
    if previous_error:
        lines.append("")
        lines.append(f"# 이전 실패 이유: {previous_error}")
        lines.append("이 이유를 참고해 결과를 개선하라.")

    return "\n".join(lines) + "\n"


def build_verify_prompt(type_str: str, content: str) -> str:
    """검증 프롬프트 — 원본 crop + (옵션) 렌더 결과를 Gemini 에게 비교시킴.

    type 별 기준:
      - equation/table: crop + 렌더 결과 2장 비교
      - text: crop 1장 + OCR content 텍스트 비교 (줄 누락·끊김 중점)
    """
    is_text = (type_str == "text")
    lines: list[str] = []
    if is_text:
        lines.extend([
            "첨부된 crop 이미지와 아래 OCR 결과를 비교해 정확성을 판정하라.",
            "",
            f"이 객체의 type 은 `{type_str}` 이고, OCR content 는:",
            "```",
            content[:2000],
            "```",
            "",
            "# 판정 기준 (중요)",
            "",
            "- 이미지의 줄 수와 content 의 줄 수가 일치하는가? (이미지에 N 줄이면",
            "  content 도 N 줄이어야 한다 — 첫 줄/중간 줄/마지막 줄 누락이 흔함)",
            "- 이미지의 모든 단어가 content 에 포함되는가? (한 단어 누락도 거부)",
            "- 경계에서 글자가 잘려 content 에 안 들어간 경우는 없는가?",
            "- 이미지에 없는 내용을 content 가 추가하지 않았는가?",
            "- 설명/요약이 아닌 원문 OCR 인가? (\"This is a paragraph...\" 같은",
            "  description 은 즉시 거부)",
            "",
            "사소한 하이픈 처리 (줄바꿈 시 hyphen 제거) 나 합자(ﬁ→fi) 펼침은 OK.",
            "폰트·줄간격 차이, 공백 정규화는 무시.",
        ])
    else:
        lines.extend([
            "첨부된 두 이미지를 비교하라:",
            "- 첫 번째: 원본 crop (실제 페이지에서 잘라낸 영역)",
            "- 두 번째: 제안된 annotation 을 렌더링한 결과",
            "",
            f"이 객체의 type 은 `{type_str}` 이고, annotation content 는:",
            "```",
            content[:2000],
            "```",
            "",
            "판정 기준:",
        ])
        if type_str == "equation":
            lines.extend([
                "- 수식 구조 (변수명, 연산자, 상·하첨자, 분수, 적분·합 기호) 가 일치하는가?",
                "- 문자 순서와 관계 기호가 보존됐는가?",
                "- 사소한 글꼴 차이·폰트 크기는 무시.",
            ])
        elif type_str == "table":
            lines.extend([
                "- 행·열 수가 일치하는가?",
                "- 셀 내용이 해당 위치에 있는가?",
                "- 병합 셀 구조가 보존됐는가?",
                "- 숫자·기호가 정확한가?",
                "- 행·열 순서가 바뀌지 않았는가?",
            ])
        else:
            lines.append("- 시각적·의미적으로 원본과 같은 내용을 담고 있는가?")
    lines.extend([
        "",
        "충분히 일치하면 `approved=true`, 아니면 `approved=false` 와 함께 "
        "무엇이 틀렸는지 구체적 이유를 `reason` 에 적어라. 줄 누락 시",
        "`reason` 에 '몇 번째 줄이 누락됨' 처럼 위치를 명시하라.",
    ])
    return "\n".join(lines) + "\n"
