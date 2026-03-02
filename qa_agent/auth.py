from __future__ import annotations

import asyncio
import json
import os
import time
from urllib.parse import urljoin

from .config import Settings


def _context_kwargs() -> dict:
    return dict(
        viewport={"width": 1280, "height": 900},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    )


async def new_browser_context(browser):
    return await browser.new_context(**_context_kwargs())


async def save_session(context, path: str) -> None:
    state = await context.storage_state()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


async def load_session_context(browser, settings: Settings):
    kwargs = _context_kwargs()
    if os.path.exists(settings.session_file):
        try:
            with open(settings.session_file, encoding="utf-8") as f:
                state = json.load(f)
            return await browser.new_context(storage_state=state, **kwargs), True
        except Exception:
            pass
    return await browser.new_context(**kwargs), False


def _auth_hint_match(value: str, hints: list[str]) -> bool:
    lowered = (value or "").lower()
    return any((hint or "").lower() in lowered for hint in hints if hint)


def _make_url(base_url: str, route_or_url: str) -> str:
    raw = (route_or_url or "").strip()
    if not raw:
        return base_url
    if raw.startswith(("http://", "https://")):
        return raw.rstrip("/")
    if not raw.startswith("/"):
        raw = "/" + raw
    return urljoin(base_url.rstrip("/") + "/", raw.lstrip("/"))


def _build_login_candidates(settings: Settings) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        url = _make_url(settings.base_url, value)
        if not url or url in seen:
            return
        seen.add(url)
        candidates.append(url)

    if settings.auth_login_url:
        add(settings.auth_login_url)
    for path in settings.auth_login_paths:
        add(path)
    for path in settings.public_routes:
        lowered = path.lower()
        if any(token in lowered for token in ("login", "sign-in", "signin", "/auth/")):
            add(path)
    add(settings.base_url)
    return candidates


def _looks_authenticated(url: str, content: str, settings: Settings) -> bool:
    on_auth = _auth_hint_match(url, settings.auth_blocking_paths)
    has_signed_out_controls = any(
        marker in content for marker in ("logout", "log out", "sign out", "profile", "my account")
    )
    has_known_app_path = _auth_hint_match(url, settings.auth_success_paths)
    return not on_auth and (has_known_app_path or has_signed_out_controls)


def _is_dns_error(exc: Exception) -> bool:
    text = str(exc)
    return any(marker in text for marker in ("ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED"))


async def _fill_first(page, selectors: list[str], value: str) -> bool:
    if not value:
        return False
    for sel in selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=2500)
            if el:
                await el.fill(value)
                return True
        except Exception:
            continue
    return False


async def _click_first(page, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            btn = await page.wait_for_selector(sel, timeout=2000)
            if btn:
                await btn.click()
                return True
        except Exception:
            continue
    return False


async def check_session_alive(page, settings: Settings | None = None) -> bool:
    try:
        url = page.url.lower()
        if settings:
            return not _auth_hint_match(url, settings.auth_blocking_paths)
        return not any(k in url for k in ["/auth/", "/login", "/signin", "/verify"])
    except Exception:
        return True


async def do_login(page, settings: Settings) -> bool:
    print(f"[AUTH] Opening {settings.base_url}")
    login_candidates = _build_login_candidates(settings)
    last_error: Exception | None = None
    opened_login_page = False

    for login_url in login_candidates:
        try:
            print(f"[AUTH] Trying {login_url}")
            await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1.0)
            opened_login_page = True
            break
        except Exception as exc:
            last_error = exc
            if _is_dns_error(exc):
                raise RuntimeError(
                    "Cannot resolve the configured site host.\n"
                    f"  Base URL : {settings.base_url}\n"
                    f"  Tried    : {login_url}\n"
                    "Fix QA_BASE_URL or QA_LOGIN_URL/QA_LOGIN_PATHS for this site."
                ) from None

    if not opened_login_page:
        if last_error is not None:
            raise RuntimeError(
                "Could not open any configured login page.\n"
                f"  Base URL        : {settings.base_url}\n"
                f"  Login candidates: {', '.join(login_candidates)}\n"
                f"  Last error      : {last_error}"
            ) from None
        raise RuntimeError("Could not open any configured login page.") from None

    email_filled = await _fill_first(page, settings.auth_email_selectors, settings.email)
    password_filled = await _fill_first(page, settings.auth_password_selectors, settings.password)
    submit_clicked = False
    if email_filled or password_filled:
        submit_clicked = await _click_first(page, settings.auth_submit_selectors)
        if submit_clicked:
            await asyncio.sleep(1.5)
    else:
        print("[AUTH] Login form was not auto-detected. Waiting for manual login in the browser.")

    # Keep prior manual verification flow.
    started = time.time()
    print("[AUTH] Waiting for login/session verification in browser (up to 10 minutes)...")
    while time.time() - started < 600:
        try:
            url = page.url.lower()
            content = (await page.content()).lower()
            if _looks_authenticated(url, content, settings):
                print(f"[AUTH] Authenticated at {page.url}")
                return True
        except Exception:
            pass
        elapsed = int(time.time() - started)
        if elapsed > 0 and elapsed % 15 == 0:
            mode = "after auto-submit" if submit_clicked else "for manual login"
            print(f"[AUTH] Still waiting {mode}... {elapsed}s")
        await asyncio.sleep(5)
    print("[AUTH] Login timeout reached.")
    return False
