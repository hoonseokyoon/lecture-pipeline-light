"""lit_fetch 전용 Chrome 프로필 초기화.

실행:
    python scripts/setup_lit_chrome_profile.py
    python scripts/setup_lit_chrome_profile.py --profile-dir /path/to/custom
    python scripts/setup_lit_chrome_profile.py --channel msedge  # Edge 쓰고 싶을 때

동작:
1. 프로필 디렉토리 생성 (없으면) — 기본 `~/.lit_chrome_profile`
2. Playwright 로 시스템 설치된 Chrome 을 headful 실행, 위 프로필을 user_data_dir 로 사용
3. 사용자가 기관 SSO / 출판사 로그인 등 수동 작업
4. 터미널에서 Enter 누르면 context 닫고 쿠키·세션 저장
5. `.env` 에 추가할 환경변수를 안내

이후 Streamlit 재시작하면 `lit_fetch` 가 이 프로필로 publisher 접근 시도 →
기관 IP 인증 + 저장된 로그인 세션 활용.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_HARNESS_ROOT = Path(__file__).resolve().parent.parent
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))


DEFAULT_PROFILE = Path.home() / ".lit_chrome_profile"

# 시작 시 표시할 유용한 탭들
STARTER_URLS = [
    "https://pubmed.ncbi.nlm.nih.gov/",
    "https://pubs.acs.org/",
    "https://academic.oup.com/",  # Oxford Academic (NAR, G&D 등)
    "https://www.pnas.org/",
    "https://link.springer.com/",
    "https://journals.asm.org/",
]


def _check_chrome_installed(channel: str) -> tuple[bool, str]:
    """시스템에 Chrome / 해당 channel 이 설치됐는지 대략 확인."""
    import shutil
    candidates: list[str]
    if channel == "chrome":
        candidates = [
            "chrome",
            "google-chrome",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    elif channel == "msedge":
        candidates = [
            "msedge",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        return True, "(검증 스킵 — channel 이 사용자 지정)"

    for c in candidates:
        if shutil.which(c) or Path(c).exists():
            return True, c
    return False, "Chrome / Edge 설치 감지 안됨 (그래도 진행은 가능)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--profile-dir",
        type=Path,
        default=DEFAULT_PROFILE,
        help=f"프로필 저장 경로 (기본: {DEFAULT_PROFILE})",
    )
    ap.add_argument(
        "--channel",
        default="chrome",
        choices=["chrome", "chrome-beta", "chrome-dev", "msedge"],
        help="사용 브라우저 채널 (기본: chrome)",
    )
    ap.add_argument(
        "--headless",
        action="store_true",
        help="디버그용 — 보통은 headful 유지",
    )
    args = ap.parse_args()

    profile_dir = args.profile_dir.expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    is_fresh = not any(profile_dir.iterdir())

    ok, chrome_info = _check_chrome_installed(args.channel)
    print("=" * 60)
    print(" lit_fetch 전용 Chrome 프로필 setup")
    print("=" * 60)
    print(f"• 프로필 경로: {profile_dir}")
    print(f"• 브라우저 channel: {args.channel}")
    print(f"• 설치 확인: {'✓' if ok else '⚠'} {chrome_info}")
    print(f"• 상태: {'신규 생성' if is_fresh else '기존 프로필 열기'}")
    print()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ playwright 가 설치돼 있지 않습니다.")
        print("   pip install -r requirements.txt && playwright install chromium")
        return 1

    print("📋 로그인 체크리스트 (Chrome 창에서 순서대로):")
    print("   1. 기관 SSO — 학교 이메일로 로그인 (Catholic University 포털)")
    print("   2. 주요 publisher 탭 접속 → 기관 액세스 배너 확인")
    print("      - ACS, Oxford Academic, PNAS, Springer, ASM")
    print("      - 필요하면 '기관 로그인 / institutional login' 클릭")
    print("   3. 쿠키 동의 팝업이 뜨면 accept")
    print("   4. 로그인 잘 됐는지 임의의 논문 페이지에서 PDF 버튼 눌러 확인")
    print()
    print("완료하면 터미널로 돌아와 Enter 를 누르세요. (창은 안 닫아도 됨)")
    print()

    with sync_playwright() as p:
        # Real Chrome 은 native UA 를 쓰는 게 bot 감지에 유리 (binary/UA 일치).
        # setup 과 lit_fetch 양쪽 모두 동일 정책 → 쿠키 fingerprint 호환.
        try:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel=args.channel,
                headless=args.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                accept_downloads=True,
            )
            # playwright-stealth 적용 — 포괄적 anti-detection
            try:
                from playwright_stealth import Stealth
                Stealth().apply_stealth_sync(ctx)
            except ImportError:
                # fallback
                ctx.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )
        except Exception as exc:
            print(f"❌ Chrome 실행 실패: {exc}")
            print("   - Chrome 이 이미 다른 창으로 실행 중이면 먼저 종료해주세요.")
            print("   - 또는 --channel msedge 로 Edge 를 사용해보세요.")
            return 1

        # starter 탭들 열기
        for i, url in enumerate(STARTER_URLS):
            try:
                page = ctx.new_page() if i > 0 else ctx.pages[0]
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass  # 네트워크 이슈는 사용자가 수동으로 해결

        print("⏳ 로그인 작업 중... 완료 후 Enter:")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            print("\n중단됨 — 현재까지의 쿠키만 저장.")

        try:
            ctx.close()
        except Exception:
            pass

    print()
    print("=" * 60)
    print(" ✅ 프로필 저장 완료")
    print("=" * 60)
    print()
    print("다음을 .env (harness 루트) 또는 실행 환경변수에 추가:")
    print()
    print(f"  LIT_USE_REAL_CHROME=1")
    print(f'  LIT_CHROME_USER_DATA_DIR={profile_dir}')
    print(f"  LIT_CHROME_CHANNEL={args.channel}")
    print()
    print("또는 prompt 없이 config 에 넣으려면")
    print("`skills/lit_fetch/config.json`:")
    print(f'  "use_real_chrome": true,')
    print(f'  "chrome_user_data_dir": "{str(profile_dir).replace(chr(92), chr(92)*2)}",')
    print(f'  "chrome_channel": "{args.channel}"')
    print()
    print("이후 Streamlit 재시작 → Chat 에서 lit_fetch 재호출 → 기관 인증된")
    print("publisher 에서 자동 다운로드됩니다.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
