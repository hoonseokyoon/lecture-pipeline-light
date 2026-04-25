---
name: rfi-followup
description: Open and run a conditional follow-up/addendum turn for an existing RFI without minting a new RFI number. Trigger when user says "/rfi-followup", "RFI-0001 추가조사", "addendum", or asks to deepen a closed RFI.
---

# RFI Follow-up / Addendum

닫힌 RFI 에 조건부 추가조사를 얹는 절차입니다. 새 RFI 번호를 만들지 않고
기존 RFI 아래에 `fu-001`, `fu-002` 같은 하위 조사 턴을 생성합니다.

## 언제 사용하나

- 기존 RFI 결론의 약한 근거를 보강할 때
- second opinion 이 지적한 gap 을 targeted search 로 확인할 때
- 전체 리뷰 재작성 전, addendum 으로 근거를 축적할 때

새로운 PIR 질문이면 `/rfi-open` 을 사용하세요. 기존 RFI 의 특정 claim/gap
보강이면 `/rfi-followup` 을 사용합니다.

## 수행 단계

1. 대상 RFI id 와 조건부 질문을 확정합니다.
   예: `RFI-0001 cross-host promoter portability 근거를 더 찾아라`.

2. 기존 산출물을 읽습니다.
   - `agent-docs/rfi/<id>-<slug>.md`
   - `agent-docs/reviews/rfi-<id>-review.md`
   - 있으면 `agent-docs/second-opinions/*<id>*critique.md`
   - 기존 candidates/triaged/summaries

3. follow-up 생성은 반드시 CLI 로 합니다.
   ```bash
   python -m gui_lit.followup open . \
     --rfi <id> \
     --topic "<짧은 주제>" \
     --question "<조건부 추가조사 질문>" \
     --domain-profile "biomed" \
     --priority "high" \
     --must-address "<반드시 확인할 gap 1>" \
     --must-address "<반드시 확인할 gap 2>"
   ```

   생성 결과의 `followup_file` 또는 `query_plan` 에서 `<fu-id>` 와 `<slug>` 를
   확인한 뒤 follow-up branch 로 전환합니다.
   ```bash
   git switch -c rfi/<id>-followup-<fu-id>-<slug>
   ```

4. 생성된 `query_plan.json` 으로 `lit_search` 를 실행합니다.
   ```bash
   python $LIT_HARNESS_ROOT/run_skill.py lit_search \
     .litproj/followups/<id>/<fu-id>-<slug>/query_plan.json \
     --out .litproj/followups/<id>/<fu-id>-<slug>
   ```

5. 후보를 기존 RFI claim 과의 관계로 triage 합니다.
   follow-up triage 는 단순 keep/drop 이 아니라 다음을 반드시 포함합니다.
   - 기존 claim 을 강화하는가, 약화하는가, 새 조건을 추가하는가
   - full-text priority
   - abstract-only 로 충분한가
   - 원 리뷰에 병합해야 하는가

6. fetch/summarize 를 수행한 뒤 addendum 을 작성합니다.
   기본 산출물:
   - `agent-docs/reviews/rfi-<id>-<fu-id>-<slug>-addendum.md`
   - `agent-docs/reviews/rfi-<id>-<fu-id>-<slug>-claim-matrix.json`

7. addendum 구조:
   ```markdown
   # RFI-<id> Addendum <fu-id>: <topic>

   ## Trigger
   (왜 추가조사를 했는가)

   ## Delta Evidence
   (새 논문/근거가 기존 결론을 어떻게 바꾸는가)

   ## Claim Updates
   - 유지:
   - 약화:
   - 강화:
   - 새 claim:

   ## Merge Recommendation
   (원 review 에 병합 / addendum 유지 / 후속 RFI 로 승격)

   ## References
   ```

8. doctor 통과 후 follow-up 을 닫습니다.
   ```bash
   python -m gui_lit.doctor .
   python -m gui_lit.followup close . \
     --rfi <id> \
     --followup <fu-id> \
     --status done \
     --outcome "<핵심 결과 1-2문장>"
   python -m gui_lit.doctor .
   ```

9. 커밋합니다.
   ```bash
   git add agent-docs/rfi agent-docs/reviews .litproj/followups .litproj/journal.jsonl
   git commit -m "rfi-<id>: add follow-up <fu-id> — <topic>"
   ```

## 원칙

- follow-up 은 원 리뷰를 바로 덮어쓰지 않습니다. 먼저 addendum 으로 작성합니다.
- 기존 결론을 강화하는 근거뿐 아니라 약화/반례도 같은 우선순위로 다룹니다.
- abstract-only 근거만으로 high-confidence claim 을 만들지 않습니다.
- addendum 이 큰 새 질문으로 커지면 새 RFI 로 승격합니다.
