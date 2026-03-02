from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "assets" / "product"
DASHBOARD_HTML = (ROOT / "dashboard.html").resolve().as_uri()


def build_page(
    url: str,
    score: float,
    load_time_ms: int,
    links_found: int,
    *,
    crawl_phase: str = "authenticated",
    http_status: int = 200,
    api_failures: int = 0,
    bugs: list[str] | None = None,
    ux_issues: list[str] | None = None,
    performance_issues: list[str] | None = None,
    api_issues: list[str] | None = None,
    broken_links: list[str] | None = None,
    summary: str = "",
    discovered_links: list[str] | None = None,
    interaction_results: list[dict] | None = None,
    frontier_score: float = 0.0,
    n_new_elements: int = 0,
    n_skipped_elements: int = 0,
    n_links_pushed: int = 0,
    route_exhausted: bool = False,
    retry_status: str = "",
) -> dict:
    return {
        "url": url,
        "title": "",
        "crawl_phase": crawl_phase,
        "http_status": http_status,
        "load_time_ms": load_time_ms,
        "links_found": links_found,
        "api_failures": api_failures,
        "perf_metrics": {"fcp": max(350, int(load_time_ms * 0.42))},
        "analysis": {
            "health_score": score,
            "summary": summary,
            "bugs": bugs or [],
            "ux_issues": ux_issues or [],
            "performance_issues": performance_issues or [],
            "api_issues": api_issues or [],
            "broken_links": broken_links or [],
            "visual_issues": [],
        },
        "interaction_results": interaction_results or [],
        "root_state": {"label": "root", "kind": "page"},
        "discovered_links": discovered_links or [],
        "states_seen": 1,
        "state_transitions": 0,
        "frontier_score": frontier_score,
        "n_new_elements": n_new_elements,
        "n_skipped_elements": n_skipped_elements,
        "n_links_pushed": n_links_pushed,
        "route_exhausted": route_exhausted,
        "retry_status": retry_status,
    }


EXAMPLE_STATE = {
    "pages_data": [
        build_page(
            "https://example-app.local/",
            8.8,
            920,
            12,
            crawl_phase="public",
            summary="Marketing landing page is stable and links into the authenticated product.",
            discovered_links=[
                "https://example-app.local/auth/sign-in",
                "https://example-app.local/dashboard",
            ],
            frontier_score=8.4,
            n_new_elements=4,
            n_skipped_elements=1,
            n_links_pushed=2,
        ),
        build_page(
            "https://example-app.local/auth/sign-in",
            7.9,
            1180,
            4,
            crawl_phase="public",
            summary="Sign-in route is healthy, with one minor copy clarity issue.",
            ux_issues=["Password reset entry point is visually subdued on narrow screens."],
            discovered_links=["https://example-app.local/dashboard"],
            frontier_score=6.9,
            n_new_elements=2,
            n_skipped_elements=1,
            n_links_pushed=1,
        ),
        build_page(
            "https://example-app.local/dashboard",
            6.2,
            2480,
            28,
            summary="Dashboard loads successfully but includes a noisy permissions warning and a slow metrics card.",
            bugs=["Permission summary widget throws a recoverable console warning during first paint."],
            performance_issues=["Dashboard KPI cards are slow to settle after navigation."],
            discovered_links=[
                "https://example-app.local/projects/alpha",
                "https://example-app.local/settings/team",
                "https://example-app.local/reports/release-readiness",
            ],
            interaction_results=[
                {"action": "Open project", "outcome": "navigation", "text": "Project Alpha"},
                {"action": "Create project", "outcome": "modal_open", "text": "New Project"},
            ],
            frontier_score=7.1,
            n_new_elements=8,
            n_skipped_elements=2,
            n_links_pushed=3,
        ),
        build_page(
            "https://example-app.local/projects/alpha",
            7.4,
            1810,
            16,
            summary="Project overview is mostly healthy with one flaky save interaction.",
            ux_issues=["Secondary action labels are inconsistent between side panels."],
            discovered_links=[
                "https://example-app.local/projects/alpha/assets",
                "https://example-app.local/projects/alpha/storyboard",
            ],
            interaction_results=[
                {"action": "Open assets", "outcome": "navigation", "text": "Assets"},
                {"action": "Open storyboard", "outcome": "navigation", "text": "Storyboard"},
            ],
            frontier_score=7.8,
            n_new_elements=6,
            n_skipped_elements=2,
            n_links_pushed=2,
        ),
        build_page(
            "https://example-app.local/projects/alpha/assets",
            5.1,
            3290,
            9,
            summary="Asset library is usable but slow, with one upload API failure affecting confidence.",
            performance_issues=["Asset table takes too long to render after filter changes."],
            api_issues=["POST /api/assets/upload intermittently returns 502 during large uploads."],
            api_failures=1,
            discovered_links=["https://example-app.local/projects/alpha/storyboard"],
            frontier_score=5.5,
            n_new_elements=5,
            n_skipped_elements=3,
            n_links_pushed=1,
            retry_status="scheduled",
        ),
        build_page(
            "https://example-app.local/projects/alpha/storyboard",
            4.3,
            4025,
            11,
            summary="Storyboard view has the highest risk in this run due to a rendering bug and unstable comment actions.",
            bugs=["Storyboard canvas fails to render one preview tile after data refresh."],
            performance_issues=["Storyboard scene graph takes over 4s to stabilize."],
            api_issues=["GET /api/storyboard/scenes shows elevated latency under load."],
            broken_links=["Comment drawer CTA opens an empty panel instead of the selected shot."],
            api_failures=2,
            discovered_links=["https://example-app.local/reports/release-readiness"],
            interaction_results=[
                {"action": "Open scene", "outcome": "dom_mutation", "text": "Scene A"},
                {"action": "Comment drawer", "outcome": "broken", "text": "Comments"},
            ],
            frontier_score=4.8,
            n_new_elements=7,
            n_skipped_elements=4,
            n_links_pushed=1,
        ),
        build_page(
            "https://example-app.local/settings/team",
            7.1,
            1710,
            7,
            summary="Team settings route is stable and suitable for invitation workflows.",
            discovered_links=["https://example-app.local/reports/release-readiness"],
            frontier_score=6.4,
            n_new_elements=3,
            n_skipped_elements=1,
            n_links_pushed=1,
            route_exhausted=True,
        ),
        build_page(
            "https://example-app.local/reports/release-readiness",
            8.2,
            1490,
            5,
            summary="Release readiness report is healthy and aggregates known issues clearly.",
            discovered_links=[],
            frontier_score=8.6,
            n_new_elements=2,
            n_skipped_elements=1,
            n_links_pushed=0,
            route_exhausted=True,
        ),
    ],
    "coverage": {
        "pct_pages_with_interactions": 88,
        "unique_actions_count": 23,
        "pct_critical_workflows": 67,
    },
    "interaction_records": {
        "dashboard::open_project": {
            "route": "/dashboard",
            "action": "Open project",
            "attempts": 9,
            "success": 8,
            "neutral": 1,
            "fail": 0,
            "reliability": 0.89,
            "flakiness": 0.11,
        },
        "storyboard::comment_drawer": {
            "route": "/projects/alpha/storyboard",
            "action": "Open comment drawer",
            "attempts": 6,
            "success": 2,
            "neutral": 1,
            "fail": 3,
            "reliability": 0.33,
            "flakiness": 0.5,
        },
        "assets::upload": {
            "route": "/projects/alpha/assets",
            "action": "Upload asset",
            "attempts": 5,
            "success": 3,
            "neutral": 0,
            "fail": 2,
            "reliability": 0.6,
            "flakiness": 0.4,
        },
    },
    "workflow_results": [
        {
            "scenario": "invite_reviewer",
            "route": "/settings/team",
            "passed": True,
            "duration_ms": 2840,
            "steps": [
                {"description": "Open invite modal", "type": "click", "passed": True},
                {"description": "Fill reviewer email", "type": "fill", "passed": True},
                {"description": "Submit invitation", "type": "click", "passed": True},
            ],
        },
        {
            "scenario": "publish_storyboard",
            "route": "/projects/alpha/storyboard",
            "passed": False,
            "duration_ms": 3610,
            "error": "Publish confirmation did not resolve after comment drawer failure.",
            "steps": [
                {"description": "Open storyboard", "type": "click", "passed": True},
                {"description": "Open comments", "type": "click", "passed": False, "error": "Drawer remained empty."},
            ],
        },
    ],
    "state_graph": {"actions": [], "edges": [], "route_links": []},
    "workspace": {
        "site_id": "example-site",
        "site_name": "Example Site",
        "base_url": "https://example-app.local",
        "site_dir": "sites/example-site",
        "site_config_file": "sites/example-site/site.json",
        "commands": {
            "crawl": "python agent.py --site example-site",
            "resume": "python agent.py --site example-site --resume",
            "serve": "python serve.py --site example-site",
            "record_workflow": "python agent.py --site example-site --record-workflow",
        },
        "artifacts": {
            "site_config": "sites/example-site/site.json",
            "state": "sites/example-site/crawl_state.json",
            "log": "sites/example-site/crawl_log.json",
            "report": "sites/example-site/report.html",
            "history": "sites/example-site/history.json",
            "knowledge": "sites/example-site/qa_knowledge.json",
            "workflows": "sites/example-site/workflows.json",
            "screenshots": "sites/example-site/screenshots",
            "sidecar": "qa_data.js",
        },
    },
}

EXAMPLE_HISTORY = [
    {"date": "2026-02-25 09:30", "page_count": 6, "avg_score": 5.8, "bug_count": 9, "slow_count": 4, "api_failure_count": 5, "avg_api_latency_ms": 810},
    {"date": "2026-02-26 09:30", "page_count": 7, "avg_score": 6.1, "bug_count": 8, "slow_count": 4, "api_failure_count": 4, "avg_api_latency_ms": 760},
    {"date": "2026-02-27 09:30", "page_count": 7, "avg_score": 6.5, "bug_count": 7, "slow_count": 3, "api_failure_count": 4, "avg_api_latency_ms": 725},
    {"date": "2026-02-28 09:30", "page_count": 8, "avg_score": 6.8, "bug_count": 6, "slow_count": 3, "api_failure_count": 3, "avg_api_latency_ms": 690},
    {"date": "2026-03-01 09:30", "page_count": 8, "avg_score": 7.0, "bug_count": 5, "slow_count": 2, "api_failure_count": 3, "avg_api_latency_ms": 655},
    {"date": "2026-03-02 09:30", "page_count": 8, "avg_score": 7.2, "bug_count": 4, "slow_count": 2, "api_failure_count": 3, "avg_api_latency_ms": 620},
]

EXAMPLE_DELTAS = {
    "score_delta": 0.2,
    "bug_delta": -1,
    "api_latency_delta_ms": -35,
    "new_routes": ["/reports/release-readiness", "/projects/alpha/assets"],
    "removed_routes": [],
}

EXAMPLE_ROUTE_STATS = [
    {"route": "/", "novelty": 6.4, "visits": 3, "links_pending": 1, "exhausted": False},
    {"route": "/auth/sign-in", "novelty": 4.2, "visits": 4, "links_pending": 0, "exhausted": True},
    {"route": "/dashboard", "novelty": 8.7, "visits": 2, "links_pending": 3, "exhausted": False},
    {"route": "/projects/alpha", "novelty": 7.8, "visits": 2, "links_pending": 2, "exhausted": False},
    {"route": "/projects/alpha/assets", "novelty": 6.1, "visits": 1, "links_pending": 1, "exhausted": False},
    {"route": "/projects/alpha/storyboard", "novelty": 9.2, "visits": 1, "links_pending": 2, "exhausted": False},
    {"route": "/settings/team", "novelty": 3.0, "visits": 4, "links_pending": 0, "exhausted": True},
    {"route": "/reports/release-readiness", "novelty": 5.6, "visits": 1, "links_pending": 0, "exhausted": False},
]

EXAMPLE_FRONTIER = [
    {"route": "/projects/alpha/storyboard", "url": "https://example-app.local/projects/alpha/storyboard", "score": 9.2, "cost": 1.4, "links_pending": 2, "visits": 1, "exhausted": False},
    {"route": "/dashboard", "url": "https://example-app.local/dashboard", "score": 8.7, "cost": 1.8, "links_pending": 3, "visits": 2, "exhausted": False},
    {"route": "/projects/alpha", "url": "https://example-app.local/projects/alpha", "score": 7.8, "cost": 2.1, "links_pending": 2, "visits": 2, "exhausted": False},
    {"route": "/reports/release-readiness", "url": "https://example-app.local/reports/release-readiness", "score": 5.6, "cost": 3.7, "links_pending": 0, "visits": 1, "exhausted": False},
]

EXAMPLE_BATCH = [
    {"route": "/projects/alpha/storyboard", "url": "https://example-app.local/projects/alpha/storyboard", "score": 9.2, "cost": 1.4, "links_pending": 2},
    {"route": "/dashboard", "url": "https://example-app.local/dashboard", "score": 8.7, "cost": 1.8, "links_pending": 3},
]

EXAMPLE_KB_SUMMARY = {"run": 12, "routes": 8, "elements": 154, "skippable": 28}

HEATMAP_COUNTS = {
    "/": 3,
    "/auth/sign-in": 4,
    "/dashboard": 7,
    "/projects/alpha": 5,
    "/projects/alpha/assets": 4,
    "/projects/alpha/storyboard": 6,
    "/settings/team": 3,
    "/reports/release-readiness": 2,
}

ACTIVE_URLS = [
    "https://example-app.local/projects/alpha/storyboard",
    "https://example-app.local/dashboard",
]

ACTIVE_ROUTES = ["/projects/alpha/storyboard", "/dashboard"]


def generate() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
        page.goto(DASHBOARD_HTML, wait_until="domcontentloaded")
        page.evaluate("sessionStorage.setItem('qa_cleared', '1')")
        page.reload(wait_until="domcontentloaded")
        page.add_style_tag(
            content="""
                *, *::before, *::after {
                    animation: none !important;
                    transition: none !important;
                    caret-color: transparent !important;
                }
                body { overflow: hidden !important; }
            """
        )
        page.evaluate(
            """async (payload) => {
                sessionStorage.removeItem('qa_cleared');
                window.__QA_STATE__ = payload.state;
                window.__QA_HISTORY__ = payload.history;
                window.__QA_KNOWLEDGE__ = {};
                await hydratFromState();
                renderHistory(payload.history, payload.deltas);
                explorerRouteStats = payload.routeStats;
                explorerFrontier = payload.frontier;
                explorerCurrentBatch = payload.batch;
                explorerKbSummary = payload.kbSummary;
                urlVisits = payload.heatmapCounts;
                document.getElementById('hQueue').textContent = String(payload.frontier.length);
                document.getElementById('scanFill').style.width = '72%';
                updateCoverageBars(payload.state.coverage);
                setActiveScan(payload.activeUrls, payload.activeRoutes, 'Example batch');
                setConn('live', 'Example data');
                log('Loaded synthetic example dataset for docs', 'ok');
                renderExplorer();
                rebuildFocusTree();
                renderTree();
            }""",
            {
                "state": EXAMPLE_STATE,
                "history": EXAMPLE_HISTORY,
                "deltas": EXAMPLE_DELTAS,
                "routeStats": EXAMPLE_ROUTE_STATS,
                "frontier": EXAMPLE_FRONTIER,
                "batch": EXAMPLE_BATCH,
                "kbSummary": EXAMPLE_KB_SUMMARY,
                "heatmapCounts": HEATMAP_COUNTS,
                "activeUrls": ACTIVE_URLS,
                "activeRoutes": ACTIVE_ROUTES,
            },
        )
        page.wait_for_timeout(1200)

        shots = [
            ("dashboard-overview.png", "Pages", None),
            ("dashboard-heatmap.png", "Heatmap", "#hmList .hm-row"),
            ("dashboard-history.png", "History", ".history-table"),
            ("dashboard-route-graph.png", "Route Graph", "#graphCanvas"),
            ("dashboard-route-tree.png", "Route Tree", "#treeBox .tree2-row"),
            ("dashboard-explorer.png", "Explorer", "#explorerContent"),
        ]
        for filename, tab_label, ready_selector in shots:
            if tab_label != "Pages":
                page.locator(".tab-btn", has_text=tab_label).click()
                page.wait_for_timeout(900)
            if ready_selector:
                page.locator(ready_selector).first.wait_for(state="visible", timeout=10000)
                page.wait_for_timeout(350)
            page.screenshot(path=str(OUTPUT_DIR / filename))

        browser.close()


if __name__ == "__main__":
    generate()
