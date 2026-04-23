---
name: rfi-open
description: Open a new Request for Information. Creates a feature branch, a new RFI file under agent-docs/rfi/, updates REQUEST_FOR_INFORMATION.md as pointer, and records the open event in journal.jsonl. Trigger when user says "새 RFI 시작" / "open new RFI" / "/rfi-open <title>".
---

# Open New RFI

새 Request for Information 을 여는 절차입니다.

## 수행 단계

1. 사용자가 RFI 의 **제목 + 핵심 질문** 을 이미 제공했는지 확인.
   부족하면 사용자에게 물어본 뒤 진행.

2. RFI 번호 결정:
   ```bash
   ls agent-docs/rfi/ 2>/dev/null | wc -l  # 현재 개수
   ```
   새 번호 = (현재 최대 + 1), 4자리 zero-pad (예: `0003`).

3. slug 결정: 제목을 kebab-case, 영문 소문자, 30자 내.
   예: "Diffusion TTS artifact survey" → `diffusion-tts-artifacts`.

4. 브랜치 생성 및 전환:
   ```bash
   git checkout main && git pull --ff-only || true
   git checkout -b rfi/<id>-<slug>
   ```

5. RFI 파일 생성: `agent-docs/rfi/<id>-<slug>.md`
   템플릿 구조:
   ```markdown
   ---
   id: "<id>"
   slug: "<slug>"
   status: "researching"
   created: "<ISO8601>"
   updated: "<ISO8601>"
   parent_pir: "<PIR-N or empty>"
   branch: "rfi/<id>-<slug>"
   ---

   # <제목>

   ## Question
   <사용자 제공 질문>

   ## Sub-questions
   - (채워넣을 것)

   ## Constraints
   - (PIR 에서 상속 + RFI 고유)

   ## Expected Deliverables
   - [ ] candidates.json
   - [ ] review.md
   - [ ] (기타)

   ## Parent PIR
   <PIR-N 링크>

   ## Progress
   (당신이 진행하며 업데이트)
   ```

6. `REQUEST_FOR_INFORMATION.md` 를 업데이트 — 현재 active RFI 가 새로 열린
   것임을 반영. 상단 front matter 의 id/slug/status/branch 를 동기화하고,
   본문의 "현재 RFI" 섹션을 새 RFI 의 요약으로 교체.

7. journal 기록 (append-only):
   ```bash
   python -c "
   import json, datetime
   with open('.litproj/journal.jsonl', 'a', encoding='utf-8') as f:
       f.write(json.dumps({
           'ts': datetime.datetime.utcnow().isoformat()+'Z',
           'actor': 'head',
           'kind': 'rfi_opened',
           'rfi': '<id>',
           'title': '<제목>',
           'branch': 'rfi/<id>-<slug>'
       }, ensure_ascii=False) + '\\n')
   "
   ```

8. 초기 커밋:
   ```bash
   git add agent-docs/rfi/<id>-<slug>.md REQUEST_FOR_INFORMATION.md .litproj/journal.jsonl
   git commit -m "rfi: open <id> — <제목>"
   ```

9. 사용자에게 결과 보고: "RFI-<id> 가 열렸습니다. 브랜치 rfi/<id>-<slug> 에서
   작업 시작합니다. 다음 단계로 문헌 검색 쿼리를 제안드릴까요?"

## 주의

- main 에 직접 커밋하지 말 것. 반드시 브랜치 전환 먼저.
- 기존에 열린 RFI (status: researching) 가 있으면 먼저 사용자에게 확인 —
  "RFI-<prev> 가 아직 열려 있습니다. 잠시 중단하고 새 RFI 로 전환할까요,
  아니면 기존 것을 마무리할까요?"
