---
name: lit-triage
description: Review the candidates.json produced by lit_search and classify each paper (keep / drop / maybe) with brief rationale. Trigger after lit_search output appears or when user says "/lit-triage".
---

# Triage Literature Candidates

`lit_search` 가 생성한 `candidates.json` 을 검토하고 각 논문을 keep/drop/maybe
로 분류합니다.

## 입력

- `.litproj/runs/<latest>/candidates.json` 또는 최근 search run 경로
- 현재 RFI 파일 (질문·제약)
- PIR (scope)

## 수행 단계

1. candidates.json 로드. 필드: title, authors, year, venue, abstract, pdf_url,
   score (lit_search 가 매긴 1차 점수).

2. 각 후보에 대해 다음을 판정:
   - **keep**: RFI 질문에 직접 답하거나 핵심 방법론/결과 제공
   - **maybe**: 관련 있지만 보조적, snowball 근거용
   - **drop**: 범위 밖, 중복, 품질 불충분

3. 판정 근거를 1줄로 첨부. 예:
   ```json
   {
     "id": "...",
     "decision": "keep",
     "reason": "RFI 질문의 핵심 방법론/결과를 직접 제공",
     "evidence_role": "core|background|method|counterexample|snowball",
     "fulltext_priority": "high|medium|low",
     "risk_of_bias": "low|medium|high|unknown",
     "must_fetch": true
   }
   ```

4. 결과를 `.litproj/runs/<latest>/triaged.json` 으로 저장.

5. 요약 보고 — keep N개 / maybe M개 / drop K개. 상위 5개 keep 을 제목+이유
   1줄씩 나열.

6. 사용자에게 확인: "이대로 `lit_fetch` 로 다운로드 진행할까요? (keep + maybe
   총 X편) 특정 논문 제외하려면 id 알려주세요."

## Heuristics (의사결정 기준)

- 연도가 PIR constraint 밖 → drop
- 동일 저자·동일 제목 (preprint+published) → 하나로 dedup, published 선호
- 인용수 0 이고 < 1년 된 논문 → maybe (신규일 수 있음)
- 인용수 많고 방법 설명이 abstract 에 뚜렷 → keep
- Survey/Review 류 논문은 snowball 시드로 쓸 수 있어 우선순위 up
- 핵심 claim 을 지탱할 논문은 `must_fetch=true`, `fulltext_priority=high`.
- abstract 만으로도 배경 설명에 충분한 후보는 `evidence_role=background`,
  `must_fetch=false` 로 두고 review confidence 를 낮춘다.

## 주의

- 판정이 애매하면 drop 이 아니라 **maybe** 로 두고 사용자에게 상의.
- 원본 candidates.json 은 **편집하지 말 것** (감사 추적용). 결과는 별도 파일에.
