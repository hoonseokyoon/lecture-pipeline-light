# gui_lit — Streamlit 기반 문헌조사 GUI

`lecture-pipeline-light` harness 위에 얹힌 **문헌조사·리뷰·학습자료 생성**
전용 GUI. 기존 `gui.py` (Tkinter, 강의 워크플로우) 와 분리되어 있다.

## 설계 요약

- **프로젝트 = git 리포지토리.** 모든 산출물·원본·저널이 git 에 추적된다.
- **Head agent = Claude Code CLI** (Claude Max 구독 사용, API 비용 0).
  - RFI 당 1세션 (`claude --print --resume <sid>`).
  - AGENTS.md 가 영속 지시서, `.litproj/journal.jsonl` 이 디스크 메모리.
- **외주 스킬**: lit_search, lit_fetch, doc_to_md (Mistral OCR), lit_summarize
  (Gemini Flash). 기존 harness `run_skill.py` 로 Head agent 가 bash 호출.
- **사용자 ↔ 에이전트 비동기**: `.litproj/inbox/` 에 메시지 drop, 에이전트가
  턴 시작 시 픽업. UI 는 `journal.jsonl` 을 tail 하며 방해 없이 관찰.
- **세션 granularity**: RFI 당 1세션. cross-RFI 맥락은 PIR + AGENTS.md +
  journal 로 전달.

## 설치

1. 기존 harness 의존성:
   ```bash
   pip install -r requirements.txt
   ```

2. gui_lit 전용 의존성 (Streamlit):
   ```bash
   pip install -r requirements-gui-lit.txt
   ```

   > **Anaconda 구버전 주의**: 기존 streamlit 0.x 가 깔려 있으면 altair
   > 충돌. `pip install -U streamlit` 로 upgrade 하거나 새 venv 추천:
   > ```bash
   > python -m venv .venv-gui-lit
   > .venv-gui-lit\Scripts\activate   # Windows
   > pip install -r requirements.txt -r requirements-gui-lit.txt
   > ```

3. API 키 (`.env` 파일, harness 루트에 위치):
   ```
   MISTRAL_API_KEY=...     # doc_to_md (OCR)
   GEMINI_API_KEY=...      # lit_summarize
   # 선택:
   SEMANTIC_SCHOLAR_API_KEY=...  # rate-limit 완화
   ```

4. Claude Code CLI 로그인:
   ```bash
   claude auth login
   ```

5. (선택) Codex CLI 로그인 (보조 subagent 용):
   ```bash
   codex auth login
   ```

## 실행

harness 루트에서:

```bash
streamlit run gui_lit/app.py
```

브라우저가 `http://localhost:8501` 로 열린다.

## 사용 흐름

1. **Home** → "새 프로젝트 생성" 에서 빈 경로 선택 → 프로젝트 생성.
   - git init + 템플릿 파일 배치 + `.litproj/` 셋업 + 초기 커밋.

2. **Overview** 에서 `PRIORITY_OF_INTELLIGENCE.md` 편집 (에디터로 직접).
   - 프로젝트 mission, scope, PIR 섹션 채우기.
   - 반영된 내용은 Streamlit 에서 자동으로 표시됨.

3. **Chat** 에서 Head agent 와 대화:
   - 예: "PIR 을 봤다. 첫 RFI 로 X 주제를 잡고 싶어. 적절한 하위 질문 제안해줘."
   - 에이전트가 `/rfi-open` 스킬 실행 → feature branch 생성 → RFI 파일 작성 → 커밋.
   - 이후 `lit_search`, `lit_fetch`, `doc_to_md`, `lit_summarize` 를 순차 호출.

4. **Observer** 에서 실시간 진행 관찰 (에이전트 방해 없이).

5. **Papers** 에서 DB 에 쌓인 논문 조회·검색.

6. RFI 완료 시 Chat 에서 `/rfi-close` → 사용자 승인 후 main 머지.

## 파일 구조

```
gui_lit/
├── app.py                  # Streamlit entry
├── app_state.py            # 공통 세션 상태 + sidebar
├── project.py              # 프로젝트 init / open / 검증
├── ipc.py                  # journal / inbox / session 파일 IPC
├── head_agent.py           # Claude Code CLI wrapper
├── db.py                   # sqlite (papers, summaries, citations)
├── pages/
│   ├── 1_Overview.py       # PIR/RFI/git 상태
│   ├── 2_Chat.py           # 에이전트 대화
│   ├── 3_Observer.py       # journal tail, git status
│   └── 4_Papers.py         # DB 브라우저
└── templates/              # 프로젝트 init 시 복사되는 템플릿
    ├── AGENTS.md
    ├── PRIORITY_OF_INTELLIGENCE.md
    ├── REQUEST_FOR_INFORMATION.md
    ├── README.md
    ├── gitignore, gitattributes
    ├── claude_settings.json
    └── claude_skills/{rfi-open,rfi-close,lit-triage,compile-review}/
```

## 트러블슈팅

### "claude CLI 를 찾을 수 없음"

- `claude --version` 이 PATH 에서 동작하는지 확인
- Windows: `claude.cmd` 가 PATH 에 있는지 확인
- 또는 `LIT_CLAUDE_PATH` 환경변수로 절대경로 지정

### 에이전트가 응답 없이 멈춘 느낌

- Observer 페이지로 이동해 journal 이 업데이트되는지 확인
- 긴 스킬 호출 (예: 50편 OCR) 은 수 분~십수 분 소요 가능
- Halt 플래그 (`Overview` 페이지) 로 긴급 정지

### "journal 이 너무 커졌어"

- `.litproj/journal.jsonl` 은 append-only 이지만 평소에는 수 MB 내
- 문제되면 세션 경계에서 `.litproj/journal-archive-<date>.jsonl` 로 rotate 후 새로 시작

### 저작권

`originals/` 에 저장되는 PDF 는 출판사 저작권 대상. **public remote 에 push
금지**. Private remote 또는 local-only 로 유지.

## 현재 상태 / 향후 과제

**MVP 구현됨**:
- 프로젝트 init / open / 최근 목록
- Head agent 기본 호출 (sync + async)
- Chat / Observer / Papers UI
- lit_search (Semantic Scholar + arXiv) / lit_fetch / lit_summarize (Gemini)
- 기본 Claude Skills (rfi-open/close, lit-triage, compile-review)

**확장 여지**:
- OpenAlex 추가, snowball (citation graph) 스킬
- study_pack 스킬 (Q&A, flashcards, mind map)
- MCP 서버 (sqlite / inbox / git 커스텀 툴)
- 벡터 검색 (sqlite-vss 또는 Chroma 통합)
- UI: 후보 논문 다중선택 triage, markdown 리뷰 인라인 편집기
- 세션 rotate / archive 자동화
