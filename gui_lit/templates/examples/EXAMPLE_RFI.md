---
id: "0001"
slug: "diffusion-tts-artifacts"
status: "researching"
created: "2026-04-23T04:30:00Z"
updated: "2026-04-23T05:12:44Z"
parent_pir: "PIR-1"
branch: "rfi/0001-diffusion-tts-artifacts"
deliverables_expected:
  - candidates.json
  - review.md
  - glossary.md
tags: ["diffusion", "tts", "artifacts", "audio-quality"]
---

# RFI-0001 — Diffusion-based TTS 의 spectrogram 아티팩트 완화 기법

## Question

**Diffusion / flow-matching 기반 음성합성(TTS) 에서 고주파 잡음·tonal
artifact·mode collapse 를 완화하기 위해 2023년 이후 제안된 대표 기법들은
무엇인가? 각 기법의 진단 방법 · 완화 메커니즘 · trade-off 를 정리하라.**

## Sub-questions

1. 아티팩트의 **진단·측정** 메트릭 (PESQ, MCD, FAD, 청취자 평가) 중 diffusion
   샘플에 가장 민감한 것은?
2. **conditioning** (speaker embedding, prosody, text) 과 artifact 발생률은
   어떤 관계?
3. **vocoder 선택** (HiFi-GAN vs BigVGAN vs DAC) 이 diffusion decoder 의
   아티팩트를 얼마나 masking/증폭?
4. **classifier-free guidance** 의 scale 조정이 artifact 와 naturalness 의
   tradeoff 에 미치는 영향.
5. **multi-stage / cascade** (coarse→fine) vs **end-to-end** 구조의
   artifact profile 차이.

## Constraints

- **출처 우선순위**: ICASSP, Interspeech, ICLR, NeurIPS peer-reviewed
  우선. arXiv 은 인용수 10+ 또는 저자 reputation 고려.
- **기간**: 2023-01 ~ 2026-04.
- **언어**: 영어. 관련 한국어 논문 있으면 포함 OK.
- **최소 논문 수**: 15편 (keep), 최대 40편 (keep + maybe).
- **제외**: 이미지·비디오 diffusion 단독 연구, 상용 TTS API 벤치마크만 있는
  블로그 포스트, whitepaper.

## Expected Deliverables

- [x] `candidates.json` — 검색 원본
- [x] `triaged.json` — keep/maybe/drop 판정
- [ ] `review.md` — 섹션형 리뷰 (8~12 페이지, 클러스터 4~6개)
- [ ] `glossary.md` — 핵심 용어 30±5 개
- [ ] (선택) `study_pack/` — Q&A + flashcards

## Parent PIR

**PIR-1** (Audio Diffusion 의 품질·제어성 현황 파악) 의 하위 작전.
세부 항목 중 "samples 의 artifact 진단·완화" 축을 담당.

## Related RFIs

- 없음 (첫 RFI).
- 이 RFI 완료 후 자연스럽게 이어질 candidate RFI:
  - "Vocoder-free diffusion TTS 의 latency vs 품질"
  - "Streaming TTS 에서 diffusion 의 실시간성 한계"

## Search Query

다음을 OR 결합으로 검색, 합산 후 dedup:

- `diffusion TTS artifact spectrogram high-frequency`
- `flow matching speech synthesis noise reduction`
- `classifier-free guidance audio diffusion naturalness`
- `latent diffusion vocoder artifact`

연도 필터: 2023 이후.

## Progress

_(Head agent 가 단계별 업데이트. 이하는 예시)_

### 2026-04-23 04:30 — 시작
- 브랜치 `rfi/0001-diffusion-tts-artifacts` 생성.
- Semantic Scholar + arXiv 로 초기 검색 (총 112 hits, dedup 후 87).

### 2026-04-23 04:45 — Triage 완료
- keep: 21 편, maybe: 18 편, drop: 48 편.
- 사용자 확인 받아 keep+maybe 39 편 `lit_fetch` 진행.
- 3 편은 paywall 로 미획득 → 사용자에게 수동 획득 요청 (inbox 질문 drop).

### 2026-04-23 05:12 — OCR + 요약 진행 중
- Mistral OCR 완료 36 편. 평균 18 페이지/편, 총 $0.65 사용.
- Gemini Flash 요약 진행 중 (20/36 완료).

### (계속 업데이트될 것)

## Outcome

_(RFI 종료 시 Head agent 가 작성)_

**결론**: _(TODO — 3~5 문장 요지)_

**산출물 경로**:
- `agent-docs/reviews/rfi-0001-review.md`
- `agent-docs/summaries/*.md/json` (36 편)
- `user-docs/rfi-0001-glossary.md` (사용자 수기 보완)

**학습 요지** (AGENTS.md 운영 교훈 후보):
- 2023년 이후 대세 기법 ≈ <한줄>
- 논문 중 <N>% 가 <공통 약점> 에서 평가 누락

**이어지는 제안**:
- RFI-0002: _(TODO)_
