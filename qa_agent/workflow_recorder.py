from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from playwright.async_api import async_playwright

from .auth import check_session_alive, do_login, load_session_context, save_session
from .config import Settings
from .utils import canonicalize_path_from_url, clean_url, same_origin
from .workflows import Scenario, StepType, WorkflowStep, upsert_scenario


RECORDER_INIT_SCRIPT = r"""
(() => {
  if (window.__qaRecorderInstalled__) return;
  window.__qaRecorderInstalled__ = true;

  const FLAG_KEY = "__qa_recorder_enabled";
  const BADGE_ID = "__qa_recorder_badge";

  const isEnabled = () => {
    try {
      return window.localStorage.getItem(FLAG_KEY) === "1";
    } catch (e) {
      return false;
    }
  };

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

    if (isEnabled()) {
      badge.textContent = "Recording workflow";
      badge.style.display = "block";
    } else {
      badge.style.display = "none";
    }
  };

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

      send({
        kind: "click",
        selector: selectorFor(actionEl),
        description: describe(actionEl),
        text: normalize(actionEl.innerText || actionEl.textContent || actionEl.value || "", 80),
        url: window.location.href,
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

      if (tag === "select") {
        kind = "select";
      } else if (inputType === "file") {
        kind = "upload";
        value = "__qa_test_file__";
      }

      send({
        kind,
        selector: selectorFor(element),
        description: describe(element),
        value,
        url: window.location.href,
        timestamp: Date.now()
      });
    },
    true
  );

  window.addEventListener("storage", renderBadge);
  window.addEventListener("focus", renderBadge);
  document.addEventListener("readystatechange", renderBadge);
  setTimeout(renderBadge, 0);
})();
"""


def _slugify_workflow_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or f"workflow_{int(time.time())}"


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


async def record_workflow_session(settings: Settings, workflow_name: str = "") -> dict[str, Any]:
    default_name = workflow_name or f"workflow_{int(time.time())}"
    entered_name = workflow_name or await _prompt("Workflow name", default=default_name)
    name = _slugify_workflow_name(entered_name)
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
            await page.evaluate(
                "() => { try { window.localStorage.setItem('__qa_recorder_enabled', '1'); } catch (e) {} }"
            )
            print(f"[RECORDER] Recording from {start_url}")
            print("[RECORDER] Perform the workflow in the browser. Every click, field change, and file input will be captured.")
            await _pause("Press Enter here when you want to stop recording and save...")

            recorder.stop()
            await page.evaluate(
                "() => { try { window.localStorage.removeItem('__qa_recorder_enabled'); } catch (e) {} }"
            )

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
