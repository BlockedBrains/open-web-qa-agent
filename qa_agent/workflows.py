"""
workflows.py — Multi-step scenario library for deep form, modal, and business-outcome testing.

Each Scenario is a sequence of Steps. A Step can:
  - fill a field
  - click a button / selector
  - assert DOM state
  - assert API call happened
  - assert business outcome via DOM or API
  - handle modals / drawers

LangGraph is used as the workflow engine: each scenario runs as a graph node
with checkpointing, retries, and branching built in.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .utils import canonicalize_path_from_url, clean_url

# ── Optional LangGraph import ──────────────────────────────────────────────
try:
    from langgraph.graph import StateGraph as LGStateGraph, END
    from langgraph.checkpoint.memory import MemorySaver
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False


class StepType(str, Enum):
    NAVIGATE = "navigate"
    FILL = "fill"
    CLICK = "click"
    SELECT = "select"
    UPLOAD = "upload"
    ASSERT_DOM = "assert_dom"
    ASSERT_API = "assert_api"
    ASSERT_BUSINESS = "assert_business"
    MODAL_OPEN = "modal_open"
    MODAL_CLOSE = "modal_close"
    WAIT = "wait"


@dataclass
class WorkflowStep:
    type: StepType
    selector: str = ""
    value: str = ""
    assertion: str = ""          # text/selector to assert present
    api_endpoint: str = ""       # substring match for API call
    api_method: str = "POST"
    timeout_ms: int = 5000
    optional: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "selector": self.selector,
            "value": self.value,
            "assertion": self.assertion,
            "api_endpoint": self.api_endpoint,
            "api_method": self.api_method,
            "timeout_ms": self.timeout_ms,
            "optional": self.optional,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowStep":
        return cls(
            type=StepType(str(data.get("type", StepType.CLICK.value))),
            selector=str(data.get("selector", "")),
            value=str(data.get("value", "")),
            assertion=str(data.get("assertion", "")),
            api_endpoint=str(data.get("api_endpoint", "")),
            api_method=str(data.get("api_method", "POST") or "POST"),
            timeout_ms=int(data.get("timeout_ms", 5000) or 5000),
            optional=bool(data.get("optional", False)),
            description=str(data.get("description", "")),
        )


@dataclass
class Scenario:
    name: str
    route: str                   # starting route
    steps: list[WorkflowStep] = field(default_factory=list)
    critical: bool = True        # counts toward coverage KPIs
    description: str = ""
    start_url: str = ""

    def add(self, step: WorkflowStep) -> "Scenario":
        self.steps.append(step)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "route": self.route,
            "critical": self.critical,
            "description": self.description,
            "start_url": self.start_url,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scenario":
        return cls(
            name=str(data.get("name", "")).strip() or "unnamed_workflow",
            route=str(data.get("route", "/dashboard") or "/dashboard"),
            steps=[WorkflowStep.from_dict(step) for step in data.get("steps", [])],
            critical=bool(data.get("critical", True)),
            description=str(data.get("description", "")),
            start_url=clean_url(str(data.get("start_url", "") or "")),
        )


# ── Built-in scenario library ──────────────────────────────────────────────

def create_project_scenario() -> Scenario:
    s = Scenario(
        name="create_project",
        route="/dashboard",
        description="Create a new project from the dashboard",
    )
    s.add(WorkflowStep(StepType.CLICK, selector='button:has-text("New Project"), button:has-text("Create Project"), [data-testid*="create"]', description="Open create project dialog"))
    s.add(WorkflowStep(StepType.MODAL_OPEN, assertion="[role=dialog], .modal, [data-testid*='modal']", description="Wait for modal"))
    s.add(WorkflowStep(StepType.FILL, selector='input[name="name"], input[placeholder*="project" i], input[placeholder*="name" i]', value="QA-AutoProject-{{ts}}", description="Fill project name"))
    s.add(WorkflowStep(StepType.FILL, selector='textarea[name="description"], textarea[placeholder*="description" i]', value="Auto-generated QA test project", optional=True))
    s.add(WorkflowStep(StepType.CLICK, selector='button[type="submit"], button:has-text("Create"), button:has-text("Save")', description="Submit form"))
    s.add(WorkflowStep(StepType.ASSERT_API, api_endpoint="/project", api_method="POST", description="API creates project"))
    s.add(WorkflowStep(StepType.ASSERT_BUSINESS, assertion="QA-AutoProject", description="Project name appears in DOM"))
    return s


def add_scene_scenario() -> Scenario:
    s = Scenario(
        name="add_scene",
        route="/project/:id",
        description="Add a scene inside an existing project",
    )
    s.add(WorkflowStep(StepType.CLICK, selector='button:has-text("Add Scene"), button:has-text("New Scene"), [data-testid*="scene"]'))
    s.add(WorkflowStep(StepType.MODAL_OPEN, assertion="[role=dialog], .modal"))
    s.add(WorkflowStep(StepType.FILL, selector='input[name="title"], input[placeholder*="scene" i]', value="QA-Scene-{{ts}}"))
    s.add(WorkflowStep(StepType.SELECT, selector='select[name="type"], [role=combobox]', value="0", optional=True))
    s.add(WorkflowStep(StepType.CLICK, selector='button[type="submit"], button:has-text("Save"), button:has-text("Add")'))
    s.add(WorkflowStep(StepType.ASSERT_API, api_endpoint="/scene", api_method="POST"))
    s.add(WorkflowStep(StepType.ASSERT_BUSINESS, assertion="QA-Scene"))
    return s


def invite_member_scenario() -> Scenario:
    s = Scenario(
        name="invite_member",
        route="/project/:id/settings",
        description="Invite a collaborator from project settings",
    )
    s.add(WorkflowStep(StepType.CLICK, selector='button:has-text("Invite"), button:has-text("Add Member")'))
    s.add(WorkflowStep(StepType.MODAL_OPEN, assertion="[role=dialog], .modal"))
    s.add(WorkflowStep(StepType.FILL, selector='input[type="email"]', value="qa-test-{{ts}}@test.invalid"))
    s.add(WorkflowStep(StepType.CLICK, selector='button[type="submit"], button:has-text("Send"), button:has-text("Invite")'))
    s.add(WorkflowStep(StepType.ASSERT_API, api_endpoint="/invite", api_method="POST"))
    return s


def upload_file_scenario() -> Scenario:
    s = Scenario(
        name="upload_file",
        route="/project/:id",
        description="Upload a file into an existing project",
    )
    s.add(WorkflowStep(StepType.CLICK, selector='button:has-text("Upload"), [data-testid*="upload"]', optional=True))
    s.add(WorkflowStep(StepType.UPLOAD, selector='input[type="file"]', value="__qa_test_file__", optional=True))
    s.add(WorkflowStep(StepType.ASSERT_API, api_endpoint="/upload", api_method="POST", optional=True))
    return s


def search_and_filter_scenario() -> Scenario:
    s = Scenario(
        name="search_filter",
        route="/dashboard",
        critical=False,
        description="Use dashboard search and verify result content renders",
    )
    s.add(WorkflowStep(StepType.FILL, selector='input[type="search"], input[placeholder*="search" i]', value="test"))
    s.add(WorkflowStep(StepType.ASSERT_DOM, assertion="[data-testid*='result'], .search-results, [class*='result']", optional=True))
    return s


def default_scenarios() -> list[Scenario]:
    return [
        create_project_scenario(),
        add_scene_scenario(),
        invite_member_scenario(),
        upload_file_scenario(),
        search_and_filter_scenario(),
    ]


DEFAULT_SCENARIOS = default_scenarios()


def _clone_scenarios(scenarios: list[Scenario]) -> list[Scenario]:
    return [Scenario.from_dict(scenario.to_dict()) for scenario in scenarios]


def _workflow_document(scenarios: list[Scenario], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = dict(existing or {})
    now = int(time.time())
    doc["version"] = 1
    doc.setdefault("created_at", now)
    doc["updated_at"] = now
    doc["scenarios"] = [scenario.to_dict() for scenario in scenarios]
    return doc


def _read_workflow_document(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        raise RuntimeError(f"Unable to read workflow file {path}: {e}") from e
    if isinstance(raw, list):
        return {"version": 1, "scenarios": raw}
    if not isinstance(raw, dict):
        raise RuntimeError(f"Workflow file must contain an object or list: {path}")
    raw.setdefault("scenarios", [])
    return raw


def save_scenarios(path: str, scenarios: list[Scenario], existing: dict[str, Any] | None = None) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = _workflow_document(scenarios, existing=existing)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def ensure_workflow_file(path: str) -> list[Scenario]:
    if os.path.exists(path):
        return load_scenarios(path)
    scenarios = _clone_scenarios(DEFAULT_SCENARIOS)
    save_scenarios(path, scenarios)
    return scenarios


def load_scenarios(path: str) -> list[Scenario]:
    if not os.path.exists(path):
        return ensure_workflow_file(path)
    try:
        doc = _read_workflow_document(path)
        return [Scenario.from_dict(item) for item in doc.get("scenarios", [])]
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise
        raise RuntimeError(f"Unable to load workflows from {path}: {e}") from e


def upsert_scenario(path: str, scenario: Scenario) -> bool:
    if os.path.exists(path):
        doc = _read_workflow_document(path)
        scenarios = [Scenario.from_dict(item) for item in doc.get("scenarios", [])]
    else:
        doc = _workflow_document(_clone_scenarios(DEFAULT_SCENARIOS))
        scenarios = [Scenario.from_dict(item) for item in doc.get("scenarios", [])]

    replaced = False
    for idx, existing in enumerate(scenarios):
        if existing.name == scenario.name:
            scenarios[idx] = scenario
            replaced = True
            break
    if not replaced:
        scenarios.append(scenario)
    save_scenarios(path, scenarios, existing=doc)
    return replaced


# ── Scenario runner ────────────────────────────────────────────────────────

class ScenarioRunner:
    def __init__(self, api_calls_ref: list[dict[str, Any]], known_urls: list[str] | None = None) -> None:
        self.api_calls_ref = api_calls_ref
        self.known_urls = [clean_url(url) for url in (known_urls or []) if url]

    async def _fill(self, page, selector: str, value: str, timeout: int) -> bool:
        try:
            el = await page.wait_for_selector(selector, timeout=timeout)
            if el:
                await el.fill(value)
                return True
        except Exception:
            pass
        return False

    async def _click(self, page, selector: str, timeout: int) -> bool:
        for sel in selector.split(","):
            sel = sel.strip()
            try:
                el = await page.wait_for_selector(sel, timeout=timeout // max(1, len(selector.split(","))))
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(0.8)
                    return True
            except Exception:
                continue
        return False

    async def _assert_dom(self, page, assertion: str, timeout: int = 3000) -> bool:
        try:
            await page.wait_for_selector(assertion, timeout=timeout)
            return True
        except Exception:
            # Also check text content
            try:
                content = await page.content()
                return assertion.lower() in content.lower()
            except Exception:
                return False

    async def _assert_api(self, endpoint: str, method: str, before_count: int) -> bool:
        new_calls = self.api_calls_ref[before_count:]
        for c in new_calls:
            if (
                endpoint.lower() in c.get("url", "").lower()
                and c.get("method", "").upper() == method.upper()
                and not c.get("failed")
            ):
                return True
        return False

    async def _upload(self, page, selector: str, value: str, timeout: int) -> bool:
        file_path = value or "__qa_test_file__"
        cleanup_path = ""
        if file_path == "__qa_test_file__":
            fd, cleanup_path = tempfile.mkstemp(prefix="qa-upload-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("Temporary QA upload file.\n")
            file_path = cleanup_path
        try:
            el = await page.wait_for_selector(selector, timeout=timeout)
            if el:
                await el.set_input_files(file_path)
                await asyncio.sleep(0.8)
                return True
        except Exception:
            pass
        finally:
            if cleanup_path and os.path.exists(cleanup_path):
                os.remove(cleanup_path)
        return False

    async def _handle_select(self, page, selector: str, value: str, timeout: int) -> bool:
        for sel in selector.split(","):
            sel = sel.strip()
            try:
                el = await page.wait_for_selector(sel, timeout=timeout // 2)
                if el:
                    tag = await el.evaluate("e => e.tagName.toLowerCase()")
                    if tag == "select":
                        if value:
                            try:
                                await el.select_option(value=value)
                                return True
                            except Exception:
                                pass
                            if value.isdigit():
                                try:
                                    await el.select_option(index=int(value))
                                    return True
                                except Exception:
                                    pass
                            try:
                                await el.select_option(label=value)
                                return True
                            except Exception:
                                pass
                        await el.select_option(index=0)
                    else:
                        await el.click()
                        await asyncio.sleep(0.3)
                        # click first option
                        opt = await page.query_selector('[role="option"]:first-child, .option:first-child')
                        if opt:
                            await opt.click()
                    return True
            except Exception:
                continue
        return False

    def _resolve_scenario_url(self, scenario: Scenario, base_url: str, current_url: str = "") -> str:
        route = (scenario.route or "/dashboard").strip()
        if route.startswith("http://") or route.startswith("https://"):
            return clean_url(route)

        if ":id" not in route:
            route = route if route.startswith("/") else f"/{route}"
            return clean_url(f"{base_url.rstrip('/')}{route}")

        candidates: list[str] = []
        if current_url:
            candidates.append(current_url)
        if scenario.start_url:
            candidates.append(scenario.start_url)
        candidates.extend(self.known_urls)

        for candidate in candidates:
            if candidate and canonicalize_path_from_url(candidate) == route:
                return clean_url(candidate)

        raise RuntimeError(
            f"Unable to resolve dynamic workflow route {route}. "
            "Crawl the app first or set start_url for this workflow."
        )

    async def run_scenario(self, page, scenario: Scenario, base_url: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "scenario": scenario.name,
            "route": scenario.route,
            "description": scenario.description,
            "started_at": time.time(),
            "steps": [],
            "passed": False,
            "error": "",
        }

        try:
            target = self._resolve_scenario_url(scenario, base_url, current_url=page.url)
            await page.goto(target, wait_until="networkidle", timeout=20000)
            await asyncio.sleep(1.0)
        except Exception as e:
            result["error"] = f"Navigation failed: {e}"
            return result

        ts = str(int(time.time()))
        before_api = len(self.api_calls_ref)

        all_passed = True
        for i, step in enumerate(scenario.steps):
            step_result: dict[str, Any] = {
                "index": i,
                "type": step.type.value,
                "description": step.description or step.selector or step.assertion,
                "passed": False,
                "error": "",
            }

            value = step.value.replace("{{ts}}", ts)
            try:
                if step.type == StepType.NAVIGATE:
                    nav_target = value or step.selector or step.assertion
                    if nav_target and not nav_target.startswith(("http://", "https://")):
                        nav_target = f"{base_url.rstrip('/')}/{nav_target.lstrip('/')}"
                    await page.goto(nav_target, wait_until="networkidle", timeout=step.timeout_ms)
                    await asyncio.sleep(1.0)
                    ok = True
                elif step.type == StepType.FILL:
                    ok = await self._fill(page, step.selector, value, step.timeout_ms)
                elif step.type == StepType.CLICK:
                    ok = await self._click(page, step.selector, step.timeout_ms)
                elif step.type == StepType.SELECT:
                    ok = await self._handle_select(page, step.selector, value, step.timeout_ms)
                elif step.type in (StepType.MODAL_OPEN, StepType.ASSERT_DOM):
                    ok = await self._assert_dom(page, step.assertion, step.timeout_ms)
                elif step.type == StepType.MODAL_CLOSE:
                    ok = await self._click(page, '[aria-label="Close"], .modal-close, button:has-text("Cancel")', 2000)
                elif step.type == StepType.ASSERT_API:
                    await asyncio.sleep(1.0)  # let API call fire
                    ok = await self._assert_api(step.api_endpoint, step.api_method, before_api)
                elif step.type == StepType.ASSERT_BUSINESS:
                    ok = await self._assert_dom(page, step.assertion, step.timeout_ms)
                elif step.type == StepType.WAIT:
                    await asyncio.sleep(float(step.value or "1"))
                    ok = True
                elif step.type == StepType.UPLOAD:
                    ok = await self._upload(page, step.selector, value, step.timeout_ms)
                else:
                    ok = True

                step_result["passed"] = ok
                if not ok and not step.optional:
                    step_result["error"] = f"Step failed: {step.type}"
                    all_passed = False
            except Exception as e:
                step_result["error"] = str(e)
                if not step.optional:
                    all_passed = False

            result["steps"].append(step_result)
            if not step_result["passed"] and not step.optional:
                break  # stop on non-optional failure

        result["passed"] = all_passed
        result["finished_at"] = time.time()
        result["duration_ms"] = round((result["finished_at"] - result["started_at"]) * 1000)
        return result


# ── LangGraph workflow engine (optional) ──────────────────────────────────

def build_langgraph_crawler(run_fn: Callable) -> Any:
    """
    Wrap the crawl runner as a LangGraph stateful workflow.
    Falls back gracefully if LangGraph is not installed.
    """
    if not LANGGRAPH_AVAILABLE:
        return None

    from typing import TypedDict

    class CrawlGraphState(TypedDict):
        url: str
        visited: list[str]
        queue: list[str]
        pages_done: int
        errors: list[str]
        status: str

    graph = LGStateGraph(CrawlGraphState)

    async def crawl_node(state: CrawlGraphState) -> CrawlGraphState:
        url = state["url"]
        try:
            result = await run_fn(url)
            return {
                **state,
                "visited": state["visited"] + [url],
                "pages_done": state["pages_done"] + 1,
                "status": "ok",
            }
        except Exception as e:
            return {
                **state,
                "errors": state["errors"] + [f"{url}: {e}"],
                "status": "error",
            }

    def route_after_crawl(state: CrawlGraphState) -> str:
        if state["queue"]:
            return "crawl"
        return END

    graph.add_node("crawl", crawl_node)
    graph.set_entry_point("crawl")
    graph.add_conditional_edges("crawl", route_after_crawl, {"crawl": "crawl", END: END})

    memory = MemorySaver()
    return graph.compile(checkpointer=memory)
