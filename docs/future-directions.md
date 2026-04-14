# 확장 방향 로드맵

현재 lecture-pipeline-light 은 강의 녹취 + 슬라이드 → 정적 HTML/MD 강의노트를
생성하는 파이프라인이다. 아래는 노트를 **인터랙티브 문서**로 발전시키기 위한
설계 방향을 정리한 문서. 각 항목은 기술적 타당성과 구현 부담을 함께 기록한다.

---

## 1. 아키텍처 전환: htmx + 백엔드 서버

### 목표
현재의 self-contained 정적 HTML(`note.html`) 모델을 **서버 기반 동적 문서**로 전환.

### 전제 구조

```
Browser (htmx) ←HTTP→ FastAPI 서버 ←→ LLM API
                          ↓
                      DB (SQLite)
                      ├ 사용자 인증
                      ├ chatbot 대화 기록
                      ├ 문서 주석/메모
                      └ 읽기 상태
```

### 핵심 컴포넌트

- **서버**: FastAPI (비동기 + SSE streaming 편리)
- **템플릿**: Jinja2
- **DB**: SQLite (단일 사용자 기준)
- **인증**: 쿠키 세션 + Argon2id 해싱
- **LLM streaming**: `sse-starlette` + htmx `hx-ext="sse"`

### 기존 파이프라인과의 통합

두 가지 모델:

- **A. Pipeline이 서버 DB에 직접 저장**:
  `run_pipeline` 완료 시 note 데이터를 서버 API로 POST → DB 저장 →
  브라우저는 서버 경유로 조회
- **B. 파일 import 모델**:
  기존 `out/note.json` 생성은 유지하고, GUI에 "서버 업로드" 버튼 추가

B 가 초기 도입 비용이 낮고, 장기적으론 A 로 이행하는 것이 깔끔.

### 배포 선택

| 배포처 | 장점 | 단점 |
|--------|------|------|
| 로컬 PC | 설정 간단, 인터넷 불필요 | PC 켜있을 때만 접근 |
| Tailscale VPN | 외부 기기에서도 본인 PC 접근 | VPN 설치 필요 |
| Fly.io / Railway | 어디서든 접근 | 월 비용 발생 |
| Raspberry Pi + Cloudflare Tunnel | 저비용 상시 | 초기 셋업 있음 |

---

## 2. 문서 내 LLM Chatbot

### 동작

1. 서버가 API key 보관 (클라이언트에 절대 노출 X)
2. 문서 내 chatbot UI → htmx 로 `/note/{id}/chat` POST
3. 서버가 세션 인증 → LLM 스트리밍 호출 → SSE 로 응답 조각 반환
4. htmx 가 응답을 `#chat-log` 에 append
5. 대화 내용을 DB 에 영속화
6. 로그인 시 이전 대화 복원

### Annotation 으로서의 chatbot 응답

각 슬라이드 섹션에 "이 슬라이드에 대해 질문" 버튼 →
chatbot 응답을 해당 섹션 하위 annotation 으로 저장 →
다음 로그인 시 그대로 복원.

### 스키마 스케치

```
users           (id, password_hash, ...)
notes           (id, run_id, owner_id, title, ...)
annotations     (id, note_id, page_idx, kind, content, created_at)
chat_sessions   (id, note_id, owner_id, started_at)
chat_messages   (id, session_id, role, content, created_at)
```

---

## 3. 로그인 인증

### 구조

- 문서 접근 시 상단에 로그인 버튼
- 비로그인: 문서 읽기만 가능 (chatbot/annotation 비활성)
- 로그인: htmx 로 `/auth/login` POST → 세션 쿠키 발급 →
  chatbot 활성화된 UI fragment 응답 → htmx 가 form 교체

### 보안

- Argon2id 로 비밀번호 해싱
- 쿠키: HttpOnly + Secure + SameSite
- 세션 만료 정책 설정
- CSRF 토큰

---

## 4. 상태 관리 (읽기 위치, 자동 저장)

### 로컬 전용
`localStorage` 에 scroll 위치, 접힌 섹션, 북마크 저장. 서버 없이도 가능.

### 서버 동기화
이미 서버가 있으면 자연스럽게 추가 — `/note/{id}/state` PUT 으로 저장,
GET 으로 복원. 여러 기기에서 동일 위치에서 이어보기 가능.

### 자동 저장 단위
- 주기적 (디바운스 30초)
- scroll idle 시
- 창 닫기 직전 (`beforeunload`)

---

## 5. 음성 + 인터랙티브 차트 임베드

### 음성

- 원본 녹취 오디오(.m4a/.mp3)를 서버에서 호스팅
- 문서 내 `<audio>` 요소 + 타임스탬프
- **녹취록 라인 클릭 → 해당 오디오 구간 재생** (lecture_note 의 line-level
  alignment 데이터 활용 가능)
- 파일 크기 때문에 base64 임베드보다 **서버 hosting + streaming** 이 실용적

### 인터랙티브 차트

- Plotly.js / Chart.js / Vega-Lite
- 강의 데이터(수치, 그래프 언급)를 LLM 이 JSON 스펙으로 추출 →
  클라이언트에서 렌더
- 수식은 KaTeX 로

---

## 6. 민감정보 암호화 저장

### 대상

- LLM API key
- 서버 주소
- 기타 자격증명

### 선택지

| 방식 | 설명 | 적합도 |
|------|------|--------|
| `keyring` 라이브러리 | Windows Credential Manager 사용 | 단일 사용자 데스크탑에 적합 |
| 사용자 마스터 패스워드 + AES-GCM | `cryptography` 라이브러리 | 이식성 필요 시 |
| HW 토큰 / YubiKey | 최상위 보안 | 오버엔지니어링 |

현재 `.env` plaintext → `keyring` 이행이 투자 대비 실익 큼.

---

## 7. iPad 자유 annotation

### 결론
**웹에서 네이티브 필기(GoodNotes 등) 수준 재현은 어려움.** 이유:
- Safari 의 Pointer Events 로 Apple Pencil 필압/기울기는 읽히지만
- **Palm rejection** 이 불완전
- **입력 지연** 이 네이티브 대비 큼
- 예측 렌더링 사용 불가

### 현실적 선택 3가지

**A. 본격 웹 필기 구현**
- 섹션별 canvas overlay + perfect-freehand / fabric.js
- 획을 JSON 으로 직렬화 → 서버 저장
- 난이도: 높음, 결과 품질: 네이티브 대비 아쉬움

**B. PDF 내보내기 + 외부 앱**
- 웹 문서는 읽기/chatbot/text annotation 중심
- "PDF 내보내기" 버튼 → GoodNotes/Notability 에서 필기
- **장점**: 네이티브 필기 경험 완벽
- **단점**: 필기가 웹 문서와 분리, 동기화 안 됨
- **권장**

**C. 영역별 "필기 첨부"**
- 각 섹션에 "필기 추가" → 간단한 캔버스 모달 → 이미지로 저장
- 네이티브 급은 아니지만 "수식 스케치" 정도는 커버
- 난이도: 중간

---

## 8. LLM 기반 벡터 annotation (이미지 위 스케치)

### 연구 기술 축

1. **Visual grounding**: Kosmos-2, Ferret, Shikra, Qwen-VL, InternVL, CogVLM —
   이미지 내 영역을 좌표/박스로 지목
2. **Set-of-Mark (SoM) 프롬프팅**: SAM 으로 사전 세그먼트 → 라벨링 →
   LLM 이 숫자 ID 로 영역 참조. **좌표 추론 부담을 분리**해서 정확도 향상
3. **Tool-use 기반**: LLM 이 `draw_arrow(from, to)` 같은 API 호출
4. **Agentic iteration**: 그리고-보고-수정 루프

### 실용 수준

| 작업 | 품질 |
|------|------|
| 큰 영역 박스/원 | 중간~높음 |
| 화살표 (A→B) | 중간 |
| 텍스트 라벨 + 지시선 | 중간 |
| 자유 스케치 | 낮음 |
| 세밀한 수식 수정 표시 | 낮음 |

### 권장 파이프라인

```
1. 슬라이드 이미지 → SAM pre-segment → 영역 ID 부여
2. SoM 오버레이 이미지 + "핵심 메커니즘 강조해줘"
3. LLM → "영역 7, 12 강조"
4. Python → 해당 segment bbox 를 SVG 원/박스로 변환 → 원본 위 overlay
```

**LLM 은 의미적 선택만** 하고, 좌표 정밀도는 SAM 마스크에서 파생 →
품질이 훨씬 안정적.

---

## 우선순위 제안

### Phase A — 서버 없이 가능 (저위험)
- 6번 keyring 기반 암호화 저장
- 4번 localStorage 기반 읽기 위치
- 5번 차트 JS 임베드

### Phase B — 서버 도입 (아키텍처 전환)
- 1번 htmx + FastAPI
- 3번 로그인
- 2번 chatbot + annotation 영속화
- 4번 서버 동기화로 확장
- 5번 음성 hosting

### Phase C — 부가 기능
- 7번 B 옵션 (PDF 내보내기는 간단)
- 8번 SoM 기반 LLM annotation (실험적)

### 유예 — 투자 대비 결과 아쉬움
- 7번 A 옵션 (웹 본격 필기)

---

## 설계 결정 대기 항목

1. **서버 배포처**: 로컬 / VPN / 클라우드
2. **파이프라인 통합**: A (직접 DB 저장) vs B (파일 import)
3. **Annotation 범위**: 텍스트 메모 / chatbot 대화 / 문서 원문 수정 허용 범위
4. **대상 사용자**: 본인 전용 vs 멀티 유저 (DB 선택, 인증 복잡도 좌우)
