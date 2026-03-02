"""
runner.py — Orchestrates crawl using Dijkstra-priority frontier.

Queue strategy (replaces dumb FIFO):
  - UrlFrontier (min-heap) scores every candidate URL by novelty
  - Never-visited URLs always score highest (10.0)
  - Previously visited but still-changing routes get moderate scores
  - Exhausted routes (visited 3+ times, no new elements) score near 0
  - After each batch, newly discovered links are scored and pushed onto frontier
  - KB's globally-known unvisited links are injected at run start
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable

from playwright.async_api import async_playwright

from .analysis import analyze_batch, normalize_analysis
from .auth import check_session_alive, do_login, load_session_context, new_browser_context, save_session
from .config import Settings
from .explorer import Explorer
from .knowledge import KnowledgeBase, UrlFrontier
from .reporting import compute_deltas, generate_report, save_history, send_slack_alert, _load_history
from .state import CrawlState
from .telemetry import ApiTelemetry
from .utils import canonicalize_path, canonicalize_path_from_url, clean_url, coerce_health_score, same_origin, should_skip
from .workflows import ScenarioRunner, load_scenarios

BroadcastFn = Callable[[dict[str, Any]], Awaitable[None]]


class CrawlRunner:
    def __init__(self, settings: Settings, broadcast: BroadcastFn | None = None) -> None:
        self.settings  = settings
        self.broadcast = broadcast
        self.state     = CrawlState()
        self.api       = ApiTelemetry()
        self.kb        = KnowledgeBase(settings.knowledge_file)
        self.explorer  = Explorer(settings, self.state, self.api, kb=self.kb, emit=self._emit)
        # Dijkstra frontier — replaces self.state.queue for URL ordering
        self.frontier  = UrlFrontier()
        # URLs queued into frontier this run (for dedup)
        self._frontier_seen: set[str] = set()
        # Canonical routes queued or visited this run (strict route-level dedupe).
        self._frontier_routes_seen: set[str] = set()
        self._visited_routes_this_run: set[str] = set()
        # Minimal hub routes revisited every run so the explorer can still find new URLs.
        # Seed entries act as route-family prefixes, so "/project-details" matches
        # "/project-details/:id" and deeper canonical children.
        self._discovery_seed_routes: set[str] = set()
        self._current_phase = "authenticated"
        self._phase_visited_urls: dict[str, set[str]] = {}

    async def _emit(self, msg: dict[str, Any]) -> None:
        if self.broadcast:
            await self.broadcast(msg)

    def load_resume_state(self) -> bool:
        if not os.path.exists(self.settings.state_file):
            return False
        self.state = CrawlState.load_snapshot(self.settings.state_file)
        self.explorer.state = self.state
        self._phase_visited_urls = {}
        for page in self.state.pages_data:
            phase = str(page.get("crawl_phase") or "authenticated")
            url = clean_url(str(page.get("url", "") or ""))
            if url:
                self._phase_visited_urls.setdefault(phase, set()).add(url)
        if not self._phase_visited_urls and self.state.visited:
            self._phase_visited_urls["authenticated"] = {
                clean_url(url) for url in self.state.visited if clean_url(url)
            }
        self._begin_phase("authenticated")
        return True

    def _phase_urls(self, phase: str | None = None) -> set[str]:
        name = phase or self._current_phase
        return self._phase_visited_urls.setdefault(name, set())

    def _begin_phase(self, phase: str) -> None:
        self._current_phase = phase
        self.frontier = UrlFrontier()
        self._frontier_seen = set()
        visited_urls = self._phase_urls(phase)
        self._visited_routes_this_run = {
            canonicalize_path_from_url(url) for url in visited_urls
        }
        self._frontier_routes_seen = set(self._visited_routes_this_run)

    def _mark_phase_visited(self, url: str, phase: str | None = None) -> None:
        clean = clean_url(url)
        if not clean:
            return
        self._phase_urls(phase).add(clean)
        self.state.visited.add(clean)

    def _register_seed_route(self, value: str) -> None:
        value = (value or "").strip()
        if not value:
            return
        route = (
            canonicalize_path_from_url(value)
            if value.startswith(("http://", "https://"))
            else canonicalize_path(value if value.startswith("/") else f"/{value}")
        )
        if route:
            self._discovery_seed_routes.add(route)

    def _configure_seed_routes(
        self,
        start_url: str = "",
        routes: list[str] | None = None,
        include_dashboard: bool = True,
    ) -> None:
        start_path = self.settings.start_path if self.settings.start_path.startswith("/") else f"/{self.settings.start_path}"
        self._discovery_seed_routes = set()
        for route in routes or self.settings.discovery_seed_routes:
            self._register_seed_route(route)
        if start_url:
            self._register_seed_route(start_url)
        if include_dashboard:
            self._register_seed_route(f"{self.settings.base_url}{start_path}")

    def _concrete_seed_urls(self) -> list[str]:
        urls: list[str] = []
        for route in sorted(self._discovery_seed_routes):
            if not route or ":" in route:
                continue
            if route == "/":
                urls.append(self.settings.base_url)
            else:
                urls.append(f"{self.settings.base_url}{route}")
        return urls

    def _route_matches_seed(self, route: str, seed: str) -> bool:
        if not route or not seed:
            return False
        if seed == "/":
            return route == "/"
        return route == seed or route.startswith(f"{seed}/")

    def _is_seed_route(self, route: str) -> bool:
        return any(self._route_matches_seed(route, seed) for seed in self._discovery_seed_routes)

    def _is_globally_known_route(self, route: str) -> bool:
        rk = self.kb.routes.get(route)
        return bool(rk and rk.visit_count > 0)

    def _should_enqueue_route(self, route: str, force: bool = False) -> bool:
        if route in self._frontier_routes_seen:
            return False
        if route in self._visited_routes_this_run:
            return False
        if not self.settings.strict_route_dedupe:
            return True
        if force or self._is_seed_route(route):
            return True
        return not self._is_globally_known_route(route)

    def _push_url(self, url: str, force: bool = False) -> None:
        """Push a URL onto the Dijkstra frontier if not already seen this run."""
        url = clean_url(url)
        if not url:
            return
        if not same_origin(url, self.settings.base_url):
            return
        if should_skip(url, self.settings.skip_paths):
            return
        if url in self._frontier_seen:
            return
        if url in self._phase_urls():
            return
        route = canonicalize_path_from_url(url)
        if not self._should_enqueue_route(route, force=force):
            return
        score = self.kb.score_url(url)
        self.frontier.push(url, score)
        self._frontier_seen.add(url)
        self._frontier_routes_seen.add(route)

    def _pop_batch(self, n: int) -> list[str]:
        """Pop up to n highest-priority URLs from the frontier."""
        batch = []
        while self.frontier and len(batch) < n:
            url = self.frontier.pop()
            if url and url not in self._phase_urls():
                batch.append(url)
        return batch

    def _inject_kb_frontier(self) -> int:
        """
        At run start: inject all URLs the KB knows about but never visited.
        These are links discovered in previous runs that were never crawled.
        Returns number injected.
        """
        injected = 0
        for rk in self.kb.routes.values():
            for url in list(rk.unvisited_links):
                if url not in self._frontier_seen and url not in self._phase_urls():
                    if same_origin(url, self.settings.base_url):
                        if not should_skip(url, self.settings.skip_paths):
                            before = len(self.frontier)
                            self._push_url(url)
                            if len(self.frontier) > before:
                                injected += 1
        return injected

    def _frontier_preview(self, limit: int = 14) -> list[dict[str, Any]]:
        preview: list[dict[str, Any]] = []
        for score, url in self.frontier.snapshot(limit):
            route = canonicalize_path_from_url(url)
            rk = self.kb.routes.get(route)
            preview.append({
                "url": url,
                "route": route,
                "score": round(score, 2),
                "cost": round(max(0.0, 10.0 - score), 2),
                "visits": rk.visit_count if rk else 0,
                "links_pending": len(rk.unvisited_links) if rk else 0,
                "exhausted": rk.is_exhausted if rk else False,
            })
        return preview

    def _selection_payload(self, urls: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for url in urls:
            score = round(self.kb.score_url(url), 2)
            route = canonicalize_path_from_url(url)
            rk = self.kb.routes.get(route)
            rows.append({
                "url": url,
                "route": route,
                "score": score,
                "cost": round(max(0.0, 10.0 - score), 2),
                "visits": rk.visit_count if rk else 0,
                "links_pending": len(rk.unvisited_links) if rk else 0,
                "selected": True,
            })
        return rows

    # ── Auth ───────────────────────────────────────────────────────────────

    async def _ensure_authenticated(self, browser, resume: bool) -> tuple[Any, str]:
        context, already_logged = await load_session_context(browser, self.settings)
        start_path = self.settings.start_path if self.settings.start_path.startswith("/") else f"/{self.settings.start_path}"
        start_url = f"{self.settings.base_url}{start_path}"
        if not already_logged:
            page = await context.new_page()
            await self._emit({"type": "status", "msg": "Login required. Complete auth in browser."})
            ok = await do_login(page, self.settings)
            if not ok:
                await page.close(); await context.close()
                raise RuntimeError("Login was not completed within timeout.")
            await save_session(context, self.settings.session_file)
            start_url = clean_url(page.url) or start_url
            await page.close()
            return context, start_url

        test_page = await context.new_page()
        try:
            await test_page.goto(start_url, wait_until="networkidle", timeout=20000)
        except Exception as nav_err:
            await test_page.close()
            err_str = str(nav_err)
            if any(e in err_str for e in ("ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION_REFUSED", "ERR_INTERNET_DISCONNECTED")):
                raise RuntimeError(
                    f"\n\n{'='*60}\nCannot reach: {start_url}\nError: {err_str.split(chr(10))[0]}\n\n"
                    f"Fix: set QA_BASE_URL in your .env file.\n  e.g. QA_BASE_URL=http://localhost:3000\n"
                    f"Current value: {self.settings.base_url}\n{'='*60}\n"
                ) from None
            await context.close()
            context, _ = await load_session_context(browser, self.settings)
            page = await context.new_page()
            ok = await do_login(page, self.settings)
            if not ok:
                await page.close(); await context.close()
                raise RuntimeError("Login was not completed within timeout.")
            await save_session(context, self.settings.session_file)
            start_url = clean_url(page.url) or start_url
            await page.close()
            return context, start_url

        if not await check_session_alive(test_page, self.settings):
            await test_page.close(); await context.close()
            context, _ = await load_session_context(browser, self.settings)
            page = await context.new_page()
            ok = await do_login(page, self.settings)
            if not ok:
                await page.close(); await context.close()
                raise RuntimeError("Re-authentication failed.")
            await save_session(context, self.settings.session_file)
            start_url = clean_url(page.url) or start_url
            await page.close()
            return context, start_url

        await test_page.close()
        if resume and self.state.pages_data:
            for page_data in reversed(self.state.pages_data):
                if (page_data.get("crawl_phase") or "authenticated") == "authenticated":
                    start_url = page_data.get("url", start_url)
                    break
        return context, start_url

    # ── Batch crawl ────────────────────────────────────────────────────────

    async def _crawl_batch(self, context, urls: list[str]) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(self.settings.parallel_limit)

        async def run_one(u: str):
            async with semaphore:
                attempt = int(self.state.retries.get(u, 0))
                return await self.explorer.explore_page(context, u, retry_attempt=attempt)

        results = await asyncio.gather(*(run_one(u) for u in urls), return_exceptions=True)
        out = []
        for u, r in zip(urls, results):
            if isinstance(r, Exception):
                out.append({
                    "url": u, "title": "", "http_status": 0, "load_time_ms": 0,
                    "js_errors": [], "network_failures": [{"url": u, "failure": str(r)}],
                    "http_errors": [], "images_missing_alt": [], "screenshot": "",
                    "screenshot_diff": None, "retried": False,
                    "retry_attempt": int(self.state.retries.get(u, 0)),
                    "perf_metrics": {}, "dom_mutations": {}, "api_calls": [],
                    "failed_apis": [], "interaction_results": [], "links_found": 0,
                    "api_failures": 0, "n_new_elements": 0, "n_skipped_elements": 0,
                    "route_exhausted": False, "discovered_links": [],
                })
            else:
                out.append(r)
        return out

    def _mark_retry_status(self, page_row: dict[str, Any]) -> None:
        score   = coerce_health_score(page_row.get("analysis", {}).get("health_score", 5), default=5.0)
        url     = page_row.get("url", "")
        route   = canonicalize_path_from_url(url)
        attempt = int(page_row.get("retry_attempt", 0))
        can_retry = not self.settings.strict_route_dedupe
        if attempt == 0 and score < self.settings.retry_threshold and can_retry:
            self.state.retries[url] = 1
            # Re-push onto frontier with boosted score (needs re-check)
            self._frontier_seen.discard(url)
            self._frontier_routes_seen.discard(route)
            self._push_url(url)
            page_row["retry_status"] = "scheduled"
        elif attempt > 0 and score < self.settings.retry_threshold:
            self.state.retry_classification[url] = "persistent"
            page_row["retry_status"] = "persistent"
        elif attempt > 0 and score >= self.settings.retry_threshold:
            self.state.retry_classification[url] = "flaky"
            page_row["retry_status"] = "flaky"
        else:
            page_row["retry_status"] = self.state.retry_classification.get(url, "")

    # ── Scenarios ──────────────────────────────────────────────────────────

    async def _run_crawl_phase(
        self,
        context,
        *,
        phase: str,
        max_pages: int,
        start_url: str = "",
        seed_routes: list[str] | None = None,
        include_dashboard: bool = True,
        inject_kb: bool = False,
    ) -> None:
        if max_pages <= 0:
            return

        self._begin_phase(phase)
        self._configure_seed_routes(start_url, routes=seed_routes, include_dashboard=include_dashboard)

        if start_url:
            self._push_url(start_url, force=True)
        if include_dashboard:
            start_path = self.settings.start_path if self.settings.start_path.startswith("/") else f"/{self.settings.start_path}"
            self._push_url(f"{self.settings.base_url}{start_path}", force=True)
        for seed_url in self._concrete_seed_urls():
            self._push_url(seed_url, force=True)

        if inject_kb:
            injected = self._inject_kb_frontier()
            if injected:
                print(f"[Frontier:{phase}] Injected {injected} previously-discovered unvisited URLs")
                await self._emit({
                    "type": "status",
                    "msg": f"Loaded {injected} unvisited URLs from knowledge base for {phase} crawl",
                    "crawl_phase": phase,
                })

        await self._emit({
            "type": "frontier_init",
            "crawl_phase": phase,
            "frontier_size": len(self.frontier),
            "kb_routes": len(self.kb.routes),
            "kb_elements": sum(len(rk.elements) for rk in self.kb.routes.values()),
            "kb_skippable": sum(len(rk.skippable_elements()) for rk in self.kb.routes.values()),
            "kb_run": self.kb.run_count,
            "route_stats": self.kb.route_stats()[:30],
            "frontier_preview": self._frontier_preview(),
        })

        phase_start_count = len(self.state.pages_data)
        while (
            self.frontier
            and len(self.state.pages_data) < self.settings.max_pages
            and (len(self.state.pages_data) - phase_start_count) < max_pages
        ):
            phase_remaining = max_pages - (len(self.state.pages_data) - phase_start_count)
            batch_urls = self._pop_batch(min(self.settings.parallel_limit, phase_remaining))
            if not batch_urls:
                break

            for u in batch_urls:
                self._mark_phase_visited(u, phase=phase)
                self._visited_routes_this_run.add(canonicalize_path_from_url(u))

            await self._emit({
                "type": "crawling",
                "crawl_phase": phase,
                "msg": f"Crawling {len(batch_urls)} {phase} page(s) — {len(self.frontier)} in frontier",
                "url": batch_urls[0],
                "active_urls": batch_urls,
                "active_routes": [canonicalize_path_from_url(url) for url in batch_urls],
                "queue_size": len(self.frontier),
                "frontier_scores": self._selection_payload(batch_urls),
                "frontier_preview": self._frontier_preview(),
            })

            raw_batch = await self._crawl_batch(context, batch_urls)
            summaries = [
                {
                    "url": p["url"], "title": p.get("title", ""),
                    "route": canonicalize_path_from_url(p.get("url", "")),
                    "http_status": p.get("http_status", 0),
                    "load_time_ms": p.get("load_time_ms", 0),
                    "links_found": p.get("links_found", 0),
                    "states_seen": p.get("states_seen", 0),
                    "state_transitions": p.get("state_transitions", 0),
                    "js_errors": p.get("js_errors", []),
                    "network_failures": p.get("network_failures", []),
                    "http_errors": p.get("http_errors", []),
                    "failed_apis": p.get("failed_apis", []),
                    "perf_metrics": p.get("perf_metrics", {}),
                    "dom_mutations": p.get("dom_mutations", {}),
                    "interaction_results": p.get("interaction_results", []),
                    "screenshot_b64": p.get("screenshot_b64", ""),
                }
                for p in raw_batch
            ]
            analyses = await analyze_batch(self.settings, summaries)

            for raw, analysis in zip(raw_batch, analyses):
                normalized = normalize_analysis(analysis if isinstance(analysis, dict) else {})
                row = {**raw, "analysis": normalized, "crawl_phase": phase}

                route = canonicalize_path_from_url(row.get("url", ""))
                self.kb.record_page(route, {
                    "analysis": normalized,
                    "title": row.get("title", ""),
                    "screenshot": row.get("screenshot", ""),
                    "discovered_links": row.get("discovered_links", []),
                    "interaction_results": row.get("interaction_results", []),
                    "states_seen": row.get("states_seen", 0),
                }, n_elements=len(row.get("interaction_results", [])))

                self._mark_retry_status(row)
                self.state.pages_data.append(row)
                self.state.update_coverage(row)

                n_pushed = 0
                for lnk in row.get("discovered_links", []):
                    if lnk not in self._phase_urls():
                        before = len(self.frontier)
                        self._push_url(lnk)
                        if len(self.frontier) > before:
                            n_pushed += 1

                page_score = self.kb.score_url(row["url"])
                await self._emit({
                    "type": "page_done",
                    "crawl_phase": phase,
                    "page": {
                        "url":                 row.get("url", ""),
                        "title":               row.get("title", ""),
                        "crawl_phase":         phase,
                        "health_score":        normalized.get("health_score", 5),
                        "health_label":        normalized.get("health_label", ""),
                        "load_time_ms":        row.get("load_time_ms", 0),
                        "links_found":         row.get("links_found", 0),
                        "api_failures":        row.get("api_failures", 0),
                        "analysis":            normalized,
                        "retry_status":        row.get("retry_status", ""),
                        "perf_metrics":        row.get("perf_metrics", {}),
                        "api_calls":           row.get("api_calls", []),
                        "interaction_results": row.get("interaction_results", []),
                        "root_state":          row.get("root_state", {}),
                        "states_seen":         row.get("states_seen", 0),
                        "state_transitions":   row.get("state_transitions", 0),
                        "discovered_links":    row.get("discovered_links", []),
                        "screenshot":          row.get("screenshot", ""),
                        "broken_interactions": row.get("broken_interactions", 0),
                        "n_new_elements":      row.get("n_new_elements", 0),
                        "n_skipped_elements":  row.get("n_skipped_elements", 0),
                        "route_exhausted":     row.get("route_exhausted", False),
                        "frontier_score":      round(page_score, 2),
                        "n_links_pushed":      n_pushed,
                    },
                    "total":            len(self.state.pages_data),
                    "frontier_size":    len(self.frontier),
                    "route_stats":      self.kb.route_stats()[:20],
                    "frontier_preview": self._frontier_preview(),
                })

            await self._emit({
                "type": "interaction_stats",
                "stats": self.state.all_interaction_stats()[:50],
                "coverage": self.state.coverage.to_dict(),
                "flaky": self.state.top_flaky_actions(5),
                "no_change": self.state.top_no_change_actions(5),
            })

            self.state.save_snapshot(self.settings.state_file)

            if any(p.get("retry_status") == "scheduled"
                   for p in self.state.pages_data[-len(raw_batch):]):
                await asyncio.sleep(self.settings.retry_delay_seconds)

    async def _run_scenarios(self, context) -> None:
        if not self.settings.run_scenarios:
            return
        scenarios = load_scenarios(self.settings.workflows_file)
        if not scenarios:
            await self._emit({"type": "status", "msg": "No workflows found to run."})
            return

        known_urls = list(self.state.visited) + [
            page_row.get("url", "") for page_row in self.state.pages_data if page_row.get("url")
        ]
        page = await context.new_page()
        runner = ScenarioRunner(self.api.all_calls, known_urls=known_urls)
        await self._emit({
            "type": "status",
            "msg": f"Running {len(scenarios)} workflow scenarios from {self.settings.workflows_file}...",
        })
        for scenario in scenarios:
            try:
                result = await asyncio.wait_for(
                    runner.run_scenario(page, scenario, self.settings.base_url),
                    timeout=self.settings.scenario_timeout_seconds,
                )
            except asyncio.TimeoutError:
                result = {"scenario": scenario.name, "route": scenario.route,
                          "passed": False, "error": "Timeout", "steps": [],
                          "duration_ms": self.settings.scenario_timeout_seconds * 1000}
            except Exception as e:
                result = {"scenario": scenario.name, "route": scenario.route,
                          "passed": False, "error": str(e), "steps": [], "duration_ms": 0}
            self.state.workflow_results.append(result)
            if scenario.critical:
                if result["passed"]:
                    self.state.coverage.critical_workflows_completed.add(scenario.name)
                if scenario.name not in self.state.coverage.critical_workflows_defined:
                    self.state.coverage.critical_workflows_defined.append(scenario.name)
            await self._emit({
                "type": "workflow_done", "result": result,
                "msg": f"Scenario {scenario.name}: {'✓' if result['passed'] else '✗'}",
            })
        await page.close()

    # ── Main run ───────────────────────────────────────────────────────────

    async def run(self, resume: bool = False) -> CrawlState:
        self.kb.load()

        if resume:
            self.load_resume_state()
            await self._emit({"type": "status", "msg": f"Resuming from {self.settings.state_file}"})

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, slow_mo=40)

            if self.settings.crawl_public and not resume and len(self.state.pages_data) < self.settings.max_pages:
                public_budget = min(
                    self.settings.public_max_pages,
                    self.settings.max_pages - len(self.state.pages_data),
                )
                if public_budget > 0:
                    await self._emit({
                        "type": "status",
                        "msg": f"Starting public pre-auth crawl ({public_budget} page budget).",
                        "crawl_phase": "public",
                    })
                    public_context = await new_browser_context(browser)
                    try:
                        await self._run_crawl_phase(
                            public_context,
                            phase="public",
                            max_pages=public_budget,
                            seed_routes=self.settings.public_routes,
                            include_dashboard=False,
                            inject_kb=False,
                        )
                    finally:
                        await public_context.close()

            context, start_url = await self._ensure_authenticated(browser, resume=resume)
            if self.state.queue:
                await self._emit({
                    "type": "status",
                    "msg": f"Skipping {len(self.state.queue)} legacy queued URL(s); the phase-aware frontier is rebuilt fresh each run.",
                    "crawl_phase": "authenticated",
                })
            self.state.queue.clear()

            remaining_budget = self.settings.max_pages - len(self.state.pages_data)
            if remaining_budget > 0:
                await self._run_crawl_phase(
                    context,
                    phase="authenticated",
                    max_pages=remaining_budget,
                    start_url=start_url,
                    seed_routes=self.settings.discovery_seed_routes,
                    include_dashboard=True,
                    inject_kb=True,
                )

            await self._run_scenarios(context)
            await browser.close()

        api_summary = self.api.summarize()

        with open(self.settings.log_file, "w", encoding="utf-8") as f:
            json.dump(self.state.pages_data, f, indent=2)

        generate_report(
            self.settings, self.state.pages_data, api_summary, self.state.error_map,
            interaction_stats=self.state.all_interaction_stats(),
            workflow_results=self.state.workflow_results,
            coverage=self.state.coverage.to_dict(),
        )
        save_history(self.settings, self.state.pages_data, api_summary)

        history = _load_history(self.settings)
        deltas  = compute_deltas(history)

        kb_summary = self.kb.summary()
        await self._emit({
            "type": "complete",
            "msg": f"Crawl complete — {len(self.state.pages_data)} pages, "
                   f"{kb_summary['elements_active']} active elements, "
                   f"{kb_summary['elements_skip']} skipped next run",
            "coverage":          self.state.coverage.to_dict(),
            "interaction_stats": self.state.all_interaction_stats()[:100],
            "workflow_results":  self.state.workflow_results,
            "deltas":            deltas,
            "history":           history[-10:],
            "flaky":             self.state.top_flaky_actions(10),
            "no_change":         self.state.top_no_change_actions(10),
            "kb_summary":        kb_summary,
            "route_stats":       self.kb.route_stats(),
            "frontier_preview":  self._frontier_preview(),
            "active_urls":       [],
        })

        self.kb.save()
        print(f"[KB] {self.kb.summary()}")
        send_slack_alert(self.settings, self.state.pages_data)
        self.state.save_snapshot(self.settings.state_file)
        return self.state
