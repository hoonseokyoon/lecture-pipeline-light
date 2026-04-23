# {project_name}

문헌조사·리뷰·학습자료 생성 프로젝트. `gui_lit` (Streamlit) 으로 관리됩니다.

## 파일 구조

- `PRIORITY_OF_INTELLIGENCE.md` — 프로젝트 mission·scope·PIR
- `REQUEST_FOR_INFORMATION.md` — 현재 active RFI
- `AGENTS.md` — Head agent 영속 지시서 (편집 주의)
- `user-docs/` — 사용자 작성 자료
- `agent-docs/` — Head agent 산출물 (reviews/summaries/study)
- `originals/` — 원본 PDF·검색결과 (git 추적)
- `extracted/` — OCR Markdown + 이미지 (git 추적)
- `scripts/` — 공용 보조 스크립트
- `.litproj/` — 프로젝트 런타임 상태

## 실행

프로젝트 루트(이 폴더) 에서:

```bash
# harness 디렉토리 기준에서
cd /path/to/lecture-pipeline-light
streamlit run gui_lit/app.py -- --project /path/to/this-project
```

또는 `gui_lit` 앱의 "Open project" 로 이 디렉토리 선택.

## 환경변수

`.env` (harness 루트) 에 다음 필요:
- `MISTRAL_API_KEY` — Mistral OCR (doc_to_md)
- `GEMINI_API_KEY` — Gemini Flash (lit_summarize, 선택)
- `SEMANTIC_SCHOLAR_API_KEY` — 선택, 있으면 rate-limit 완화

Claude Code CLI 와 Codex CLI 는 각자 `claude auth login` / `codex auth login`
으로 개별 인증.

## 저작권 주의

`originals/` 에 저장되는 PDF 는 출판사 저작권 대상일 수 있습니다. 이 프로젝트는
**사적 연구 용도** 로만 사용하고, public remote 에 push 하지 마세요.
