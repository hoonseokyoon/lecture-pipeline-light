"""Gemini / Codex agent 프롬프트 생성.

호스트 (composite) 쪽에서만 씀. detect/annotate 스크립트의 Gemini 프롬프트는
scripts/_prompts.py 에 따로 있다.
"""


def build_profile_prompt(n_pages: int) -> str:
    return (
        f"첨부된 {n_pages}개의 샘플 페이지 이미지를 보고, 이 문서의 성격을 "
        "3~5 문장의 한국어로 간결히 묘사하세요.\n"
        "\n"
        "포함 항목:\n"
        "- 문서 종류: 논문/보고서/슬라이드/매뉴얼/법률/교재/서적/양식 중 어떤 것\n"
        "- 주요 언어\n"
        "- 주요 content 타입: 수식·표·그림·코드의 존재 여부와 빈도\n"
        "- 레이아웃 특이사항: 단수(1단/2단), 여백, 특수 요소\n"
        "\n"
        "JSON 형식으로 `summary_ko` 필드에 담아 반환."
    )


def build_page_agent_prompt(
    page_idx: int,
    page_total: int,
    profile_text: str,
    existing_objects: list[dict],
    max_attempts: int,
    agent_turns: int,
) -> str:
    """Codex agent 프롬프트. detect/annotate 두 도구만 쓰게 한다."""

    has_existing = bool(existing_objects)
    existing_note = (
        f"현재 objects.json 에 {len(existing_objects)}개 객체가 이미 저장되어 있음. "
        "누락된 영역만 추가 detect 하고, annotation 비어있는 객체만 annotate 하라."
        if has_existing else
        "이 페이지는 처음 처리됨. objects.json 비어있음 — detect 부터 시작."
    )

    lines = [
        f"# 목표 — 페이지 {page_idx}/{page_total} 구조화",
        "",
        "이 페이지의 모든 시각적 content 영역을 탐지·분류하고, **각 객체에 대해**",
        "`scripts/annotate.py` 를 호출해 annotation 을 채워 `outputs/objects.json`",
        "에 저장하라.",
        "",
        "# 문서 참고 (profile)",
        "",
        profile_text.strip() or "(프로파일 없음)",
        "",
        "# 현재 상태",
        "",
        existing_note,
        "",
        "# 가용 도구 (오직 2개)",
        "",
        "## scripts/detect.py",
        "```",
        "python scripts/detect.py --prompt \"...\" [--target OBJ_ID] [--overwrite]",
        "```",
        "- Gemini vision 에 prompt 를 주고 한 번에 여러 bbox 를 받아 objects.json 에 추가.",
        "- 증분: 기존 객체와 IoU ≥ 0.5 로 중복되는 bbox 는 자동 제외.",
        "- `--target OBJ_ID`: 그 객체의 bbox 내부만 focus detection — sub-객체로 분할.",
        "  큰 영역(컬럼·섹션 전체)이 하나의 bbox 로 잡혔을 때 이걸로 문단 단위로 쪼개라.",
        "- `--overwrite`: 전체 초기화 후 재감지 (최후 수단).",
        "- 호출 후 `outputs/annotated.png` 가 갱신됨.",
        "",
        "## scripts/annotate.py",
        "```",
        "python scripts/annotate.py [--target OBJ_ID] [--reannotate]",
        "```",
        "- annotation 이 비어있는 객체를 **실제 이미지에서** type 분류 + 구조화 텍스트 추출.",
        "  - text → OCR 결과 (이미지의 글자를 **그대로** 옮겨 적음)",
        f"  - equation → LaTeX (+ 렌더 검증 loop, 최대 {max_attempts}회)",
        "  - table → HTML (+ 검증 loop)",
        "  - figure → alt-text 설명 (검증 없음)",
        "  - code → 코드 문자열",
        "- target 지정 없으면 annotation 이 비어있는 모든 객체를 처리.",
        "- `--target OBJ_ID`: 그 객체만 강제 재annotate.",
        "- `--reannotate`: 전부 재처리.",
        "",
        "# 필수 흐름 (반드시 따를 것)",
        "",
        "**STEP 1 — detect (문단 수준 분할)**",
        "",
        "`detect --prompt \"...\"` 로 페이지 전체를 감지하되, 프롬프트 본문에는 반드시",
        "아래 네 가지를 포함하라:",
        "- 각 **문단(paragraph)**, 제목, 캡션, 각주, 페이지 번호를 각각 별도의 bbox 로 분리",
        "- 그림·표·수식·코드도 별도 bbox",
        "- 2단 컬럼 레이아웃이면 왼쪽·오른쪽 컬럼을 나누고, 그 안에서 다시 문단별로 분리",
        "- 컬럼 전체나 섹션 전체를 하나의 bbox 로 묶지 말 것",
        "",
        "bbox 가 너무 coarse 하면 (예: 컬럼 전체, 페이지 절반 이상) 해당 객체에",
        "`detect --target OBJ_ID --prompt \"이 영역을 문단·헤더·캡션 단위로 세분화\"`",
        "를 실행해 sub-객체로 쪼개라.",
        "",
        "**STEP 2 — annotate (실제 텍스트/수식 추출)**",
        "",
        "detect 가 끝나면 **반드시** `annotate` 를 호출하라 (target 없이 전체).",
        "이 단계를 건너뛰고 objects.json 을 직접 편집해 annotation 필드를 채우는 것은",
        "**엄격히 금지** — Gemini 의 실제 OCR·LaTeX 추출 결과 없이는 content 가",
        "이미지 description(요약) 으로 오염된다. detect 의 `detected_hint` 는 참고용",
        "힌트일 뿐 annotation 이 아니다.",
        "",
        "**STEP 3 — 검토 및 보정**",
        "",
        "`outputs/annotated.png` 와 `outputs/objects.json` 를 읽어 확인:",
        "- annotation 이 여전히 비거나 품질이 의심되면 `annotate --target OBJ_ID`",
        "- bbox 가 잘못됐거나 coarse 한 영역은 `detect --target OBJ_ID --prompt \"...\"`",
        "- 누락된 visual content 가 있으면 `detect --prompt \"...\"` 추가 호출",
        "",
        "",
        "# 종료 조건",
        "",
        "- 모든 객체의 `status` 가 'annotated' 또는 'verified' 이고, 페이지의 "
        "visual content 가 합리적으로 커버됨.",
        f"- 또는 agent turn 한계 ({agent_turns}) 도달 — 현재 상태로 종료.",
        "",
        "# 제약",
        "",
        "- **annotation 필드를 수동 편집하지 말 것**. bbox 좌표 소폭 조정이나 "
        "reading order 재배열 정도는 OK. 하지만 `annotation.type`, `.content`, "
        "`.latex`, `.html`, `.description`, `.raw_ocr` 은 반드시 annotate.py 로만 채운다.",
        "- detect 가 반환한 bbox 좌표는 최대한 존중.",
        "- reading order 는 `objects[]` 배열 순서로 표현.",
        "",
        "반드시 `outputs/objects.json`, `outputs/annotated.png`, "
        "`outputs/crops/*.png` 를 생성해서 종료하라.",
    ]
    return "\n".join(lines) + "\n"
