"""
explorer.py — Exhaustive page exploration with self-learning skip logic.

Every run gets smarter:
  - Elements that consistently produce no_change are SKIPPED (skip_score in KB)
  - Elements never seen before are clicked FIRST (new discovery)
  - Known high-value elements (produced nav/modal) are clicked SECOND
  - Known boring elements are skipped entirely, saving time for new areas
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
from typing import Any, Awaitable, Callable

from .config import Settings
from .knowledge import KnowledgeBase
from .state import CrawlState
from .telemetry import (
    ApiTelemetry, collect_perf_metrics,
    enable_dom_mutation_watcher, read_dom_mutations,
)
from .utils import canonicalize_path_from_url, clean_url, hash_text, safe_name_from_url, same_origin


CLICKABLE_SELECTORS: list[str] = [
    "button:not([disabled])",
    'a[href]:not([href^="mailto:"]):not([href^="tel:"]):not([href="#"])',
    "input[type=submit]:not([disabled])",
    "input[type=button]:not([disabled])",
    "input[type=reset]:not([disabled])",
    '[role="button"]:not([disabled])',
    '[role="menuitem"]', '[role="menuitemcheckbox"]', '[role="menuitemradio"]',
    '[role="option"]', '[role="tab"]', '[role="link"]', '[role="treeitem"]', '[role="gridcell"]',
    "[aria-haspopup]:not([disabled])", "[aria-expanded]:not([disabled])", "[aria-controls]:not([disabled])",
    "[data-testid]", "[data-test]", "[data-cy]", "[data-qa]", "[data-action]", "[data-click]", "[data-href]",
    "[class*='btn']:not(div)", "[class*='button']:not(div)", "[class*='action']:not(div)",
    "[class*='trigger']:not(div)", "[class*='toggle']:not(div)", "[class*='clickable']",
    "[class*='nav-item']", "[class*='menu-item']", "[class*='tab-item']",
    "[class*='accordion']", "[class*='collapse']", "[class*='dropdown-toggle']",
    "[onclick]", "[tabindex='0']:not(input):not(textarea):not(select)",
    "svg[role='button']", "svg[onclick]", "summary",
]

SKIP_TEXT_PATTERNS: frozenset[str] = frozenset([
    "sign out", "logout", "log out", "sign-out",
    "delete account", "remove account", "deactivate account",
    "©", "cookie", "privacy policy", "terms of service", "terms & conditions",
])

MAX_CLICKABLES = 60
MAX_STATE_DEPTH = 2
MAX_ACTIONS_PER_STATE = 18
MAX_FIELDS_PER_STATE = 10
MAX_STATES_PER_PAGE = 14

SAFE_INPUT_TYPES = {
    "", "text", "search", "email", "url", "tel", "number",
    "date", "datetime-local", "month", "week", "time",
    "checkbox", "radio",
}
SKIP_INPUT_TYPES = {
    "hidden", "password", "file", "submit", "button", "reset",
    "image", "range", "color",
}
DANGEROUS_TEXT_PATTERNS: frozenset[str] = frozenset([
    "delete", "remove", "destroy", "purge", "erase", "revoke",
    "archive", "deactivate", "disable", "permanent", "clear all",
    "reset account", "reset workspace",
])
SAFE_FORM_SUBMIT_PATTERNS: frozenset[str] = frozenset([
    "search", "find", "filter", "apply", "show", "refresh", "check",
    "validate", "preview", "test", "run",
])
QUERY_HINT_PATTERNS: frozenset[str] = frozenset([
    "search", "find", "lookup", "look up", "query", "keyword",
])
FILTER_HINT_PATTERNS: frozenset[str] = frozenset([
    "filter", "status", "sort", "category", "tag", "date range",
    "date from", "date to", "range", "type",
])
AUTH_HINT_PATTERNS: frozenset[str] = frozenset([
    "login", "sign in", "signin", "password", "otp", "verification code",
])
MUTATING_FORM_PATTERNS: frozenset[str] = frozenset([
    "create", "save", "submit", "add", "invite", "publish", "send",
    "update", "register", "checkout", "pay",
])
SETTINGS_HINT_PATTERNS: frozenset[str] = frozenset([
    "settings", "preferences", "profile", "workspace", "configuration",
])

MODAL_SELECTORS = [
    "[role=dialog]", "[aria-modal=true]", "[aria-modal='true']",
    ".modal", ".drawer", ".sheet",
    "[data-testid*=modal]", "[data-testid*=drawer]", "[data-testid*=dialog]",
    "[class*='modal']", "[class*='dialog']", "[class*='drawer']", "[class*='overlay']",
]
CLOSE_SELECTORS = [
    "[aria-label='Close']", "[aria-label='close']", "[aria-label='Dismiss']",
    ".modal-close", "[data-testid*=close]", "[class*='close-btn']", "[class*='btn-close']",
]


async def _modal_open(page) -> bool:
    for sel in MODAL_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return True
        except Exception:
            pass
    return False


async def _close_modal(page) -> None:
    for sel in CLOSE_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(0.35)
                return
        except Exception:
            pass
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.25)
    except Exception:
        pass


async def _modal_info(page) -> dict[str, Any]:
    info: dict[str, Any] = {"open": False, "title": "", "fields": [], "buttons": []}
    try:
        info["open"] = await _modal_open(page)
        if not info["open"]:
            return info
        for tsel in ["[role=dialog] h2", "[role=dialog] h3",
                     ".modal-title", ".drawer-title", "[class*='dialog__title']"]:
            try:
                el = await page.query_selector(tsel)
                if el:
                    info["title"] = (await el.inner_text() or "").strip()[:80]
                    break
            except Exception:
                pass
        zone = "[role=dialog],.modal,[class*='modal'],[class*='dialog']"
        for sel, key in [("input,textarea,select", "fields"), ("button", "buttons")]:
            try:
                els = await page.query_selector_all(f"{zone} {sel}")
                info[key] = [
                    (await e.inner_text() or await e.get_attribute("placeholder") or "")[:40]
                    for e in els[:12]
                ]
            except Exception:
                pass
    except Exception:
        pass
    return info


async def _take_screenshot(page, path: str) -> str:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        await page.screenshot(path=path, full_page=False, timeout=8000)
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        return ""


def _clean_label(value: str, max_len: int = 80) -> str:
    return " ".join(str(value or "").split())[:max_len]


def _is_dangerous_label(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return any(pat in text for pat in DANGEROUS_TEXT_PATTERNS)


def _synthetic_value(field_type: str, label: str, form_intent: str = "") -> str:
    field_type = (field_type or "").lower()
    hint = f"{label or ''} {form_intent or ''}".lower()
    if field_type == "email" or "email" in hint:
        return "qa.agent@example.com"
    if field_type == "url" or "website" in hint or "link" in hint:
        return "https://example.com"
    if field_type == "tel" or "phone" in hint or "mobile" in hint:
        return "5550101"
    if "first name" in hint:
        return "QA"
    if "last name" in hint or "surname" in hint:
        return "Agent"
    if "full name" in hint or ("name" in hint and "company" not in hint):
        return "QA Agent"
    if "company" in hint or "organization" in hint or "workspace" in hint:
        return "QA Workspace"
    if "title" in hint:
        return "QA test title"
    if "description" in hint or "summary" in hint or "notes" in hint or "message" in hint:
        return "QA generated coverage note."
    if "city" in hint:
        return "Pune"
    if "state" in hint or "province" in hint:
        return "MH"
    if "country" in hint:
        return "India"
    if "zip" in hint or "postal" in hint or "pincode" in hint:
        return "411001"
    if "address" in hint:
        return "123 QA Street"
    if "price" in hint or "amount" in hint or "budget" in hint or "cost" in hint:
        return "25"
    if "percent" in hint or "%" in hint:
        return "10"
    if field_type == "number":
        return "2"
    if field_type in {"search"} or "search" in hint or "filter" in hint:
        return "qa"
    if form_intent in {"query", "filter"}:
        return "qa"
    return "qa agent"


def _contains_any(value: str, patterns: frozenset[str]) -> bool:
    text = str(value or "").lower()
    return any(pattern in text for pattern in patterns)


def _form_intent(ctx: dict[str, Any], label: str, field_type: str, tag: str) -> str:
    combined = " ".join([
        str(label or ""),
        str(ctx.get("form") or ""),
        str(ctx.get("modal") or ""),
        str(ctx.get("section") or ""),
        str(ctx.get("placeholder") or ""),
        str(ctx.get("name") or ""),
    ]).lower()
    if _contains_any(combined, AUTH_HINT_PATTERNS):
        return "auth"
    if field_type == "search" or _contains_any(combined, QUERY_HINT_PATTERNS):
        return "query"
    if _contains_any(combined, FILTER_HINT_PATTERNS):
        return "filter"
    if _contains_any(combined, MUTATING_FORM_PATTERNS):
        return "mutating"
    if _contains_any(combined, SETTINGS_HINT_PATTERNS):
        return "settings"
    if tag == "textarea" or any(token in combined for token in ("description", "summary", "message", "notes", "comment", "bio")):
        return "content"
    return "general"


def _field_priority(candidate: dict[str, Any]) -> tuple[int, int, int, int, str]:
    intent_rank = {
        "query": 0,
        "filter": 1,
        "settings": 2,
        "content": 3,
        "general": 4,
        "auth": 5,
        "mutating": 6,
    }
    action_rank = {"select": 0, "date": 1, "checkbox": 2, "radio": 3, "input": 4}
    scope_rank = {"form": 0, "modal": 1, "section": 2, "tab": 3, "page": 4, "chrome": 5}
    return (
        0 if candidate.get("is_new") else 1,
        0 if candidate.get("is_priority") else 1,
        intent_rank.get(str(candidate.get("form_intent", "general")), 4),
        action_rank.get(str(candidate.get("action_kind", "input")), 5) + scope_rank.get(str(candidate.get("scope_kind", "page")), 4),
        str(candidate.get("text", "")).lower(),
    )


def _scope_details(ctx: dict[str, Any]) -> tuple[str, str]:
    if ctx.get("modal"):
        return "modal", _clean_label(ctx.get("modal"), 48)
    if ctx.get("chrome"):
        return "chrome", _clean_label(ctx.get("chrome"), 48)
    if ctx.get("tab"):
        return "tab", _clean_label(ctx.get("tab"), 48)
    if ctx.get("form"):
        return "form", _clean_label(ctx.get("form"), 48)
    if ctx.get("section"):
        return "section", _clean_label(ctx.get("section"), 48)
    return "page", ""


async def _element_context(el) -> dict[str, Any]:
    try:
        ctx = await el.evaluate(
            """e => {
                const clean = (v, n = 80) => (String(v || '').replace(/\\s+/g, ' ').trim()).slice(0, n);
                const textOf = (node) => clean(
                    node?.innerText ||
                    node?.textContent ||
                    node?.getAttribute?.('aria-label') ||
                    node?.getAttribute?.('data-testid') ||
                    node?.getAttribute?.('name') ||
                    node?.id ||
                    ''
                );
                const headingOf = (node) => {
                    if (!node || !node.querySelector) return '';
                    const own = textOf(node);
                    if (own) return own;
                    const heading = node.querySelector('h1,h2,h3,legend,[aria-label],[data-testid]');
                    return textOf(heading);
                };
                const closest = (selector) => e.closest(selector);
                const sectionNode = closest('section,article,[role="region"],[data-section],[data-testid*="section"]');
                const dialogNode = closest('[role="dialog"],[aria-modal="true"],.modal,[class*="dialog"]');
                const tabNode = closest('[role="tabpanel"],[data-state],[class*="tab-panel"]');
                const formNode = closest('form');
                const chromeNode = closest(
                    'nav,header,aside,[role="navigation"],[role="menubar"],[role="tablist"],' +
                    '[class*="sidebar"],[class*="sidenav"],[class*="menu"],[class*="breadcrumb"],' +
                    '[data-testid*="sidebar"],[data-testid*="nav"],[data-testid*="menu"]'
                );
                const labelNode = e.labels && e.labels.length ? e.labels[0] : null;
                const fieldType = clean(
                    e.getAttribute?.('type') ||
                    e.tagName?.toLowerCase() ||
                    ''
                );
                return {
                    section: textOf(sectionNode) || headingOf(sectionNode),
                    modal: textOf(dialogNode) || headingOf(dialogNode),
                    tab: textOf(tabNode) || headingOf(tabNode),
                    form: textOf(formNode) || headingOf(formNode),
                    chrome: textOf(chromeNode) || headingOf(chromeNode),
                    field_label: textOf(labelNode),
                    field_type: fieldType,
                    placeholder: clean(e.getAttribute?.('placeholder') || ''),
                    name: clean(e.getAttribute?.('name') || ''),
                };
            }"""
        )
        return {k: _clean_label(v) for k, v in (ctx or {}).items()}
    except Exception:
        return {
            "section": "",
            "modal": "",
            "tab": "",
            "form": "",
            "chrome": "",
            "field_label": "",
            "field_type": "",
            "placeholder": "",
            "name": "",
        }


async def _state_snapshot(page, route: str, depth: int) -> dict[str, Any]:
    meta = {"section": "", "headings": [], "active_tabs": [], "expanded": []}
    try:
        meta = await page.evaluate(
            """() => {
                const clean = (v, n = 64) => (String(v || '').replace(/\\s+/g, ' ').trim()).slice(0, n);
                const visible = (el) => !!(
                    el &&
                    el.isConnected &&
                    (el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                );
                const textOf = (el) => clean(
                    el?.innerText ||
                    el?.textContent ||
                    el?.getAttribute?.('aria-label') ||
                    el?.getAttribute?.('data-testid') ||
                    ''
                );
                const collect = (selector, limit = 5) => Array.from(document.querySelectorAll(selector))
                    .filter(visible)
                    .map(textOf)
                    .filter(Boolean)
                    .slice(0, limit);
                const headings = collect(
                    'main h1, main h2, main h3, [role="main"] h1, [role="main"] h2, [role="main"] h3, section h2, section h3, form legend'
                );
                return {
                    section: headings[0] || '',
                    headings,
                    active_tabs: collect('[role="tab"][aria-selected="true"], [role="tab"][aria-current="page"], [aria-current="page"], [aria-pressed="true"]', 4),
                    expanded: collect('[aria-expanded="true"]', 4),
                    links: Array.from(document.querySelectorAll('a[href]')).filter(visible).length,
                    clickables: Array.from(document.querySelectorAll('button,[role="button"],[role="tab"],[role="link"],summary,[aria-haspopup],[aria-expanded]')).filter(visible).length,
                    inputs: Array.from(document.querySelectorAll('input:not([type="hidden"]),textarea,select,[role="combobox"]')).filter(visible).length,
                };
            }"""
        ) or meta
    except Exception:
        pass

    modal = await _modal_info(page)
    clean_page_url = clean_url(page.url)
    label_parts: list[str] = []
    kind = "page"
    if modal.get("open"):
        kind = "modal"
        if modal.get("title"):
            label_parts.append(f"modal:{_clean_label(modal['title'], 48)}")
    if meta.get("active_tabs"):
        if kind == "page":
            kind = "section"
        label_parts.append(f"tab:{_clean_label(meta['active_tabs'][0], 40)}")
    if meta.get("section"):
        label_parts.append(_clean_label(meta["section"], 40))
    if not label_parts:
        label_parts.append("root")
    fingerprint_source = "|".join([
        route,
        clean_page_url,
        kind,
        modal.get("title", ""),
        *meta.get("active_tabs", []),
        *meta.get("expanded", []),
        *meta.get("headings", [])[:3],
    ])
    state_hash = hash_text(fingerprint_source, 12)
    label = " | ".join(label_parts)[:88]
    return {
        "route": route,
        "url": clean_page_url,
        "kind": kind,
        "label": label,
        "depth": depth,
        "modal_title": _clean_label(modal.get("title", ""), 48),
        "section": _clean_label(meta.get("section", ""), 48),
        "active_tabs": [_clean_label(v, 40) for v in meta.get("active_tabs", [])[:4]],
        "expanded": [_clean_label(v, 40) for v in meta.get("expanded", [])[:4]],
        "headings": [_clean_label(v, 48) for v in meta.get("headings", [])[:5]],
        "visible_links": int(meta.get("links", 0) or 0),
        "visible_clickables": int(meta.get("clickables", 0) or 0),
        "visible_inputs": int(meta.get("inputs", 0) or 0),
        "fingerprint": state_hash,
        "state_id": f"{route}::state::{state_hash}",
    }


async def _field_value(el) -> str:
    try:
        value = await el.evaluate(
            """e => {
                if (!e) return '';
                if (e.type === 'checkbox' || e.type === 'radio') return e.checked ? 'checked' : 'unchecked';
                if (e.tagName && e.tagName.toLowerCase() === 'select') return e.value || '';
                return e.value || e.textContent || '';
            }"""
        )
        return _clean_label(value, 64)
    except Exception:
        return ""


async def _validation_messages(page, context: dict[str, Any]) -> list[str]:
    scope = {
        "form": _clean_label(context.get("form", ""), 48).lower(),
        "modal": _clean_label(context.get("modal", ""), 48).lower(),
    }
    try:
        items = await page.evaluate(
            """scope => {
                const clean = (v, n = 120) => String(v || '').replace(/\\s+/g, ' ').trim().slice(0, n);
                const visible = el => !!(
                    el &&
                    el.isConnected &&
                    (el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                );
                const textOf = el => clean(
                    el?.validationMessage ||
                    el?.getAttribute?.('aria-errormessage') ||
                    el?.getAttribute?.('title') ||
                    el?.innerText ||
                    el?.textContent ||
                    el?.placeholder ||
                    el?.name ||
                    el?.id ||
                    ''
                );
                const matchesScope = el => {
                    if (!scope.form && !scope.modal) return true;
                    const formNode = el.closest('form');
                    const modalNode = el.closest('[role="dialog"],[aria-modal="true"],.modal,[class*="dialog"]');
                    const formText = clean(formNode?.innerText || formNode?.textContent || '', 160).toLowerCase();
                    const modalText = clean(modalNode?.innerText || modalNode?.textContent || '', 160).toLowerCase();
                    if (scope.form && formText && formText.includes(scope.form)) return true;
                    if (scope.modal && modalText && modalText.includes(scope.modal)) return true;
                    return !scope.form && !scope.modal;
                };
                const out = [];
                const seen = new Set();
                const selectors = [
                    '[aria-invalid="true"]',
                    'input:invalid',
                    'textarea:invalid',
                    'select:invalid',
                    '[role="alert"]',
                    '[aria-live="assertive"]',
                    '.error',
                    '.errors',
                    '.invalid-feedback',
                    '.field-error',
                    '[class*="error"]',
                ];
                for (const sel of selectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (!visible(el) || !matchesScope(el)) continue;
                        const text = textOf(el);
                        if (!text || seen.has(text)) continue;
                        seen.add(text);
                        out.push(text);
                        if (out.length >= 6) return out;
                    }
                }
                return out;
            }""",
            scope,
        )
        return [_clean_label(item, 120) for item in (items or []) if str(item or "").strip()][:6]
    except Exception:
        return []


def _is_safe_submit_label(label: str, form_intent: str) -> bool:
    cleaned = _clean_label(label, 64).lower()
    if not cleaned or _is_dangerous_label(cleaned):
        return False
    if any(token in cleaned for token in MUTATING_FORM_PATTERNS):
        return False
    if form_intent in {"query", "filter"}:
        return any(token in cleaned for token in ("search", "find", "filter", "apply", "show", "refresh"))
    if form_intent == "settings":
        return any(token in cleaned for token in ("test", "check", "validate", "preview"))
    return any(token in cleaned for token in SAFE_FORM_SUBMIT_PATTERNS)


async def _safe_submit_after_fill(page, candidate: dict[str, Any]) -> dict[str, Any]:
    form_intent = str(candidate.get("form_intent", "") or "")
    if form_intent not in {"query", "filter", "settings"}:
        return {"submitted": False, "submit_action": "", "submit_kind": ""}

    context = candidate.get("context", {}) or {}
    form_scope = _clean_label(context.get("form", ""), 48).lower()
    modal_scope = _clean_label(context.get("modal", ""), 48).lower()
    selectors = [
        "form button:not([disabled]), form [role='button']:not([disabled])",
        "[role='dialog'] button:not([disabled]), [role='dialog'] [role='button']:not([disabled])",
        "[role='search'] button:not([disabled]), [role='search'] [role='button']:not([disabled])",
        "button:not([disabled]), [role='button']:not([disabled])",
    ]
    try:
        for sel in selectors:
            buttons = await page.query_selector_all(sel)
            for btn in buttons[:18]:
                try:
                    if not await btn.is_visible():
                        continue
                    btn_ctx = await _element_context(btn)
                    btn_scope = _clean_label(btn_ctx.get("form", ""), 48).lower()
                    btn_modal = _clean_label(btn_ctx.get("modal", ""), 48).lower()
                    if form_scope and btn_scope and btn_scope != form_scope:
                        continue
                    if modal_scope and btn_modal and btn_modal != modal_scope:
                        continue
                    label = _clean_label(
                        await btn.inner_text()
                        or await btn.get_attribute("aria-label")
                        or await btn.get_attribute("data-testid"),
                        64,
                    )
                    if not _is_safe_submit_label(label, form_intent):
                        continue
                    await btn.click(timeout=2500)
                    await asyncio.sleep(0.35)
                    return {"submitted": True, "submit_action": label, "submit_kind": "button"}
                except Exception:
                    continue
    except Exception:
        pass

    if candidate.get("field_type") in {"search", "text", ""} and form_intent in {"query", "filter"}:
        try:
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.3)
            return {"submitted": True, "submit_action": "Enter", "submit_kind": "keyboard"}
        except Exception:
            pass
    return {"submitted": False, "submit_action": "", "submit_kind": ""}


class BreakDetector:
    def __init__(self) -> None:
        self._errors: list[str] = []
        self._fails:  list[str] = []

    def attach(self, page) -> None:
        page.on("console",       lambda m: self._errors.append(m.text[:200]) if m.type == "error" else None)
        page.on("pageerror",     lambda e: self._errors.append(str(e)[:200]))
        page.on("requestfailed", lambda r: self._fails.append(r.url[:200]))

    def snapshot(self) -> tuple[int, int]:
        return len(self._errors), len(self._fails)

    def diff(self, pe: int, pf: int) -> dict[str, Any]:
        return {
            "broke":        bool(self._errors[pe:] or self._fails[pf:]),
            "js_errors":    self._errors[pe:][:5],
            "net_failures": self._fails[pf:][:5],
        }


async def _try_select(page, el) -> dict[str, Any]:
    r: dict[str, Any] = {"type": "select", "options": [], "selected": "", "ok": False}
    try:
        tag = await el.evaluate("e => e.tagName.toLowerCase()")
        if tag == "select":
            opts = await el.evaluate("e => Array.from(e.options).map(o => o.text)")
            r["options"] = opts[:10]
            if len(opts) > 1:
                await el.select_option(index=1)
                r["selected"] = opts[1]; r["ok"] = True
        else:
            await el.click(); await asyncio.sleep(0.3)
            opts = await page.query_selector_all(
                '[role="option"],[role="listbox"] li,.dropdown-item,[class*="select-option"]'
            )
            r["options"] = [(await o.inner_text() or "")[:40] for o in opts[:10]]
            if opts:
                await opts[0].click(); r["selected"] = r["options"][0]; r["ok"] = True
    except Exception:
        pass
    return r


async def _try_date(page, el) -> dict[str, Any]:
    r: dict[str, Any] = {"type": "date", "ok": False}
    try:
        if await el.get_attribute("type") == "date":
            await el.fill("2025-06-15"); r["ok"] = True
        else:
            await el.click(); await asyncio.sleep(0.4)
            day = await page.query_selector(
                '[class*="day"]:not([class*="disabled"]),[data-date],'
                '.react-datepicker__day:not(.react-datepicker__day--disabled)'
            )
            if day:
                await day.click(); r["ok"] = True
    except Exception:
        pass
    return r


class Explorer:
    def __init__(
        self,
        settings: Settings,
        state:    CrawlState,
        api:      ApiTelemetry,
        kb:       KnowledgeBase | None = None,
        emit:     Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.state    = state
        self.api      = api
        self.kb       = kb
        self.emit     = emit
        self._first_shots: dict[str, str] = {}

    async def _shot(self, page, url: str, suffix: str = "") -> dict[str, Any]:
        name = safe_name_from_url(url) + (f"__{suffix}" if suffix else "") + ".png"
        path = os.path.join(self.settings.screenshot_dir, name)
        b64  = await _take_screenshot(page, path)
        baseline = self._first_shots.get(url)
        diff = None
        if not baseline:
            self._first_shots[url] = path
        else:
            try:
                pct = abs(os.path.getsize(baseline) - os.path.getsize(path)) / max(os.path.getsize(baseline), 1) * 100
                if pct > 5:
                    diff = f"{pct:.1f}% visual change"
            except Exception:
                pass
        return {"path": path, "b64": b64, "diff": diff}

    async def _discover_links(self, page) -> set[str]:
        found: set[str] = set()
        try:
            hrefs = await page.eval_on_selector_all(
                "a[href],[data-href],[router-link],[to]",
                """els => els.map(e =>
                    e.href || e.dataset.href ||
                    (e.getAttribute('to') ? location.origin + e.getAttribute('to') : '') || ''
                )"""
            )
            for h in hrefs:
                if isinstance(h, str) and h.startswith("http"):
                    found.add(clean_url(h))
        except Exception:
            pass
        return found

    async def _record_links(self, route: str, links: set[str] | list[str]) -> list[str]:
        discovered: list[str] = []
        for lnk in links:
            if not isinstance(lnk, str):
                continue
            cleaned = clean_url(lnk)
            if not cleaned or not same_origin(cleaned, self.settings.base_url):
                continue
            discovered.append(cleaned)
            if self.kb:
                self.kb.record_link(route, cleaned)
        return discovered

    async def _collect_clickables(self, page, route: str, state_key: str) -> tuple[list[dict], int]:
        """
        Collect ALL visible clickables then sort into buckets:
          1. NEW  — never seen in KB → always click (top priority, discovery)
          2. PRIO — known high-value (produced nav/modal before) → click second
          3. REST — known but unremarkable → click if budget allows
          4. SKIP — boring (≥3 consecutive no_change, low priority) → skipped

        Returns (ordered_candidates_to_click, n_skipped).
        """
        seen: set[str] = set()
        all_found: list[dict] = []

        kb_skip_raw = self.kb.elements_to_skip(route) if self.kb else set()
        kb_prio_raw = self.kb.priority_elements(route) if self.kb else set()
        kb_skip = {_clean_label(t, 60).lower() for t in kb_skip_raw}
        kb_prio = {_clean_label(t, 60).lower() for t in kb_prio_raw}
        kb_known = kb_skip | kb_prio

        for sel in CLICKABLE_SELECTORS:
            if len(all_found) >= MAX_CLICKABLES * 2:
                break
            try:
                for el in await page.query_selector_all(sel):
                    if len(all_found) >= MAX_CLICKABLES * 2:
                        break
                    try:
                        if not await el.is_visible():
                            continue
                        text = _clean_label(await el.inner_text(), 80)
                        aria = _clean_label(await el.get_attribute("aria-label"), 80)
                        testid = _clean_label(await el.get_attribute("data-testid"), 60)
                        tag = await el.evaluate("e => e.tagName.toLowerCase()")
                        role = _clean_label(await el.get_attribute("role"), 40)
                        ctx = await _element_context(el)
                        scope_kind, scope = _scope_details(ctx)
                        label = text or aria or ctx.get("field_label", "") or testid or f"<{tag}:{sel}>"
                        label_key = _clean_label(label, 60).lower()
                        key = f"{state_key}::{scope_kind}::{scope.lower()}::{label_key}"
                        if not label_key or key in seen:
                            continue
                        if any(p in label_key for p in SKIP_TEXT_PATTERNS) or _is_dangerous_label(label_key):
                            continue
                        seen.add(key)
                        all_found.append({
                            "el": el, "text": label, "tag": tag, "role": role,
                            "selector": sel,
                            "context": ctx,
                            "scope": scope,
                            "scope_kind": scope_kind,
                            "action_kind": "click",
                            "field_type": "",
                            "is_new":      label_key not in kb_known,
                            "is_priority": label_key in kb_prio,
                            "is_skip":     label_key in kb_skip,
                        })
                    except Exception:
                        continue
            except Exception:
                continue

        new_  = [c for c in all_found if c["is_new"]  and not c["is_skip"]]
        prio_ = [c for c in all_found if not c["is_new"] and c["is_priority"] and not c["is_skip"]]
        rest_ = [c for c in all_found if not c["is_new"] and not c["is_priority"] and not c["is_skip"]]
        skip_ = [c for c in all_found if c["is_skip"]]

        return (new_ + prio_ + rest_)[:MAX_CLICKABLES], len(skip_)

    async def _collect_inputs(self, page, route: str, state_key: str) -> tuple[list[dict], int]:
        seen: set[str] = set()
        found: list[dict] = []
        skipped = 0
        field_budget = getattr(self.settings, "max_form_fields", MAX_FIELDS_PER_STATE)

        kb_skip_raw = self.kb.elements_to_skip(route) if self.kb else set()
        kb_prio_raw = self.kb.priority_elements(route) if self.kb else set()
        kb_skip = {_clean_label(t, 60).lower() for t in kb_skip_raw}
        kb_prio = {_clean_label(t, 60).lower() for t in kb_prio_raw}
        kb_known = kb_skip | kb_prio

        selectors = [
            "input:not([type=hidden]):not([disabled]):not([readonly])",
            "textarea:not([disabled]):not([readonly])",
            "select:not([disabled])",
            '[role="combobox"]',
            "[class*='select__control']",
            "[data-datepicker]",
            "[class*='datepicker']",
        ]
        for sel in selectors:
            if len(found) >= field_budget * 2:
                break
            try:
                for el in await page.query_selector_all(sel):
                    if len(found) >= field_budget * 2:
                        break
                    try:
                        if not await el.is_visible():
                            continue
                        tag = await el.evaluate("e => e.tagName.toLowerCase()")
                        role = _clean_label(await el.get_attribute("role"), 40)
                        field_type = _clean_label(await el.get_attribute("type"), 24).lower()
                        ctx = await _element_context(el)
                        scope_kind, scope = _scope_details(ctx)
                        effective_type = ctx.get("field_type", "").lower() or field_type or tag
                        custom_field = role == "combobox" or "select" in sel or "datepicker" in sel
                        if effective_type in SKIP_INPUT_TYPES:
                            skipped += 1
                            continue
                        if effective_type not in SAFE_INPUT_TYPES and tag not in {"textarea", "select"} and not custom_field:
                            continue
                        label = _clean_label(
                            ctx.get("field_label") or ctx.get("placeholder") or ctx.get("name")
                            or await el.get_attribute("aria-label")
                            or await el.get_attribute("data-testid")
                            or await el.inner_text()
                            or f"<{tag}:{sel}>",
                            80,
                        )
                        label_key = _clean_label(label, 60).lower()
                        key = f"{state_key}::{scope_kind}::{scope.lower()}::{effective_type}::{label_key}"
                        if key in seen or not label_key:
                            continue
                        if any(p in label_key for p in SKIP_TEXT_PATTERNS) or _is_dangerous_label(label_key):
                            skipped += 1
                            continue
                        seen.add(key)
                        action_kind = "input"
                        if tag == "select" or role == "combobox" or "select" in sel:
                            action_kind = "select"
                        elif effective_type in {"date", "datetime-local", "month", "week", "time"} or "datepicker" in sel:
                            action_kind = "date"
                        elif effective_type in {"checkbox", "radio"}:
                            action_kind = effective_type
                        form_intent = _form_intent(ctx, label, effective_type, tag)
                        found.append({
                            "el": el,
                            "text": label,
                            "tag": tag,
                            "role": role,
                            "selector": sel,
                            "context": ctx,
                            "scope": scope,
                            "scope_kind": scope_kind,
                            "action_kind": action_kind,
                            "field_type": effective_type,
                            "form_intent": form_intent,
                            "is_new": label_key not in kb_known,
                            "is_priority": label_key in kb_prio,
                            "is_skip": label_key in kb_skip,
                        })
                    except Exception:
                        continue
            except Exception:
                continue

        ordered = sorted(
            [c for c in found if not c["is_skip"]],
            key=_field_priority,
        )
        return ordered[:field_budget], skipped

    async def _emit_interaction(self, root_url: str, interaction: dict[str, Any]) -> None:
        if not self.emit:
            return
        try:
            await self.emit({
                "type": "interaction_live",
                "url": root_url,
                "route": canonicalize_path_from_url(root_url),
                "interaction": interaction,
            })
        except Exception:
            pass

    async def _restore_root(self, page, root_url: str) -> bool:
        try:
            await page.goto(root_url, wait_until="networkidle", timeout=15000)
            await enable_dom_mutation_watcher(page)
            await asyncio.sleep(0.45)
            return True
        except Exception:
            return False

    async def _run_action(
        self,
        page,
        root_url: str,
        source_route: str,
        current_state: dict[str, Any],
        candidate: dict[str, Any],
        api_calls: list[dict[str, Any]],
        bd: BreakDetector,
    ) -> dict[str, Any]:
        el = candidate["el"]
        text = candidate["text"]
        context = candidate.get("context", {})
        before_url = page.url
        before_n = len(api_calls)
        before_mut = await read_dom_mutations(page)
        before_state = await _state_snapshot(page, source_route, current_state["depth"])
        before_value = ""
        after_value = ""
        before_validation = await _validation_messages(page, context)
        pe, pf = bd.snapshot()
        action_kind = candidate.get("action_kind", "click")
        primitive_meta: dict[str, Any] = {}
        scope_kind = candidate.get("scope_kind", "page")
        form_intent = candidate.get("form_intent", "")

        try:
            if action_kind == "click":
                await el.click(timeout=3000)
            elif action_kind == "select":
                before_value = await _field_value(el)
                primitive_meta = await _try_select(page, el)
                primitive_meta.update(await _safe_submit_after_fill(page, candidate))
                after_value = await _field_value(el)
            elif action_kind == "date":
                before_value = await _field_value(el)
                primitive_meta = await _try_date(page, el)
                primitive_meta.update(await _safe_submit_after_fill(page, candidate))
                after_value = await _field_value(el)
            elif action_kind in {"checkbox", "radio"}:
                before_value = await _field_value(el)
                await el.check(timeout=3000)
                primitive_meta.update(await _safe_submit_after_fill(page, candidate))
                after_value = await _field_value(el)
            else:
                before_value = await _field_value(el)
                await el.click(timeout=2000)
                await el.fill(_synthetic_value(candidate.get("field_type", ""), text, form_intent), timeout=3000)
                try:
                    await page.keyboard.press("Tab")
                except Exception:
                    pass
                primitive_meta.update(await _safe_submit_after_fill(page, candidate))
                after_value = await _field_value(el)
        except Exception:
            self.state.record_interaction(text, source_route, "timeout")
            if self.kb:
                self.kb.record_element(
                    source_route,
                    text,
                    candidate["selector"],
                    "timeout",
                    [],
                    [],
                    meta={
                        "action_kind": action_kind,
                        "same_page_transition": False,
                        "value_changed": False,
                        "surface_delta": 0,
                        "submitted": False,
                    },
                )
            interaction_id = hash_text(
                "|".join([source_route, before_state["state_id"], action_kind, text, "timeout"]),
                14,
            )
            return {
                "interaction_id": interaction_id,
                "action": text,
                "action_kind": action_kind,
                "selector": candidate["selector"],
                "tag": candidate["tag"],
                "role": candidate["role"],
                "field_type": candidate.get("field_type", ""),
                "form_intent": form_intent,
                "section": context.get("section", ""),
                "modal_context": context.get("modal", ""),
                "tab_context": context.get("tab", ""),
                "form_context": context.get("form", ""),
                "chrome_context": context.get("chrome", ""),
                "scope_label": candidate.get("scope", ""),
                "scope_kind": scope_kind,
                "outcome": "timeout",
                "from_state": clean_url(before_url),
                "to_state": clean_url(page.url),
                "from_route": source_route,
                "to_route": canonicalize_path_from_url(page.url),
                "from_state_id": before_state["state_id"],
                "to_state_id": before_state["state_id"],
                "from_state_label": before_state["label"],
                "to_state_label": before_state["label"],
                "from_state_kind": before_state["kind"],
                "to_state_kind": before_state["kind"],
                "state_depth": current_state["depth"],
                "next_state_depth": before_state["depth"],
                "state_changed": False,
                "same_page_transition": False,
                "value_changed": False,
                "broke": False,
                "is_new": candidate["is_new"],
                "discovered_urls": [],
                "api_calls": 0,
                "api_failures": 0,
                "dom_delta": 0,
                "surface_delta": 0,
                "surface_link_delta": 0,
                "surface_input_delta": 0,
                "surface_clickable_delta": 0,
                "js_errors": [],
                "net_failures": [],
                "submitted": False,
                "submit_action": "",
                "submit_kind": "",
                "validation_errors": [],
                "modal": {},
            }

        await asyncio.sleep(0.85)
        after_url = page.url
        after_mut = await read_dom_mutations(page)
        after_route = canonicalize_path_from_url(after_url)
        next_depth = current_state["depth"] + 1 if clean_url(after_url) == clean_url(before_url) else 0
        after_state = await _state_snapshot(page, after_route, next_depth)
        new_calls = api_calls[before_n:]
        fail_apis = [x for x in new_calls if x.get("failed")]
        mut_delta = max(0, int(after_mut.get("total", 0)) - int(before_mut.get("total", 0)))
        brk = bd.diff(pe, pf)
        url_changed = clean_url(after_url) != clean_url(before_url)
        state_changed = before_state["fingerprint"] != after_state["fingerprint"]
        modal_opened = (
            after_state["kind"] == "modal"
            and (
                before_state["kind"] != "modal"
                or after_state["state_id"] != before_state["state_id"]
            )
        )
        surface_link_delta = max(0, int(after_state.get("visible_links", 0)) - int(before_state.get("visible_links", 0)))
        surface_input_delta = max(0, int(after_state.get("visible_inputs", 0)) - int(before_state.get("visible_inputs", 0)))
        surface_clickable_delta = max(0, int(after_state.get("visible_clickables", 0)) - int(before_state.get("visible_clickables", 0)))
        surface_delta = surface_link_delta + surface_input_delta + surface_clickable_delta
        value_changed = bool(after_value and after_value != before_value)
        validation_errors = [msg for msg in await _validation_messages(page, context) if msg not in before_validation][:5]
        if primitive_meta.get("ok"):
            value_changed = value_changed or action_kind in {"select", "date"}

        if brk["broke"] and not url_changed and not modal_opened and not state_changed and not value_changed:
            outcome = "broken"
        elif url_changed:
            outcome = "navigation"
        elif modal_opened:
            outcome = "modal_open"
        elif state_changed or value_changed or mut_delta > 8 or surface_delta > 0 or validation_errors:
            outcome = "dom_mutation"
        elif fail_apis:
            outcome = "api_error"
        else:
            outcome = "no_change"

        discovered = await self._record_links(source_route, await self._discover_links(page))

        self.state.record_interaction(text, source_route, outcome)
        if self.kb:
            self.kb.record_element(
                source_route,
                text,
                candidate["selector"],
                outcome,
                brk["js_errors"] + brk["net_failures"],
                discovered,
                meta={
                    "action_kind": action_kind,
                    "same_page_transition": not url_changed and state_changed,
                    "value_changed": value_changed,
                    "surface_delta": surface_delta,
                    "submitted": bool(primitive_meta.get("submitted")),
                },
            )

        interaction_id = hash_text(
            "|".join([
                source_route,
                before_state["state_id"],
                action_kind,
                text,
                after_route,
                after_state["state_id"],
                outcome,
            ]),
            14,
        )
        return {
            "interaction_id": interaction_id,
            "action": text,
            "action_kind": action_kind,
            "selector": candidate["selector"],
            "tag": candidate["tag"],
            "role": candidate["role"],
            "field_type": candidate.get("field_type", ""),
            "form_intent": form_intent,
            "section": context.get("section", ""),
            "modal_context": context.get("modal", ""),
            "tab_context": context.get("tab", ""),
            "form_context": context.get("form", ""),
            "chrome_context": context.get("chrome", ""),
            "scope_label": candidate.get("scope", ""),
            "scope_kind": scope_kind,
            "outcome": outcome,
            "from_state": clean_url(before_url),
            "to_state": clean_url(after_url),
            "from_route": source_route,
            "to_route": after_route,
            "from_state_id": before_state["state_id"],
            "to_state_id": after_state["state_id"],
            "from_state_label": before_state["label"],
            "to_state_label": after_state["label"],
            "from_state_kind": before_state["kind"],
            "to_state_kind": after_state["kind"],
            "state_depth": current_state["depth"],
            "next_state_depth": after_state["depth"],
            "state_changed": state_changed,
            "same_page_transition": not url_changed and state_changed,
            "value_changed": value_changed,
            "api_calls": len(new_calls),
            "api_failures": len(fail_apis),
            "dom_delta": mut_delta,
            "surface_delta": surface_delta,
            "surface_link_delta": surface_link_delta,
            "surface_input_delta": surface_input_delta,
            "surface_clickable_delta": surface_clickable_delta,
            "broke": brk["broke"],
            "js_errors": brk["js_errors"],
            "net_failures": brk["net_failures"],
            "submitted": bool(primitive_meta.get("submitted")),
            "submit_action": _clean_label(primitive_meta.get("submit_action", ""), 48),
            "submit_kind": primitive_meta.get("submit_kind", ""),
            "validation_errors": validation_errors,
            "modal": after_state["kind"] == "modal" and {
                "open": True,
                "title": after_state.get("modal_title", ""),
            } or {},
            "discovered_urls": discovered[:8],
            "is_new": candidate["is_new"],
        }

    async def _explore_state(
        self,
        page,
        root_url: str,
        source_route: str,
        state_depth: int,
        api_calls: list[dict[str, Any]],
        bd: BreakDetector,
        seen_states: set[str],
        seen_actions: set[str],
    ) -> tuple[list[dict[str, Any]], int]:
        if len(seen_states) >= getattr(self.settings, "max_page_states", MAX_STATES_PER_PAGE):
            return [], 0

        current_state = await _state_snapshot(page, source_route, state_depth)
        if current_state["fingerprint"] in seen_states:
            return [], 0
        seen_states.add(current_state["fingerprint"])

        results: list[dict[str, Any]] = []
        skipped_total = 0
        inputs, skipped_inputs = await self._collect_inputs(page, source_route, current_state["fingerprint"])
        clicks, skipped_clicks = await self._collect_clickables(page, source_route, current_state["fingerprint"])
        skipped_total += skipped_inputs + skipped_clicks
        budget = getattr(self.settings, "max_state_actions", MAX_ACTIONS_PER_STATE)
        candidates = (inputs + clicks)[:budget]

        for candidate in candidates:
            action_token = "::".join([
                current_state["state_id"],
                candidate.get("action_kind", "click"),
                _clean_label(candidate.get("scope", ""), 48).lower(),
                _clean_label(candidate["text"], 60).lower(),
            ])
            if action_token in seen_actions:
                continue
            seen_actions.add(action_token)

            result = await self._run_action(
                page,
                root_url,
                source_route,
                current_state,
                candidate,
                api_calls,
                bd,
            )
            results.append(result)
            await self._emit_interaction(root_url, result)

            should_recurse = (
                result.get("same_page_transition")
                and result.get("outcome") in {"modal_open", "dom_mutation"}
                and state_depth < getattr(self.settings, "page_state_depth", MAX_STATE_DEPTH)
                and result.get("to_state_id") != result.get("from_state_id")
            )
            needs_reset = (
                result.get("state_changed")
                or result.get("value_changed")
                or result.get("outcome") in {"navigation", "modal_open", "dom_mutation"}
            )
            if should_recurse:
                nested_results, nested_skipped = await self._explore_state(
                    page,
                    root_url,
                    source_route,
                    state_depth + 1,
                    api_calls,
                    bd,
                    seen_states,
                    seen_actions,
                )
                results.extend(nested_results)
                skipped_total += nested_skipped

            if needs_reset:
                if not await self._restore_root(page, root_url):
                    break

        return results, skipped_total

    async def _click_all(
        self, page, root_url: str, api_calls: list, bd: BreakDetector,
    ) -> tuple[list[dict], int]:
        route = canonicalize_path_from_url(root_url)
        seen_states: set[str] = set()
        seen_actions: set[str] = set()
        return await self._explore_state(
            page,
            root_url,
            route,
            0,
            api_calls,
            bd,
            seen_states,
            seen_actions,
        )

    async def explore_page(self, context, url: str, retry_attempt: int = 0) -> dict[str, Any]:
        api_calls: list[dict] = []
        js_errors: list[dict] = []
        net_fails: list[dict] = []
        http_errs: list[dict] = []
        bd = BreakDetector()

        page = await context.new_page()
        self.api.attach(page, api_calls)
        bd.attach(page)
        page.on("console",       lambda m: js_errors.append({"type": m.type, "text": m.text})
                                           if m.type in ("error", "warning") else None)
        page.on("requestfailed", lambda r: net_fails.append({"url": r.url, "failure": str(r.failure)}))
        page.on("response",      lambda r: http_errs.append({"url": r.url, "status": r.status})
                                           if r.status >= 400 else None)

        started = time.time()
        status = 0; title = ""
        shot: dict[str, Any] = {"path": "", "b64": "", "diff": None}

        try:
            resp = await page.goto(url, wait_until="networkidle", timeout=30000)
            status = int(resp.status) if resp else 0
            await enable_dom_mutation_watcher(page)
            await asyncio.sleep(1.0)
            title = await page.title()
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.7)
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.4)
        except Exception as e:
            await page.close()
            return self._error_result(url, retry_attempt, str(e), time.time() - started)

        shot = await self._shot(page, url)
        route = canonicalize_path_from_url(url)
        root_state = await _state_snapshot(page, route, 0)
        discovered = await self._record_links(route, await self._discover_links(page))

        if self.kb:
            self.kb.mark_url_visited(url)

        is_exhausted = self.kb.is_route_exhausted(route) if self.kb else False

        interactions, n_skipped = await self._click_all(page, url, api_calls, bd)
        post_shot = await self._shot(page, url, suffix="post")

        perf    = await collect_perf_metrics(page)
        dom_mut = await read_dom_mutations(page)
        try:
            missing_alt = await page.eval_on_selector_all("img:not([alt])", "els => els.map(e => e.src)")
        except Exception:
            missing_alt = []

        await page.close()

        filtered_errs = self.state.register_errors(url, js_errors)
        failed_apis   = [c for c in api_calls if c.get("failed")]
        for c in api_calls:
            self.state.api_log.append(c)

        n_new = sum(1 for r in interactions if r.get("is_new"))
        broken = sum(1 for r in interactions if r.get("broke"))
        discovered_all = list(dict.fromkeys(
            list(discovered) + [
                url
                for interaction in interactions
                for url in interaction.get("discovered_urls", [])
                if url
            ]
        ))
        state_ids = {root_state["state_id"]}
        for interaction in interactions:
            if interaction.get("from_state_id"):
                state_ids.add(interaction["from_state_id"])
            if interaction.get("to_state_id"):
                state_ids.add(interaction["to_state_id"])
        state_transitions = sum(
            1 for interaction in interactions
            if interaction.get("state_changed") or interaction.get("from_route") != interaction.get("to_route")
        )

        return {
            "url": url, "title": title, "http_status": status,
            "load_time_ms": round((time.time() - started) * 1000),
            "links_found": len(discovered_all),
            "js_errors": filtered_errs[:10],
            "network_failures": net_fails[:10],
            "http_errors": http_errs[:10],
            "images_missing_alt": missing_alt[:5],
            "screenshot": shot["path"], "screenshot_b64": shot["b64"],
            "screenshot_post": post_shot["path"], "screenshot_diff": shot["diff"],
            "retried": retry_attempt > 0, "retry_attempt": retry_attempt,
            "perf_metrics": perf, "dom_mutations": dom_mut,
            "api_calls": api_calls[:50], "failed_apis": failed_apis[:10],
            "api_failures": len(failed_apis),
            "interaction_results": interactions[:80],
            "broken_interactions": broken,
            "root_state": root_state,
            "states_seen": len(state_ids),
            "state_transitions": state_transitions,
            # Learning metadata — shown in dashboard Explorer tab
            "n_new_elements":     n_new,
            "n_skipped_elements": n_skipped,
            "route_exhausted":    is_exhausted,
            "discovered_links":   discovered_all,
        }

    def _error_result(self, url: str, attempt: int, err: str, elapsed: float) -> dict:
        return {
            "url": url, "title": "", "http_status": 0,
            "load_time_ms": round(elapsed * 1000),
            "js_errors": [], "network_failures": [{"url": url, "failure": err}],
            "http_errors": [], "images_missing_alt": [],
            "screenshot": "", "screenshot_b64": "", "screenshot_post": "",
            "screenshot_diff": None, "retried": attempt > 0, "retry_attempt": attempt,
            "perf_metrics": {}, "dom_mutations": {},
            "api_calls": [], "failed_apis": [], "api_failures": 0,
            "interaction_results": [], "broken_interactions": 0, "links_found": 0,
            "root_state": {}, "states_seen": 0, "state_transitions": 0,
            "n_new_elements": 0, "n_skipped_elements": 0, "route_exhausted": False,
            "discovered_links": [],
        }
