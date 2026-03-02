from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from playwright.async_api import async_playwright

from .auth import check_session_alive, do_login, load_session_context, save_session
from .config import Settings
from .knowledge import KnowledgeBase
from .utils import canonicalize_path_from_url, clean_url, same_origin
from .workflows import Scenario, StepType, WorkflowStep, upsert_scenario


RECORDER_INIT_SCRIPT = r"""
(() => {
  if (window.__qaRecorderInstalled__) return;
  window.__qaRecorderInstalled__ = true;

  const MODE_KEY = "__qa_recorder_mode";
  const BADGE_ID = "__qa_recorder_badge";
  const MODE_EVENT = "qa-recorder-mode-changed";
  let lastMode = "";
  let lastUrl = window.location.href;
  let lastSnapshotFingerprint = "";
  let mutationTimer = 0;

  const currentMode = () => {
    try {
      return window.localStorage.getItem(MODE_KEY) || "";
    } catch (e) {
      return "";
    }
  };
  const isEnabled = () => !!currentMode();
  const isGuided = () => currentMode() === "guided_tour";

  const normalize = (value, maxLen = 120) => {
    return String(value || "").replace(/\s+/g, " ").trim().slice(0, maxLen);
  };

  const quote = (value) => JSON.stringify(String(value));

  const canUseSelector = (selector) => {
    if (!selector) return false;
    try {
      return document.querySelectorAll(selector).length === 1;
    } catch (e) {
      return false;
    }
  };

  const candidateFromAttr = (tag, name, value) => {
    if (!value) return "";
    const selector = `${tag}[${name}=${quote(value)}]`;
    return canUseSelector(selector) ? selector : "";
  };

  const buildFallbackSelector = (element) => {
    const parts = [];
    let current = element;

    while (current && current.nodeType === 1 && parts.length < 6) {
      const tag = current.tagName.toLowerCase();
      let part = tag;

      if (current.id) {
        const idSelector = `#${CSS.escape(current.id)}`;
        if (canUseSelector(idSelector)) return idSelector;
        part += idSelector;
      } else {
        const classNames = Array.from(current.classList || [])
          .filter((cls) => cls && cls.length <= 40)
          .slice(0, 2);
        if (classNames.length) {
          part += classNames.map((cls) => `.${CSS.escape(cls)}`).join("");
        }
      }

      const parent = current.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(
          (child) => child.tagName === current.tagName
        );
        if (siblings.length > 1) {
          part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
        }
      }

      parts.unshift(part);
      const selector = parts.join(" > ");
      if (canUseSelector(selector)) return selector;
      current = parent;
    }

    return parts.join(" > ");
  };

  const selectorFor = (input) => {
    if (!(input instanceof Element)) return "";
    const element =
      input.closest(
        'button,a,summary,input,textarea,select,[role="button"],[data-testid],[data-test],[data-cy],[data-qa],[aria-label]'
      ) || input;
    const tag = element.tagName.toLowerCase();

    const testIdAttrs = ["data-testid", "data-test", "data-cy", "data-qa"];
    for (const attr of testIdAttrs) {
      const value = element.getAttribute(attr);
      if (value) {
        const selector = `[${attr}=${quote(value)}]`;
        if (canUseSelector(selector)) return selector;
      }
    }

    if (element.id) {
      const selector = `#${CSS.escape(element.id)}`;
      if (canUseSelector(selector)) return selector;
    }

    const attrSelectors = [
      candidateFromAttr(tag, "name", element.getAttribute("name")),
      candidateFromAttr(tag, "aria-label", element.getAttribute("aria-label")),
      candidateFromAttr(tag, "placeholder", element.getAttribute("placeholder")),
      candidateFromAttr(tag, "type", element.getAttribute("type")),
    ].filter(Boolean);
    if (attrSelectors.length) return attrSelectors[0];

    const text = normalize(
      element.innerText || element.textContent || element.value || "",
      50
    );
    if (text && ["button", "a", "label", "summary"].includes(tag)) {
      return `${tag}:has-text(${quote(text)})`;
    }

    return buildFallbackSelector(element);
  };

  const describe = (element) => {
    return normalize(
      element.getAttribute("aria-label") ||
        element.innerText ||
        element.textContent ||
        element.value ||
        element.getAttribute("placeholder") ||
        element.getAttribute("name") ||
        element.id ||
        element.tagName.toLowerCase()
    );
  };

  const visible = (element) =>
    !!(
      element &&
      element.isConnected &&
      (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
    );

  const collectText = (selector, limit = 5) =>
    Array.from(document.querySelectorAll(selector))
      .filter(visible)
      .map((node) =>
        normalize(
          node.innerText ||
            node.textContent ||
            node.getAttribute?.("aria-label") ||
            node.getAttribute?.("data-testid") ||
            "",
          60
        )
      )
      .filter(Boolean)
      .slice(0, limit);

  const closestText = (element, selector) => {
    const node = element.closest(selector);
    return normalize(
      node?.innerText ||
        node?.textContent ||
        node?.getAttribute?.("aria-label") ||
        node?.getAttribute?.("data-testid") ||
        "",
      80
    );
  };

  const pageSnapshot = () => {
    const headings = collectText(
      "main h1, main h2, main h3, [role='main'] h1, [role='main'] h2, [role='main'] h3, section h2, section h3, form legend",
      6
    );
    const activeTabs = collectText(
      "[role='tab'][aria-selected='true'], [role='tab'][aria-current='page'], [aria-current='page'], [aria-pressed='true']",
      4
    );
    const expanded = collectText("[aria-expanded='true']", 4);
    const modal = document.querySelector(
      "[role='dialog'], [aria-modal='true'], .modal, [class*='dialog']"
    );
    const modalTitle = normalize(
      modal?.querySelector?.("h1,h2,h3,[role='heading']")?.innerText ||
        modal?.getAttribute?.("aria-label") ||
        modal?.innerText ||
        "",
      80
    );
    const pageLabel =
      headings[0] ||
      normalize(
        document.querySelector("h1,h2,[role='heading']")?.innerText ||
          document.title ||
          "",
        80
      );
    const visibleLinks = Array.from(document.querySelectorAll("a[href]")).filter(visible).length;
    const visibleClickables = Array.from(
      document.querySelectorAll(
        "button,[role='button'],summary,[role='tab'],[role='link'],[aria-haspopup],[aria-expanded]"
      )
    ).filter(visible).length;
    const visibleInputs = Array.from(
      document.querySelectorAll(
        "input:not([type='hidden']),textarea,select,[role='combobox']"
      )
    ).filter(visible).length;
    const fingerprint = JSON.stringify([
      window.location.pathname,
      pageLabel,
      modalTitle,
      visibleLinks,
      visibleClickables,
      visibleInputs,
      headings.join("|"),
      activeTabs.join("|"),
      expanded.join("|"),
    ]);
    return {
      title: normalize(document.title || "", 140),
      page_label: pageLabel,
      headings,
      active_tabs: activeTabs,
      expanded,
      modal_title: modalTitle,
      visible_links: visibleLinks,
      visible_clickables: visibleClickables,
      visible_inputs: visibleInputs,
      fingerprint,
    };
  };

  const elementContext = (element) => {
    const labelNode =
      element.labels && element.labels.length ? element.labels[0] : null;
    return {
      section: closestText(
        element,
        "section,article,[role='region'],[data-section],[data-testid*='section']"
      ),
      modal: closestText(
        element,
        "[role='dialog'],[aria-modal='true'],.modal,[class*='dialog']"
      ),
      tab: closestText(element, "[role='tabpanel'],[data-state],[class*='tab-panel']"),
      form: closestText(element, "form"),
      chrome: closestText(
        element,
        "nav,header,aside,[role='navigation'],[role='menubar'],[role='tablist'],[class*='sidebar'],[class*='menu'],[data-testid*='nav'],[data-testid*='menu']"
      ),
      field_label: normalize(
        labelNode?.innerText ||
          labelNode?.textContent ||
          element.getAttribute?.("aria-label") ||
          "",
        80
      ),
      field_type: normalize(
        element.getAttribute?.("type") || element.tagName?.toLowerCase() || "",
        24
      ),
    };
  };

  const scopeDetails = (ctx) => {
    if (ctx.modal) return { scope_kind: "modal", scope_label: ctx.modal };
    if (ctx.chrome) return { scope_kind: "chrome", scope_label: ctx.chrome };
    if (ctx.tab) return { scope_kind: "tab", scope_label: ctx.tab };
    if (ctx.form) return { scope_kind: "form", scope_label: ctx.form };
    if (ctx.section) return { scope_kind: "section", scope_label: ctx.section };
    return { scope_kind: "page", scope_label: "" };
  };

  const send = (payload) => {
    if (!isEnabled()) return;
    if (typeof window.__qaRecorderPush !== "function") return;
    try {
      window.__qaRecorderPush(payload);
    } catch (e) {}
  };

  const renderBadge = () => {
    const root = document.body || document.documentElement;
    if (!root) return;

    let badge = document.getElementById(BADGE_ID);
    if (!badge) {
      badge = document.createElement("div");
      badge.id = BADGE_ID;
      badge.setAttribute("data-qa-recorder-ignore", "1");
      badge.style.cssText = [
        "position:fixed",
        "top:16px",
        "right:16px",
        "z-index:2147483647",
        "padding:8px 12px",
        "border-radius:999px",
        "background:#b91c1c",
        "color:#fff",
        "font:600 12px/1 sans-serif",
        "letter-spacing:.04em",
        "box-shadow:0 10px 30px rgba(0,0,0,.25)",
        "display:none",
        "pointer-events:none"
      ].join(";");
      root.appendChild(badge);
    }

    const mode = currentMode();
    if (mode) {
      badge.textContent =
        mode === "guided_tour" ? "Recording guided tour" : "Recording workflow";
      badge.style.display = "block";
    } else {
      badge.style.display = "none";
    }
  };

  const emitSnapshot = (kind, reason, extras = {}) => {
    if (!isEnabled()) return;
    const snapshot = pageSnapshot();
    if (kind === "state" && snapshot.fingerprint === lastSnapshotFingerprint) {
      return;
    }
    lastSnapshotFingerprint = snapshot.fingerprint;
    send({
      kind,
      reason,
      mode: currentMode(),
      url: window.location.href,
      title: snapshot.title,
      page_label: snapshot.page_label,
      snapshot,
      timestamp: Date.now(),
      ...extras,
    });
  };

  const syncMode = () => {
    const mode = currentMode();
    renderBadge();
    if (mode && mode !== lastMode) {
      lastSnapshotFingerprint = "";
      lastUrl = window.location.href;
      emitSnapshot("page", "mode_start");
    }
    lastMode = mode;
  };

  const emitRoute = (reason) => {
    if (!isEnabled()) return;
    const currentUrl = window.location.href;
    if (currentUrl === lastUrl && reason !== "load") return;
    const fromUrl = lastUrl;
    lastUrl = currentUrl;
    emitSnapshot("route", reason, { from_url: fromUrl, to_url: currentUrl });
  };

  const scheduleState = (reason) => {
    if (!isGuided()) return;
    window.clearTimeout(mutationTimer);
    mutationTimer = window.setTimeout(() => emitSnapshot("state", reason), 360);
  };

  const wrapHistory = (method) => {
    const original = window.history[method];
    if (typeof original !== "function") return;
    window.history[method] = function (...args) {
      const result = original.apply(this, args);
      window.setTimeout(() => emitRoute(method), 120);
      return result;
    };
  };

  wrapHistory("pushState");
  wrapHistory("replaceState");

  document.addEventListener(
    "click",
    (event) => {
      if (!isEnabled()) return;
      const target = event.target instanceof Element ? event.target : null;
      if (!target || target.closest("[data-qa-recorder-ignore]")) return;

      const actionEl =
        target.closest(
          'button,a,summary,[role="button"],[data-testid],[data-test],[data-cy],[data-qa],input[type="checkbox"],input[type="radio"],input[type="submit"],input[type="button"]'
      );
      if (!actionEl) return;
      const ctx = elementContext(actionEl);
      const scope = scopeDetails(ctx);
      const snapshot = pageSnapshot();

      send({
        kind: "click",
        action_kind: "click",
        selector: selectorFor(actionEl),
        description: describe(actionEl),
        label: describe(actionEl),
        text: normalize(actionEl.innerText || actionEl.textContent || actionEl.value || "", 80),
        tag: actionEl.tagName.toLowerCase(),
        role: normalize(actionEl.getAttribute("role") || "", 32),
        scope_kind: scope.scope_kind,
        scope_label: scope.scope_label,
        field_type: normalize(ctx.field_type || "", 24),
        page_label: snapshot.page_label,
        url: window.location.href,
        title: snapshot.title,
        timestamp: Date.now()
      });
    },
    true
  );

  document.addEventListener(
    "change",
    (event) => {
      if (!isEnabled()) return;
      const element = event.target instanceof Element ? event.target : null;
      if (!element || element.closest("[data-qa-recorder-ignore]")) return;

      const tag = element.tagName.toLowerCase();
      if (!["input", "textarea", "select"].includes(tag)) return;

      const inputType = String(element.getAttribute("type") || "").toLowerCase();
      if (["password", "hidden", "checkbox", "radio", "submit", "button", "reset"].includes(inputType)) {
        return;
      }

      let kind = "fill";
      let value = element.value || "";
      let valuePreview = "__entered__";
      let selectedLabel = "";

      if (tag === "select") {
        kind = "select";
        selectedLabel = normalize(
          element.options?.[element.selectedIndex]?.text ||
            element.selectedOptions?.[0]?.text ||
            "",
          80
        );
        valuePreview = selectedLabel || normalize(value, 80);
      } else if (inputType === "file") {
        kind = "upload";
        value = "__qa_test_file__";
        valuePreview = "__uploaded__";
      }
      const ctx = elementContext(element);
      const scope = scopeDetails(ctx);
      const snapshot = pageSnapshot();

      send({
        kind,
        action_kind: kind,
        selector: selectorFor(element),
        description: describe(element),
        value,
        value_preview: valuePreview,
        selected_label: selectedLabel,
        field_type: normalize(ctx.field_type || inputType || tag, 24),
        scope_kind: scope.scope_kind,
        scope_label: scope.scope_label,
        page_label: snapshot.page_label,
        url: window.location.href,
        title: snapshot.title,
        timestamp: Date.now()
      });
    },
    true
  );

  const observer = new MutationObserver((mutations) => {
    if (!isGuided()) return;
    for (const mutation of mutations) {
      if (mutation.type === "childList" || mutation.type === "attributes") {
        scheduleState("mutation");
        return;
      }
    }
  });

  if (document.documentElement) {
    observer.observe(document.documentElement, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: [
        "class",
        "open",
        "aria-expanded",
        "aria-hidden",
        "aria-selected",
        "aria-current",
        "data-state",
        "style",
        "value",
      ],
    });
  }

  window.addEventListener("storage", syncMode);
  window.addEventListener("focus", syncMode);
  window.addEventListener("popstate", () => window.setTimeout(() => emitRoute("popstate"), 120));
  window.addEventListener("hashchange", () => window.setTimeout(() => emitRoute("hashchange"), 120));
  window.addEventListener(MODE_EVENT, syncMode);
  document.addEventListener("readystatechange", syncMode);
  window.addEventListener("load", () => window.setTimeout(() => emitSnapshot("page", "load"), 60));
  window.setTimeout(syncMode, 0);
})();
"""


def _slugify_recording_name(value: str, prefix: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or f"{prefix}_{int(time.time())}"


async def _prompt(text: str, default: str = "") -> str:
    label = f"{text}"
    if default:
        label += f" [{default}]"
    answer = await asyncio.to_thread(input, f"{label}: ")
    answer = answer.strip()
    return answer or default


async def _pause(text: str) -> None:
    await asyncio.to_thread(input, f"{text}")


async def _prompt_bool(text: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = await asyncio.to_thread(input, f"{text} [{suffix}]: ")
    answer = answer.strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes", "1", "true"}


async def _set_recorder_mode(page, mode: str = "") -> None:
    mode_js = repr(mode)
    await page.evaluate(
        f"""() => {{
            try {{
                const key = "__qa_recorder_mode";
                if ({mode_js}) {{
                    window.localStorage.setItem(key, {mode_js});
                }} else {{
                    window.localStorage.removeItem(key);
                }}
                window.dispatchEvent(new Event("qa-recorder-mode-changed"));
            }} catch (e) {{}}
        }}"""
    )


async def _ensure_authenticated_context(browser, settings: Settings):
    context, already_logged = await load_session_context(browser, settings)
    start_path = settings.start_path if settings.start_path.startswith("/") else f"/{settings.start_path}"
    start_url = f"{settings.base_url}{start_path}"

    if not already_logged:
        page = await context.new_page()
        print("[RECORDER] Login required. Complete auth in the browser window.")
        ok = await do_login(page, settings)
        if not ok:
            await page.close()
            await context.close()
            raise RuntimeError("Login was not completed within timeout.")
        await save_session(context, settings.session_file)
        start_url = clean_url(page.url) or start_url
        await page.close()
        return context, start_url

    test_page = await context.new_page()
    try:
        await test_page.goto(start_url, wait_until="networkidle", timeout=20000)
    except Exception as nav_err:
        await test_page.close()
        err_str = str(nav_err)
        if any(
            marker in err_str
            for marker in ("ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION_REFUSED", "ERR_INTERNET_DISCONNECTED")
        ):
            raise RuntimeError(
                f"Cannot reach {start_url}. Set QA_BASE_URL in .env. Current value: {settings.base_url}"
            ) from None
        await context.close()
        context, _ = await load_session_context(browser, settings)
        page = await context.new_page()
        print("[RECORDER] Session expired. Complete auth in the browser window.")
        ok = await do_login(page, settings)
        if not ok:
            await page.close()
            await context.close()
            raise RuntimeError("Login was not completed within timeout.")
        await save_session(context, settings.session_file)
        start_url = clean_url(page.url) or start_url
        await page.close()
        return context, start_url

    if not await check_session_alive(test_page, settings):
        await test_page.close()
        await context.close()
        context, _ = await load_session_context(browser, settings)
        page = await context.new_page()
        print("[RECORDER] Session needs re-authentication. Complete auth in the browser window.")
        ok = await do_login(page, settings)
        if not ok:
            await page.close()
            await context.close()
            raise RuntimeError("Re-authentication failed.")
        await save_session(context, settings.session_file)
        start_url = clean_url(page.url) or start_url
        await page.close()
        return context, start_url

    current_url = clean_url(test_page.url) or start_url
    await test_page.close()
    return context, current_url


class WorkflowRecorder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.start_url = ""
        self.steps: list[WorkflowStep] = []
        self.recording = False
        self._last_event_key = ""
        self._last_event_time = 0.0

    def begin(self, start_url: str) -> None:
        self.start_url = clean_url(start_url)
        self.steps.clear()
        self.recording = True
        self._last_event_key = ""
        self._last_event_time = 0.0

    def stop(self) -> None:
        self.recording = False

    def _make_step(self, payload: dict[str, Any]) -> WorkflowStep | None:
        kind = str(payload.get("kind", "")).strip().lower()
        selector = str(payload.get("selector", "")).strip()
        label = str(payload.get("description") or payload.get("text") or selector).strip()
        value = str(payload.get("value", ""))

        if not selector:
            return None

        if kind == "click":
            return WorkflowStep(
                StepType.CLICK,
                selector=selector,
                description=f"Click {label}" if label else "Click element",
            )
        if kind == "fill":
            return WorkflowStep(
                StepType.FILL,
                selector=selector,
                value=value,
                description=f"Fill {label}" if label else "Fill field",
            )
        if kind == "select":
            return WorkflowStep(
                StepType.SELECT,
                selector=selector,
                value=value,
                description=f"Select {label}" if label else "Select option",
            )
        if kind == "upload":
            return WorkflowStep(
                StepType.UPLOAD,
                selector=selector,
                value=value or "__qa_test_file__",
                description=f"Upload using {label}" if label else "Upload file",
            )
        return None

    def _append_step(self, step: WorkflowStep, payload: dict[str, Any]) -> None:
        event_time = float(payload.get("timestamp", 0) or 0) / 1000.0 or time.time()
        event_key = f"{step.type.value}|{step.selector}|{step.value}"
        if event_key == self._last_event_key and event_time - self._last_event_time < 0.75:
            return

        if (
            self.steps
            and step.type in (StepType.FILL, StepType.SELECT)
            and self.steps[-1].type == step.type
            and self.steps[-1].selector == step.selector
        ):
            self.steps[-1] = step
        else:
            self.steps.append(step)

        self._last_event_key = event_key
        self._last_event_time = event_time
        print(f"[RECORDER] {len(self.steps):02d}. {step.type.value:<6} {step.description}")

    async def capture_binding(self, source: Any, payload: Any) -> None:
        del source
        if not self.recording or not isinstance(payload, dict):
            return
        step = self._make_step(payload)
        if not step:
            return
        self._append_step(step, payload)

    def build_scenario(
        self,
        name: str,
        critical: bool,
        dom_assertion: str = "",
        api_endpoint: str = "",
        api_method: str = "POST",
    ) -> Scenario:
        scenario = Scenario(
            name=name,
            route=canonicalize_path_from_url(self.start_url or self.settings.base_url),
            steps=list(self.steps),
            critical=critical,
            description=f"Recorded workflow for {canonicalize_path_from_url(self.start_url or self.settings.base_url)}",
            start_url=self.start_url,
        )
        if api_endpoint:
            scenario.steps.append(
                WorkflowStep(
                    StepType.ASSERT_API,
                    api_endpoint=api_endpoint,
                    api_method=api_method or "POST",
                    description=f"Assert API call {api_method or 'POST'} {api_endpoint}",
                )
            )
        if dom_assertion:
            scenario.steps.append(
                WorkflowStep(
                    StepType.ASSERT_BUSINESS,
                    assertion=dom_assertion,
                    description=f"Assert business outcome {dom_assertion}",
                )
            )
        return scenario


class GuidedTourRecorder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.start_url = ""
        self.end_url = ""
        self.events: list[dict[str, Any]] = []
        self.recording = False
        self._last_event_key = ""
        self._last_event_time = 0.0

    def begin(self, start_url: str) -> None:
        self.start_url = clean_url(start_url)
        self.end_url = self.start_url
        self.events.clear()
        self.recording = True
        self._last_event_key = ""
        self._last_event_time = 0.0

    def stop(self, end_url: str = "") -> None:
        self.recording = False
        self.end_url = clean_url(end_url) or self.end_url or self.start_url

    def _event_time(self, payload: dict[str, Any]) -> float:
        raw = float(payload.get("timestamp", 0) or 0)
        return raw / 1000.0 if raw else time.time()

    def _sanitize_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
        if not snapshot:
            return {}
        return {
            "page_label": str(payload.get("page_label", "") or snapshot.get("page_label", "") or ""),
            "title": str(payload.get("title", "") or snapshot.get("title", "") or ""),
            "headings": [str(v) for v in snapshot.get("headings", [])[:6]],
            "active_tabs": [str(v) for v in snapshot.get("active_tabs", [])[:4]],
            "expanded": [str(v) for v in snapshot.get("expanded", [])[:4]],
            "modal_title": str(snapshot.get("modal_title", "") or ""),
            "visible_links": int(snapshot.get("visible_links", 0) or 0),
            "visible_clickables": int(snapshot.get("visible_clickables", 0) or 0),
            "visible_inputs": int(snapshot.get("visible_inputs", 0) or 0),
            "fingerprint": str(snapshot.get("fingerprint", "") or ""),
        }

    def _normalize_event(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        kind = str(payload.get("kind", "") or "").strip().lower()
        if kind not in {"click", "fill", "select", "upload", "page", "route", "state"}:
            return None

        event_time = self._event_time(payload)
        url = clean_url(str(payload.get("url", "") or payload.get("to_url", "") or ""))
        route = canonicalize_path_from_url(url) if url else ""
        title = str(payload.get("title", "") or "").strip()
        page_label = str(payload.get("page_label", "") or "").strip()
        snapshot = self._sanitize_snapshot(payload)
        if snapshot.get("page_label") and not page_label:
            page_label = snapshot["page_label"]
        if snapshot.get("title") and not title:
            title = snapshot["title"]

        event: dict[str, Any] = {
            "kind": kind,
            "timestamp": round(event_time, 3),
        }
        if url:
            event["url"] = url
        if route:
            event["route"] = route
        if title:
            event["title"] = title
        if page_label:
            event["page_label"] = page_label
        if snapshot:
            event["snapshot"] = snapshot

        if kind == "route":
            from_url = clean_url(str(payload.get("from_url", "") or ""))
            to_url = clean_url(str(payload.get("to_url", "") or url or ""))
            if from_url:
                event["from_url"] = from_url
            if to_url:
                event["to_url"] = to_url
                event["url"] = to_url
                event["route"] = canonicalize_path_from_url(to_url)
        elif kind in {"click", "fill", "select", "upload"}:
            label = str(
                payload.get("label")
                or payload.get("description")
                or payload.get("text")
                or payload.get("selected_label")
                or payload.get("selector")
                or ""
            ).strip()
            selector = str(payload.get("selector", "") or "").strip()
            if not label and not selector:
                return None
            event.update({
                "action_kind": str(payload.get("action_kind", "") or kind),
                "selector": selector,
                "label": label,
                "scope_kind": str(payload.get("scope_kind", "") or ""),
                "scope_label": str(payload.get("scope_label", "") or ""),
                "field_type": str(payload.get("field_type", "") or ""),
                "tag": str(payload.get("tag", "") or ""),
                "role": str(payload.get("role", "") or ""),
            })
            if kind == "select":
                preview = str(payload.get("selected_label", "") or payload.get("value_preview", "") or "__selected__")
                event["value_preview"] = preview[:80]
            elif kind == "upload":
                event["value_preview"] = "__uploaded__"
            elif kind == "fill":
                event["value_preview"] = "__entered__"

        reason = str(payload.get("reason", "") or "").strip()
        if reason:
            event["reason"] = reason
        return event

    def _append_event(self, event: dict[str, Any]) -> None:
        event_time = float(event.get("timestamp", 0) or time.time())
        snapshot_fp = str((event.get("snapshot") or {}).get("fingerprint", "") or "")
        event_key = "|".join([
            str(event.get("kind", "")),
            str(event.get("route", "")),
            str(event.get("url", "")),
            str(event.get("selector", "")),
            str(event.get("action_kind", "")),
            snapshot_fp,
        ])
        if event_key == self._last_event_key and event_time - self._last_event_time < 0.75:
            return
        self.events.append(event)
        self._last_event_key = event_key
        self._last_event_time = event_time

        kind = str(event.get("kind", "") or "")
        if kind in {"click", "fill", "select", "upload"}:
            detail = str(event.get("label", "") or event.get("selector", ""))
        elif kind == "route":
            detail = str(event.get("to_url", "") or event.get("url", ""))
        else:
            detail = str(event.get("page_label", "") or event.get("route", "") or event.get("url", ""))
        print(f"[GUIDED] {len(self.events):02d}. {kind:<6} {detail}")

    async def capture_binding(self, source: Any, payload: Any) -> None:
        del source
        if not self.recording or not isinstance(payload, dict):
            return
        event = self._normalize_event(payload)
        if not event:
            return
        self._append_event(event)

    def build_tour(self, name: str, label: str = "") -> dict[str, Any]:
        routes = sorted({
            str(event.get("route", "") or "")
            for event in self.events
            if event.get("route")
        })
        urls = sorted({
            str(event.get("url", "") or event.get("to_url", "") or "")
            for event in self.events
            if event.get("url") or event.get("to_url")
        })
        end_url = self.end_url or (urls[-1] if urls else self.start_url)
        return {
            "name": name,
            "label": label or name,
            "started_at": self.events[0]["timestamp"] if self.events else time.time(),
            "finished_at": self.events[-1]["timestamp"] if self.events else time.time(),
            "start_url": self.start_url,
            "end_url": end_url,
            "routes": routes,
            "urls": urls,
            "events": list(self.events),
        }


async def record_workflow_session(settings: Settings, workflow_name: str = "") -> dict[str, Any]:
    default_name = workflow_name or f"workflow_{int(time.time())}"
    entered_name = workflow_name or await _prompt("Workflow name", default=default_name)
    name = _slugify_recording_name(entered_name, "workflow")
    critical = await _prompt_bool("Count this workflow in coverage metrics", default=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=40)
        context = None
        try:
            context, ready_url = await _ensure_authenticated_context(browser, settings)
            recorder = WorkflowRecorder(settings)
            await context.expose_binding("__qaRecorderPush", recorder.capture_binding)
            await context.add_init_script(RECORDER_INIT_SCRIPT)

            page = await context.new_page()
            await page.goto(ready_url, wait_until="networkidle", timeout=20000)

            print("[RECORDER] Browser ready.")
            print("[RECORDER] Navigate to the page where this workflow should start.")
            await _pause("Press Enter here when the browser is on the correct start page...")

            start_url = clean_url(page.url)
            if not same_origin(start_url, settings.base_url):
                raise RuntimeError(
                    f"Start page must stay on {settings.base_url}. Current page: {start_url or '(blank)'}"
                )

            recorder.begin(start_url)
            await _set_recorder_mode(page, "workflow")
            print(f"[RECORDER] Recording from {start_url}")
            print("[RECORDER] Perform the workflow in the browser. Every click, field change, and file input will be captured.")
            await _pause("Press Enter here when you want to stop recording and save...")

            recorder.stop()
            await _set_recorder_mode(page, "")

            if not recorder.steps:
                raise RuntimeError("No workflow steps were captured.")

            dom_assertion = await _prompt("Success selector or visible text (optional)", default="")
            api_endpoint = await _prompt("API endpoint substring to assert (optional)", default="")
            api_method = "POST"
            if api_endpoint:
                api_method = (await _prompt("API method", default="POST")).upper()

            scenario = recorder.build_scenario(
                name=name,
                critical=critical,
                dom_assertion=dom_assertion,
                api_endpoint=api_endpoint,
                api_method=api_method,
            )
            replaced = upsert_scenario(settings.workflows_file, scenario)

            await page.close()
            await context.close()
            await browser.close()
            return {
                "name": scenario.name,
                "route": scenario.route,
                "start_url": scenario.start_url,
                "steps": len(scenario.steps),
                "replaced": replaced,
                "workflows_file": settings.workflows_file,
            }
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            try:
                await browser.close()
            except Exception:
                pass


async def record_guided_tour_session(settings: Settings, tour_name: str = "") -> dict[str, Any]:
    default_name = tour_name or f"guided_tour_{int(time.time())}"
    entered_name = tour_name or await _prompt("Guided tour name", default=default_name)
    name = _slugify_recording_name(entered_name, "guided_tour")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=40)
        context = None
        try:
            context, ready_url = await _ensure_authenticated_context(browser, settings)
            recorder = GuidedTourRecorder(settings)
            await context.expose_binding("__qaRecorderPush", recorder.capture_binding)
            await context.add_init_script(RECORDER_INIT_SCRIPT)

            page = await context.new_page()
            await page.goto(ready_url, wait_until="networkidle", timeout=20000)

            print("[GUIDED] Browser ready.")
            print("[GUIDED] Navigate to the page where this guided tour should start.")
            await _pause("Press Enter here when the browser is on the correct start page...")

            start_url = clean_url(page.url)
            if not same_origin(start_url, settings.base_url):
                raise RuntimeError(
                    f"Start page must stay on {settings.base_url}. Current page: {start_url or '(blank)'}"
                )

            recorder.begin(start_url)
            await _set_recorder_mode(page, "guided_tour")
            await asyncio.sleep(0.35)
            print(f"[GUIDED] Recording from {start_url}")
            print("[GUIDED] Drive the product normally. Routes, clicks, field changes, and page-state snapshots will be captured.")
            await _pause("Press Enter here when you want to stop recording and seed the knowledge base...")

            recorder.stop(clean_url(page.url))
            await _set_recorder_mode(page, "")

            if not recorder.events:
                raise RuntimeError("No guided-tour events were captured.")

            kb = KnowledgeBase(settings.knowledge_file)
            kb.load()
            seed_result = kb.seed_guided_tour(recorder.build_tour(name=name, label=entered_name))
            kb.save(increment_run=False)

            await page.close()
            await context.close()
            await browser.close()
            return {
                "name": name,
                "start_url": recorder.start_url,
                "end_url": recorder.end_url,
                "events": seed_result["events"],
                "routes": seed_result["routes"],
                "urls": seed_result["urls"],
                "seeded_elements": seed_result["seeded_elements"],
                "seeded_transitions": seed_result["seeded_transitions"],
                "replaced": seed_result["replaced"],
                "knowledge_file": settings.knowledge_file,
            }
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            try:
                await browser.close()
            except Exception:
                pass
