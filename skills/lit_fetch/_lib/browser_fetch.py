"""Playwright 기반 PDF 다운로드 — JS challenge / 쿠키 필요 사이트 대응.

언제 쓰나:
- NCBI PMC: JS 검증 후에만 PDF 서빙 → requests 로는 HTML 만 받음
- 일부 Frontiers / 출판사: JS 로 리다이렉트 후 다운로드
- 기관 SSO 필요한 출판사: `storage_state` 에 로그인 상태 보관 후 재사용

사용:
    with BrowserSession() as sess:
        ok, reason, size = sess.download_pdf(url, dest_path, timeout=60)

의존성:
- playwright (pip install playwright)
- chromium 바이너리 (playwright install chromium — 최초 1회)

인증 (선택):
- 환경변수 `LIT_FETCH_STORAGE_STATE` 에 쿠키 JSON 파일 경로 지정
- 파일은 `python -m playwright codegen --save-storage=state.json <url>` 으로 생성
- 기관 SSO 로 1회 로그인 후 저장 → 이후 lit_fetch 가 자동 재사용
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Playwright

logger = logging.getLogger("lecture_pipeline.lit_fetch.browser")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def is_available() -> tuple[bool, str]:
    """Playwright 설치 상태 점검."""
    try:
        import playwright  # noqa: F401
    except ImportError as exc:
        return False, f"playwright 패키지 미설치: {exc}"
    # Chromium 바이너리 유무는 launch 시에만 확인 가능 — pre-check skip.
    return True, "ok"


class BrowserSession:
    """단일 Playwright 인스턴스 · chromium · context 재사용.

    lit_fetch 한 번 실행에 브라우저 하나만 띄워 여러 PDF 를 순차 다운로드.
    병렬 탭 사용하면 리소스 과다 → 순차 처리가 안전.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        storage_state: str | None = None,
        download_timeout_s: int = 90,
        use_real_chrome: bool = False,
        chrome_user_data_dir: str | None = None,
        chrome_channel: str = "chrome",
    ):
        """
        Args:
            headless: 기본 True. real Chrome 모드일 땐 False 권장.
            storage_state: 로그인 세션 JSON 경로 (standard 모드).
            download_timeout_s: 개별 다운로드 타임아웃.
            use_real_chrome: True 면 launch_persistent_context 로 실제 Chrome
                실행 — IP 기반 기관 인증 + 본인 Chrome 쿠키 상속.
                ACS/PNAS/Oxford 등 bot detection 회피에 강력.
            chrome_user_data_dir: Chrome 프로필 디렉토리. None 이면 임시 디렉토리.
                실제 본인 프로필 지정 시 (예: `~/AppData/Local/Google/Chrome/User Data`)
                **Chrome 을 종료한 뒤** 실행해야 lock 충돌 없음.
                Lit 전용 임시 프로필 경로 (예: `~/.lit_chrome_profile`) 권장.
            chrome_channel: "chrome" / "msedge" / "chrome-beta" 등. 번들이 아닌
                시스템 설치된 브라우저 사용.
        """
        self.headless = headless
        self.storage_state = storage_state
        self.download_timeout_s = download_timeout_s
        self.use_real_chrome = use_real_chrome
        self.chrome_user_data_dir = chrome_user_data_dir
        self.chrome_channel = chrome_channel
        self._pw: "Playwright | None" = None
        self._browser = None  # persistent 모드에선 사용 안함
        self._context: "BrowserContext | None" = None
        self._persistent = False  # persistent_context 사용 여부

    def __enter__(self) -> "BrowserSession":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        if self.use_real_chrome:
            # Real Chrome / persistent context 모드
            user_data = self.chrome_user_data_dir or str(
                Path.home() / ".lit_chrome_profile"
            )
            Path(user_data).mkdir(parents=True, exist_ok=True)
            logger.info(
                "[browser_fetch] real Chrome 모드 (channel=%s, profile=%s)",
                self.chrome_channel, user_data,
            )
            # Real Chrome 은 native UA 를 그대로 쓴다 — binary 와 UA version 을
            # 일치시켜 bot 감지 회피. 수동 오버라이드는 오히려 불일치 신호.
            try:
                self._context = self._pw.chromium.launch_persistent_context(
                    user_data_dir=user_data,
                    channel=self.chrome_channel,
                    headless=self.headless,
                    args=launch_args,
                    accept_downloads=True,
                    # user_agent 명시 안 함 → Chrome 의 실제 UA 사용
                )
                self._persistent = True
            except Exception as exc:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None
                raise RuntimeError(
                    f"real Chrome launch 실패 — {self.chrome_channel} 설치 확인 또는 "
                    f"use_real_chrome=false. 원인: {exc}"
                ) from exc
        else:
            # 표준: 번들 chromium (UA 오버라이드로 HeadlessChrome 문자열 제거)
            try:
                self._browser = self._pw.chromium.launch(
                    headless=self.headless, args=launch_args,
                )
            except Exception as exc:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None
                raise RuntimeError(
                    f"chromium launch 실패 — `playwright install chromium` 실행 "
                    f"필요: {exc}"
                ) from exc

            ctx_kwargs: dict = {
                "accept_downloads": True,
                "user_agent": _USER_AGENT,
            }
            if self.storage_state and Path(self.storage_state).exists():
                ctx_kwargs["storage_state"] = self.storage_state
                logger.info(
                    "[browser_fetch] storage_state 로드: %s",
                    self.storage_state,
                )
            self._context = self._browser.new_context(**ctx_kwargs)

        # Anti-detection — playwright-stealth (window.chrome, WebGL, plugins,
        # permissions, navigator.languages, iframe isolation 등 포괄 패치) 적용.
        # 설치 안돼있으면 기본 navigator.webdriver 패치로 fallback.
        try:
            from playwright_stealth import Stealth
            Stealth().apply_stealth_sync(self._context)
            logger.info("[browser_fetch] playwright-stealth 적용됨")
        except ImportError:
            logger.warning(
                "[browser_fetch] playwright-stealth 미설치 — "
                "기본 webdriver 마스킹만 적용"
            )
            try:
                self._context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )
            except Exception:
                pass
        except Exception as exc:
            logger.warning("[browser_fetch] stealth 적용 실패: %s", exc)
            try:
                self._context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )
            except Exception:
                pass

        self._context.set_default_navigation_timeout(
            self.download_timeout_s * 1000,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        closers = [lambda: self._context and self._context.close()]
        if not self._persistent:
            closers.append(lambda: self._browser and self._browser.close())
        closers.append(lambda: self._pw and self._pw.stop())
        for close in closers:
            try:
                close()
            except Exception:
                pass

    def download_pdf(
        self,
        url: str,
        dest: Path,
        *,
        min_bytes: int = 5000,
    ) -> tuple[bool, str, int]:
        """단일 URL → dest 로 PDF 저장. (success, reason, bytes) 반환.

        3단계 fallback:
        1) navigate → download event 기대
        2) navigate → 페이지의 PDF 링크 locator click
        3) 위 실패 시 PDF 링크 href 를 절대경로로 만들어 직접 navigate
        """
        if self._context is None:
            return False, "browser session 미초기화", 0
        from playwright.sync_api import TimeoutError as PWTimeout
        from urllib.parse import urljoin

        _DL_INTERRUPTED = ("download is starting", "net::err_aborted")

        def _is_download_signal(exc: Exception) -> bool:
            msg = str(exc).lower()
            return any(s in msg for s in _DL_INTERRUPTED)

        def _goto_catching_download(target: str, timeout_ms: int):
            """expect_download 안에서 goto. Navigation 이 download 로 대체된 경우
            예외를 swallow. return Download 객체 또는 None (timeout)."""
            try:
                with page.expect_download(timeout=timeout_ms) as dl_info:
                    try:
                        page.goto(target, wait_until="commit")
                    except Exception as e:
                        if not _is_download_signal(e):
                            raise
                return dl_info.value
            except PWTimeout:
                return None

        def _try_api_fetch(pdf_url: str) -> tuple[bool, str, int]:
            """Playwright APIRequestContext 로 HTTP GET. 브라우저 쿠키 재사용.
            헤드리스 chromium 이 PDF 를 inline 렌더링해 expect_download 가
            안 뜨는 문제를 우회 — 순수 HTTP 로 body 받아 파일 저장.
            """
            try:
                api = self._context.request
                # 쿠키는 context 에 이미 있음. 헤더 일부 추가.
                resp = api.get(
                    pdf_url,
                    headers={
                        "Accept": "application/pdf,*/*",
                        "Referer": page.url or pdf_url,
                    },
                    timeout=self.download_timeout_s * 1000,
                    max_redirects=10,
                )
            except Exception as exc:
                return False, f"api fetch 실패: {exc}", 0
            if not resp.ok:
                return False, f"api fetch HTTP {resp.status}", 0
            ctype = (resp.headers.get("content-type") or "").lower()
            body = resp.body()
            if body[:4] != b"%PDF":
                return False, f"api fetch: not a PDF (ctype={ctype[:60]})", len(body)
            if len(body) < min_bytes:
                return False, f"api fetch: too small ({len(body)}B)", len(body)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_bytes(body)
            except OSError as exc:
                return False, f"api fetch write 실패: {exc}", 0
            return True, "ok-browser-api", len(body)

        # URL 힌트
        url_lower = url.lower()
        looks_like_direct = (
            url_lower.endswith(".pdf")
            or ".pdf?" in url_lower
            or "/pdf/" in url_lower
            or "pdf=render" in url_lower
            or "ptpmcrender" in url_lower
            or "blobtype=pdf" in url_lower
        )

        page = self._context.new_page()
        try:
            download = None

            if looks_like_direct:
                # Attempt 1a: download 이벤트 짧게 기대 (일부 URL 은 즉시 다운로드)
                direct_ms = min(10000, self.download_timeout_s * 1000)
                download = _goto_catching_download(url, direct_ms)

            if download is None:
                # Attempt 1b: HTML 페이지 / 혹은 PDF viewer 로 열림
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    if _is_download_signal(e):
                        pass  # goto 안에서 download 시작 — 이후 expect_download 에서 못 잡음
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                # Cloudflare "Just a moment..." challenge 가 있으면 사라질 때까지 대기.
                # 최대 30s. headful + cf_clearance 쿠키 쌓이면 이후 빠름.
                self._wait_for_cloudflare_challenge(page, max_wait_s=30)
                # 쿠키 배너 자동 dismiss (배너가 citation_pdf_url 조회·click 방해하는
                # 경우가 있음). 실패해도 조용히 진행.
                self._dismiss_cookie_banner(page)

                # Attempt 2: API request — direct URL 이었으면 **본 URL**,
                #            아니면 citation_pdf_url / PDF locator
                pdf_url_to_fetch: str | None = None

                if looks_like_direct:
                    pdf_url_to_fetch = url
                else:
                    # 2a: meta citation_pdf_url
                    citation_pdf = self._find_citation_pdf_url(page)
                    if citation_pdf:
                        logger.info(
                            "[browser_fetch] citation_pdf_url: %s",
                            citation_pdf[:100],
                        )
                        pdf_url_to_fetch = citation_pdf
                    else:
                        # 2b: DOM locator
                        loc = self._find_pdf_locator(page)
                        if loc is not None:
                            href = None
                            try:
                                href = loc.get_attribute("href", timeout=3000)
                            except Exception:
                                pass
                            if href:
                                pdf_url_to_fetch = (
                                    href
                                    if href.startswith(("http://", "https://"))
                                    else urljoin(page.url, href)
                                )
                                logger.info(
                                    "[browser_fetch] DOM PDF link: %s",
                                    pdf_url_to_fetch[:100],
                                )

                if pdf_url_to_fetch is None:
                    return (
                        False,
                        "browser: citation meta / pdf link 모두 없음",
                        0,
                    )

                # Primary: API request (브라우저 쿠키 + HTTP, viewer 우회)
                ok, reason, size = _try_api_fetch(pdf_url_to_fetch)
                if ok:
                    return True, reason, size

                # Secondary: expect_download 패턴 (일부 사이트는 click 필수)
                logger.info(
                    "[browser_fetch] api fetch 실패 (%s) → expect_download 재시도",
                    reason,
                )
                try:
                    with page.expect_download(
                        timeout=self.download_timeout_s * 1000,
                    ) as dl_info2:
                        try:
                            page.goto(pdf_url_to_fetch, wait_until="commit")
                        except Exception as e:
                            if not _is_download_signal(e):
                                raise
                    download = dl_info2.value
                except PWTimeout:
                    return False, f"both api and download event failed ({reason})", 0

            # download 받은 경우: save_as
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                download.save_as(str(dest))
            except Exception as exc:
                return False, f"browser save_as 실패: {exc}", 0

            if not dest.exists():
                return False, "browser: saved file 미존재", 0
            size = dest.stat().st_size
            if size < min_bytes:
                try:
                    dest.unlink()
                except OSError:
                    pass
                return False, f"browser: too small ({size}B)", size

            try:
                with dest.open("rb") as f:
                    head = f.read(5)
                if head[:4] != b"%PDF":
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    return False, "browser: not a PDF", size
            except OSError:
                pass

            return True, "ok-browser", size
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _dismiss_cookie_banner(self, page, *, timeout_ms: int = 3000) -> bool:
        """흔한 쿠키 동의 배너를 자동으로 클릭. best-effort — 없어도 조용히 종료.

        실험·관찰에 기반한 publisher 별 selector. "Accept all" / "Agree" 류 우선.
        """
        # 명시적 selector 먼저 (구체적일수록 우선)
        specific_selectors = [
            # OneTrust (Elsevier, Taylor & Francis, Wiley, IEEE, ACM, ACS 많이)
            "#onetrust-accept-btn-handler",
            "button#onetrust-accept-btn-handler",
            # Cookiebot (Springer, BMC, 일부 Oxford)
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
            "#CybotCookiebotDialogBodyButtonAccept",
            # Nature / Springer Nature
            'button[data-test="cookie-banner-accept"]',
            'button[data-cc-action="accept"]',
            # EuropePMC / 기타
            ".cc-btn.cc-accept",
            ".cc-allow",
            # 일반 (아이디·클래스 힌트)
            '[id*="accept-cookies" i]',
            '[id*="cookie-accept" i]',
            '[class*="accept-cookies" i]',
            '[class*="cookie-accept" i]',
        ]
        # 텍스트 기반 (language-aware 가 아니라 영어·한국어 기본 커버)
        text_selectors = [
            'button:has-text("Accept all")',
            'button:has-text("Accept All")',
            'button:has-text("Accept cookies")',
            'button:has-text("Allow all")',
            'button:has-text("I agree")',
            'button:has-text("동의")',
            'button:has-text("전체 동의")',
            'button:has-text("Accept")',
        ]

        for sel in specific_selectors + text_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    try:
                        loc.click(timeout=timeout_ms, force=True)
                        logger.info("[browser_fetch] cookie banner dismissed (%s)", sel)
                        # 버튼 반응 후 잠깐 settle
                        import time as _t
                        _t.sleep(0.5)
                        return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    def _wait_for_cloudflare_challenge(self, page, *, max_wait_s: int = 30) -> bool:
        """Cloudflare 'Just a moment...' challenge 가 사라질 때까지 폴링.

        타이틀이 "Just a moment" 또는 URL 에 `__cf_chl` 가 있으면 challenge 중.
        반환: 통과 시 True, 시한 내 안 풀리면 False.
        """
        import time as _t
        deadline = _t.monotonic() + max_wait_s
        first_seen = False
        while _t.monotonic() < deadline:
            try:
                title = page.title()
                url = page.url
            except Exception:
                return False
            is_challenge = (
                "Just a moment" in title
                or "Please wait" in title
                or "Attention Required" in title
                or "__cf_chl" in url
            )
            if not is_challenge:
                if first_seen:
                    logger.info("[browser_fetch] Cloudflare challenge 통과")
                return True
            if not first_seen:
                logger.info("[browser_fetch] Cloudflare challenge 감지 — 대기 중")
                first_seen = True
            _t.sleep(1.5)
        if first_seen:
            logger.warning(
                "[browser_fetch] Cloudflare challenge %ds 내 미통과", max_wait_s,
            )
        return False

    def _find_citation_pdf_url(self, page) -> str | None:
        """`<meta name="citation_pdf_url" content="...">` — 학술지 산업 표준.
        거의 모든 peer-reviewed journal landing page 가 이걸 달고 있어 가장
        안정적이다. Google Scholar 도 이걸로 PDF 찾음.
        """
        try:
            # citation_pdf_url 또는 그 변형
            for name in ("citation_pdf_url", "citation_fulltext_html_url"):
                loc = page.locator(f'meta[name="{name}"]')
                if loc.count() > 0:
                    content = loc.first.get_attribute("content")
                    if content and "://" in content:
                        return content
        except Exception:
            pass
        return None

    def _find_pdf_locator(self, page):
        """페이지에서 PDF 링크 locator 반환. 출판사별 heuristic.

        중요: NCBI / Frontiers 는 모바일·데스크톱 양쪽에 동일 href 가 있고 한쪽은
        hidden. :visible 우선, 없으면 any.
        """
        # 우선순위: 좁은 매치부터 → 넓은 매치
        selector_groups = [
            # NCBI / PMC 전용 (부분 매치 — mobile/desktop 변형 모두 포함)
            ['a[data-ga-label*="pdf_download"]', 'a[data-ga-action*="pdf" i]'],
            # Aria / title 기반
            ['a[aria-label*="Download PDF" i]', 'a[title*="Download PDF" i]'],
            # Frontiers
            ['a.article-section__pdf', 'a[href*="/pdf/download" i]'],
            # 일반 .pdf 링크
            ['a[href$=".pdf"]', 'a[href*="/pdf/"]', 'a[href*=".pdf?"]'],
            # 클래스 힌트
            ['a.btn-pdf', 'a.download-pdf', 'a.PdfDownload'],
            # EuropePMC / NCBI 쿼리 URL
            ['a[href*="pdf=render"]', 'a[href*="ptpmcrender.fcgi"]'],
        ]
        # 1차: visible 매치만
        for group in selector_groups:
            for sel in group:
                try:
                    base = page.locator(f"{sel}:visible")
                    if base.count() > 0:
                        return base.first
                except Exception:
                    continue
        # 2차: 안 보여도 매치되는 것 수용
        for group in selector_groups:
            for sel in group:
                try:
                    base = page.locator(sel)
                    if base.count() > 0:
                        return base.first
                except Exception:
                    continue
        return None


# ── 유틸 ──

_PMC_RE = re.compile(r"(?i)^(PMC)?(\d+)$")


def normalize_pmc_id(raw: str) -> str | None:
    """'PMC12345' / '12345' → 'PMC12345'."""
    if not raw:
        return None
    m = _PMC_RE.match(raw.strip())
    if not m:
        return None
    return f"PMC{m.group(2)}"


def europepmc_pdf_url(pmc_id: str) -> str | None:
    """EuropePMC direct PDF URL (JS 없이 대부분 동작)."""
    norm = normalize_pmc_id(pmc_id)
    if not norm:
        return None
    return f"https://europepmc.org/articles/{norm}?pdf=render"


def europepmc_fcgi_pdf_url(pmc_id: str) -> str | None:
    """Legacy EuropePMC PDF endpoint. render URL 실패 시 fallback."""
    norm = normalize_pmc_id(pmc_id)
    if not norm:
        return None
    return f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={norm}&blobtype=pdf"


def pmc_article_url(pmc_id: str) -> str | None:
    """NCBI PMC article page URL (Playwright 로 열 때)."""
    norm = normalize_pmc_id(pmc_id)
    if not norm:
        return None
    return f"https://www.ncbi.nlm.nih.gov/pmc/articles/{norm}/"
