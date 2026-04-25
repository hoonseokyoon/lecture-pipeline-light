---
name: compile-review
description: Synthesize per-paper summaries into a coherent literature review document addressing the current RFI. Trigger after lit_summarize completes for all kept papers, or when user says "/compile-review".
---

# Compile Literature Review

개별 논문 요약(`agent-docs/summaries/*.json`) 을 종합해 RFI 에 답하는 리뷰
문서를 작성합니다.

## 입력

- `agent-docs/summaries/<paper-slug>.json` (lit_summarize 산출물)
  필드: problem, method, findings, limitations, relevance_to_rfi, key_quotes
- 현재 RFI 파일 — 질문·sub-questions·deliverables
- PIR — 전략 맥락

## 출력

- `agent-docs/reviews/rfi-<id>-claim-matrix.json` — 본문 작성 전 생성하는
  주장-근거 매트릭스.
- `agent-docs/reviews/rfi-<id>-review.md` — 섹션형 리뷰 (기본 5~10 페이지).

## 이미지 인라인 인용 규칙

논문에서 핵심 그림·표가 있으면 **반드시** 리뷰에 인라인 embed 하세요.

- 이미지는 이미 `extracted/<paper-slug>/assets/page_NNN/img-*.jpeg` 에 존재 (Mistral OCR 산출).
- 리뷰 파일은 `agent-docs/reviews/rfi-<id>-review.md` 에 작성 → 상대경로는
  `../../extracted/<paper-slug>/assets/page_NNN/img-X.jpeg`.
- 마크다운 형식:
  ```markdown
  ![Figure 2 — diffusion trajectory (Smith 2024)](../../extracted/2024-smith-diffusion/assets/page_004/img-1.jpeg)
  ```
- 원 논문의 Figure/Table 번호를 alt text 에 보존하고, 짧은 캡션을 1줄 첨부.
- 같은 그림을 여러 논문이 비교 제시한다면 나란히 배치 (표 또는 연속 이미지).
- 인용 없이 장식용 이미지 embed 금지.

Journal 에도 embed 한 이미지 목록 기록:
```bash
python -m gui_lit.ipc append-journal . \
  --event-json '{"actor":"head","kind":"review_images_embedded","rfi":"<id>","count":"<N>","paths":["../../extracted/.../img-1.jpeg"]}'
```

## 구조 (기본 — config 로 조정 가능)

```markdown
# RFI-<id> Literature Review: <제목>

_작성: <ISO date> / 커밋: <git SHA>_

## Executive Summary
3~5 문장. RFI 질문에 대한 현시점 답. 핵심 발견·격차.

## Scope & Method
- 어떤 RFI 에 답하는가 (질문 재진술)
- 검색 범위·기간·소스
- 포함 N편 / 제외 M편 / 이유

## Synthesis

### 주제 클러스터 1: <이름>
- 관련 논문: [A], [B], [C]
- 공통 가정/방법:
- 성과:
- 차이점:

### 주제 클러스터 2: ...

## Consensus & Disagreement
- 합의: ...
- 불일치: ...

## Research Gaps
- 미답 문제:
- 방법론적 공백:
- 재현성 이슈:

## Recommendations
- 다음 읽기 (5편): [ref + 이유]
- 이어질 RFI 제안:

## References
(번호 매긴 목록. DOI/URL 포함. 각 항목에 1줄 요약)
```

## 수행 단계

1. summaries/*.json 모두 로드. keep 만 대상 (triaged.json 참조).

2. 공통 주제 클러스터링 — 방법·데이터·결과 기준. LLM 능력 활용 (이미 풀
    컨텍스트 접근). 3~6 개 클러스터 추천.

3. 본문을 쓰기 전에 claim matrix 를 먼저 작성:
   ```json
   {
     "rfi": "<id>",
     "claims": [
       {
         "claim": "핵심 주장",
         "refs": ["paper_id_or_ref"],
         "fulltext_refs": ["paper_id_or_ref"],
         "abstract_only_refs": [],
         "confidence": "high|medium|low",
         "section": "Synthesis"
       }
     ]
   }
   ```
   저장 위치: `agent-docs/reviews/rfi-<id>-claim-matrix.json`.

4. claim matrix 를 근거로 각 섹션 작성 — 구체 근거(논문 번호) 와 함께.
   추측·과장 금지. claim matrix 에 없는 핵심 주장은 본문에 넣지 말 것.

5. 인용 형식 — `[N]` 본문, References 섹션에 대응. 인용 없는 주장 금지.
   abstract-only 근거만 있는 claim 은 high confidence 금지.

6. 초안 완성 후 스스로 점검:
   - RFI 의 모든 sub-question 이 답해졌는가?
   - 각 주장에 근거가 있는가?
   - 상반된 견해가 모두 소개됐는가?

7. doctor 로 review lint 통과:
   ```bash
   python -m gui_lit.doctor .
   ```

8. journal 기록 후 커밋:
   ```bash
   python -m gui_lit.ipc append-journal . \
     --event-json '{"actor":"head","kind":"review_compiled","rfi":"<id>","papers_count":"<N>","clusters_count":"<K>"}'
   git add agent-docs/reviews/rfi-<id>-review.md agent-docs/reviews/rfi-<id>-claim-matrix.json .litproj/journal.jsonl
   git commit -m "rfi-<id>: compile review — N papers, K clusters"
   ```

9. **Second opinion 요청 (권장)**:
   ```bash
   python $LIT_HARNESS_ROOT/run_skill.py second_opinion \
       agent-docs/reviews/rfi-<id>-review.md \
       REQUEST_FOR_INFORMATION.md \
       agent-docs/reviews/rfi-<id>-claim-matrix.json \
       .litproj/runs/<latest>/candidates.json \
       .litproj/runs/<latest>/triaged.json \
       --out agent-docs/second-opinions
   ```
   critique 내용을 읽고 개선이 필요하면 1회 개정. journal 에 기록.

10. 사용자에게 보고: 경로 + 주요 발견 3~5 줄. critique 이 있다면 그 요지도.
   "학습자료(glossary/Q&A/flashcards) 도 생성할까요?"

## 주의

- Abstract 만 읽고 종합하지 말 것. `extracted/<slug>/doc.md` 를 필요 시
  직접 확인 (특히 inconsistency 감지 시).
- 한국어 프로젝트라면 리뷰도 한국어로. 원제/원저자는 원문 유지.
- 길이는 config `review_depth` 에 따라 조정: short(1~2p) / section(5~10p) /
  full(20p+).
