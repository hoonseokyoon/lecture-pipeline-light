# lecture-pipeline

강의 음성 파일을 자동으로 전사(NotebookLM)하고 교정(Codex CLI)하는 파이프라인.

## 구조

```
lecture-pipeline/
├── transcribe.py          # 단일 파일 전사 (NotebookLM)
├── correct.py             # 단일 파일 교정 (Codex CLI via WSL)
├── correct_config.json    # 교정 설정 (모델, 프롬프트 파일, reasoning effort)
├── correct_prompt.txt     # 교정 프롬프트 (자유 편집)
├── transcribe_folder.py   # 폴더 일괄 처리 파이프라인
├── transcribe_folder.sh   # 실행 래퍼
├── requirements.txt
└── .gitignore
```

## 동작 흐름

```
오디오 폴더 입력
    │
    ├─ 1) 노트북 생성 (또는 기존 노트북 재사용)
    │
    ├─ 2) NB Worker (기본 2병렬)
    │     오디오 → NotebookLM 업로드 → 전사 텍스트 + 요약 + 키워드 추출
    │     출력: <파일명>.txt
    │
    ├─ 3) Codex Worker (기본 4병렬, 전사 완료 즉시 시작)
    │     전사본 → Codex CLI로 교정 (간투사 제거, 문단 구분, 문장 부호 등)
    │     출력: <파일명>_corrected.txt
    │
    └─ 4) .transcribe_log로 진행 상태 관리
          재실행 시 미완료 단계만 수행
```

## 세팅 (Windows)

### 1. Python 패키지 설치

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. NotebookLM 로그인

Google AI Pro 계정이 필요합니다.

```bash
notebooklm login
```

브라우저가 열리면 Google 계정으로 로그인합니다. 쿠키가 `~/.notebooklm/storage_state.json`에 저장됩니다.

> **인증 만료 주의**: NotebookLM 인증은 비교적 빨리 만료됩니다.
> 전사 중 `Authentication expired or invalid` 에러가 나면 `notebooklm login`을 다시 실행하세요.
> 파이프라인 재실행 시 이미 완료된 파일은 건너뛰므로 중간부터 이어서 처리됩니다.

### 3. Codex CLI 설치 (Windows 네이티브)

교정 및 skill 실행에 필요합니다. 전사만 사용하려면 `--no-correct` 옵션을 사용하세요.

```powershell
# Node.js 필요: https://nodejs.org/
npm install -g @openai/codex
codex auth login
```

설치 확인:
```powershell
codex --version
```

> 이전 버전은 WSL 경유 Codex 호출을 썼지만 이제 Windows 네이티브로 직접 실행합니다.
> `codex.cmd`/`codex.exe` 가 PATH 에 있으면 자동 감지. 커스텀 경로는 환경변수
> `LECTURE_CODEX_PATH` 로 지정.

### 4. git CLI

Codex `--full-auto` 는 작업 디렉토리가 git repo 일 것을 요구합니다.
`git --version` 으로 확인. 없으면 https://git-scm.com/ 에서 설치.

### 5. (선택) Claude CLI — fallback 용

Codex 실패 시 Claude 로 재시도하는 경로만 **WSL** 을 씁니다.
fallback 이 필요 없으면 건너뜁니다.

```powershell
wsl --install -d Ubuntu
```

```bash
# WSL Ubuntu 내에서
sudo npm install -g @anthropic-ai/claude-code
claude /login
```

### 6. 스킬별 Python 의존성 (자동 처리)

각 스킬의 `scripts/requirements.txt` 는 하네스가 **자동으로 venv 에 설치**합니다.
첫 실행 시 한 번 빌드되고 이후 캐시. requirements.txt 가 바뀌면 자동 재빌드.

캐시 위치: `%USERPROFILE%\.lecture-pipeline\venvs\<skill>\`

### 7. (선택) Goose CLI — 대안 에이전트 백엔드

Codex 대신 [Goose](https://github.com/aaif-goose/goose) (Block, Apache-2.0) 로 스킬을
돌리고 싶을 때 사용. 스킬 `config.json` 에 `"backend": "goose"` 를 지정한 스킬만 이 경로로 흐릅니다.
기본 스킬은 전부 Codex 로 동작하므로 Goose 가 없어도 됩니다.

설치 (Windows):
```powershell
scoop bucket add extras
scoop install goose   # Desktop 앱 + CLI 바이너리 포함
```

scoop 설치가 **Desktop 앱**만 PATH 에 등록하므로, CLI 바이너리 (`goose.exe`) 를
PATH 에 별도로 노출해야 합니다. 아래 중 하나:

- (권장) scoop 설치본의 CLI 추출 후 `%USERPROFILE%\scoop\shims\goose.exe` 에 복사
- 또는 GitHub 릴리즈에서 `goose-x86_64-pc-windows-msvc.zip` 직접 다운로드 후 아무 디렉토리에 풀고
  `LECTURE_GOOSE_PATH` 환경변수로 절대경로 지정

> **주의**: scoop 이 만드는 `goose.cmd` 배치 shim 은 multi-line 프롬프트를
> 손상시켜 goose 가 "please send the task" 응답만 반환하는 증상이 있습니다.
> **반드시 `.exe` 를 직접 쓰세요** (하네스 resolver 가 자동으로 `.exe` 를 우선).

공급자 설정:
```powershell
# 한 번만: 기본 공급자/모델 설정
$env:GOOSE_PROVIDER = "openai"          # 또는 anthropic, gemini-cli, ollama, ...
$env:GOOSE_MODEL = "gpt-5.4"
# 공급자별 API 키 (OPENAI_API_KEY, ANTHROPIC_API_KEY, ...)
```

또는 `goose configure` 로 영속 저장.

스킬 opt-in 예:
```jsonc
// skills/my_skill/config.json
{
    "backend": "goose",
    "goose_provider": "openai",          // 선택 — 미지정시 GOOSE_PROVIDER env
    "goose_model": "gpt-5.4",
    "goose_max_turns": 30,
    "goose_max_tool_repetitions": 3
}
```

## 사용법

### 폴더 일괄 처리 (권장)

```bash
# 기본 실행 (NB 2병렬, Codex 4병렬)
bash transcribe_folder.sh /path/to/audio/folder

# 워커 수 조정
bash transcribe_folder.sh /path/to/folder --nb-workers 1 --codex-workers 2

# 전사만 (교정 없이)
bash transcribe_folder.sh /path/to/folder --no-correct

# 재실행 시 새 파일 및 미완료 작업만 처리
bash transcribe_folder.sh /path/to/folder
```

### 단일 파일

```bash
# 전사
python transcribe.py lecture.m4a -o lecture.txt

# 기존 노트북에 추가
python transcribe.py lecture.m4a --notebook-id <ID>

# 교정
python correct.py lecture.txt
python correct.py lecture.txt -o corrected.txt
```

## 출력 형식

### 전사 결과 (`<파일명>.txt`)

```
<text>
전사된 텍스트 (축어적, 간투사 포함)
</text>

<summary>
AI 요약
</summary>

<keywords>
키워드1
키워드2
</keywords>
```

### 교정 결과 (`<파일명>_corrected.txt`)

`<text>` 블록만 교정되고, `<summary>`와 `<keywords>`는 원본 유지.

## 설정 변경

### 교정 모델 변경

`correct_config.json`:

```json
{
    "model": "gpt-5.4",
    "reasoning_effort": "high",
    "prompt_file": "correct_prompt.txt"
}
```

### 교정 규칙 변경

`correct_prompt.txt`를 직접 편집하세요.

## 로그

`.transcribe_log` 파일이 대상 폴더에 생성됩니다.

```
NOTEBOOK_ID=<uuid>
NOTEBOOK_NAME=<폴더명>
TRANSCRIBED:<파일명>
CORRECTED:<파일명>
```

- `TRANSCRIBED`만 있고 `CORRECTED` 없음 → 재실행 시 교정만 수행
- 둘 다 있음 → 스킵
- 없음 → 전사 + 교정 수행
