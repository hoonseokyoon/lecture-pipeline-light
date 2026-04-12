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

### 3. WSL + Codex CLI 설치

교정 기능에 필요합니다. 전사만 사용하려면 이 단계를 건너뛰고 `--no-correct` 옵션을 사용하세요.

```powershell
# PowerShell (관리자 권한)
wsl --install -d Ubuntu
```

재부팅 후 Ubuntu 초기 설정(사용자명/비밀번호)을 완료합니다.

```bash
# WSL Ubuntu 내에서
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g @openai/codex
codex auth login
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
