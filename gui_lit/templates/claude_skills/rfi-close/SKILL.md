---
name: rfi-close
description: Close the current RFI — commit final state, mark status, and propose merge to main. Trigger when user says "RFI 완료" / "RFI close" / "/rfi-close".
---

# Close Current RFI

현재 active RFI 를 마무리하는 절차입니다.

## 수행 단계

1. 현재 브랜치 확인: `git branch --show-current` — `rfi/<id>-<slug>` 형태여야
   함. 아니면 경고하고 사용자 확인.

2. 미커밋 변경분 점검: `git status --short`. 있으면 의미있는 단위로 `git add`
   + `git commit` 수행. 커밋 메시지는 RFI id 접두사: `rfi-<id>: <요약>`.

3. 해당 RFI 파일 (`agent-docs/rfi/<id>-<slug>.md`) 의 front matter 업데이트:
   - `status: "done"` (또는 사용자 지시에 따라 `abandoned`)
   - `updated: "<ISO8601 now>"`
   - 본문 말미에 `## Outcome` 섹션 추가 — 산출물 경로, 핵심 결론 3~5줄.

4. journal 기록:
   ```bash
   python -c "
   import json, datetime
   with open('.litproj/journal.jsonl', 'a', encoding='utf-8') as f:
       f.write(json.dumps({
           'ts': datetime.datetime.utcnow().isoformat()+'Z',
           'actor': 'head',
           'kind': 'rfi_closed',
           'rfi': '<id>',
           'status': 'done',
           'deliverables': ['path/to/review.md', ...]
       }, ensure_ascii=False) + '\\n')
   "
   ```

5. 최종 커밋:
   ```bash
   git add -A  # RFI 파일 + journal
   git commit -m "rfi-<id>: close — <요약>"
   ```

6. **main 머지는 사용자 승인 필요**. 다음과 같이 보고:
   "RFI-<id> 완료. 브랜치 rfi/<id>-<slug> 에 <N>개 커밋 누적됨.
   main 에 머지하시겠어요? (승인하시면 `git switch main && git merge --no-ff
   rfi/<id>-<slug>` 수행)"

7. 사용자가 승인하면 머지, 아니면 브랜치 유지하고 다음 지시 대기.

## 주의

- `--no-ff` 머지 권장 (RFI 단위가 git log 에 뚜렷이 남음).
- 실패한 RFI (`status: abandoned`) 는 머지하지 말고 브랜치만 유지.
- AGENTS.md 의 "운영 교훈" 섹션에 학습 사항 있으면 append (선택).
