from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from typing import Any

from .graph_evidence import build_state_graph_evidence
from .utils import hash_text


class InteractionRecord:
    """Tracks reliability of a single UI action across attempts."""

    def __init__(self, action: str, route: str) -> None:
        self.action = action
        self.route = route
        self.attempts = 0
        self.success = 0   # navigation / dom_mutation
        self.neutral = 0   # no_change
        self.fail = 0      # api_error / timeout
        self.last_seen: float = 0.0
        self.outcomes: list[str] = []

    def record(self, outcome: str) -> None:
        self.attempts += 1
        self.last_seen = time.time()
        self.outcomes.append(outcome)
        if outcome in ("navigation", "dom_mutation", "modal_open"):
            self.success += 1
        elif outcome in ("api_error", "broken", "timeout"):
            self.fail += 1
        else:
            self.neutral += 1

    @property
    def reliability(self) -> float:
        if self.attempts == 0:
            return 0.0
        return round(self.success / self.attempts, 3)

    @property
    def flakiness(self) -> float:
        """High when action alternates between success and neutral/fail."""
        if self.attempts < 2:
            return 0.0
        alternations = sum(
            1 for i in range(1, len(self.outcomes))
            if self.outcomes[i] != self.outcomes[i - 1]
        )
        return round(alternations / (self.attempts - 1), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "route": self.route,
            "attempts": self.attempts,
            "success": self.success,
            "neutral": self.neutral,
            "fail": self.fail,
            "last_seen": self.last_seen,
            "reliability": self.reliability,
            "flakiness": self.flakiness,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "InteractionRecord":
        rec = cls(d["action"], d["route"])
        rec.attempts = d.get("attempts", 0)
        rec.success = d.get("success", 0)
        rec.neutral = d.get("neutral", 0)
        rec.fail = d.get("fail", 0)
        rec.last_seen = d.get("last_seen", 0.0)
        return rec


class StateGraph:
    """Persisted route/state/action evidence for the crawl."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {
            "summary": {},
            "states": [],
            "edges": [],
            "actions": [],
            "roots": [],
            "route_links": [],
        }

    def rebuild(self, pages: list[dict[str, Any]]) -> None:
        self.data = build_state_graph_evidence(pages)

    def unexplored_high_value_routes(self, visited_routes: set[str], limit: int = 20) -> list[str]:
        """Return routes that exist as targets but haven't been visited."""
        target_routes: set[str] = set()
        for edge in self.data.get("edges", []) or []:
            route = str(edge.get("to_route", "") or "")
            if route:
                target_routes.add(route)
        for link in self.data.get("route_links", []) or []:
            route = str(link.get("to_route", "") or "")
            if route:
                target_routes.add(route)
        return list(sorted(target_routes - visited_routes))[:limit]

    def to_dict(self) -> dict[str, Any]:
        return self.data

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StateGraph":
        g = cls()
        if isinstance(d, dict):
            g.data = d
        return g


class CoverageKPIs:
    """Track coverage metrics across a crawl run."""

    def __init__(self) -> None:
        self.total_pages = 0
        self.pages_with_interactions = 0
        self.unique_actions_seen: set[str] = set()
        self.critical_workflows_defined: list[str] = []
        self.critical_workflows_completed: set[str] = set()

    @property
    def pct_pages_with_interactions(self) -> float:
        if self.total_pages == 0:
            return 0.0
        return round(self.pages_with_interactions / self.total_pages * 100, 1)

    @property
    def unique_actions_count(self) -> int:
        return len(self.unique_actions_seen)

    @property
    def pct_critical_workflows(self) -> float:
        if not self.critical_workflows_defined:
            return 100.0
        return round(
            len(self.critical_workflows_completed) / len(self.critical_workflows_defined) * 100, 1
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_pages": self.total_pages,
            "pages_with_interactions": self.pages_with_interactions,
            "pct_pages_with_interactions": self.pct_pages_with_interactions,
            "unique_actions_count": self.unique_actions_count,
            "unique_actions": list(self.unique_actions_seen),
            "critical_workflows_defined": self.critical_workflows_defined,
            "critical_workflows_completed": list(self.critical_workflows_completed),
            "pct_critical_workflows": self.pct_critical_workflows,
        }


class CrawlState:
    def __init__(self) -> None:
        self.visited: set[str] = set()
        self.queue: list[str] = []
        self.pages_data: list[dict[str, Any]] = []
        self.error_map: dict[str, list[str]] = {}
        self.api_log: list[dict[str, Any]] = []
        self.retries: dict[str, int] = {}
        self.retry_classification: dict[str, str] = {}
        # New in v2
        self.interaction_records: dict[str, InteractionRecord] = {}
        self.state_graph = StateGraph()
        self.coverage = CoverageKPIs()
        self.workflow_results: list[dict[str, Any]] = []

    # ── Queue management ──────────────────────────────────────────────────

    def enqueue(self, url: str, base_url: str, skip_paths: list[str]) -> None:
        from .utils import clean_url, same_origin, should_skip

        url = clean_url(url)
        if (
            url
            and url not in self.visited
            and url not in self.queue
            and same_origin(url, base_url)
            and not should_skip(url, skip_paths)
        ):
            self.queue.append(url)

    # ── Error deduplication ────────────────────────────────────────────────

    def register_errors(
        self, url: str, js_errors: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        seen = self.error_map.get(url, [])
        new_errors = []
        for e in js_errors:
            text = e.get("text", "")
            h = hash_text(text)
            if h not in seen:
                seen.append(h)
                new_errors.append(e)
        self.error_map[url] = seen
        return new_errors

    # ── Interaction reliability ────────────────────────────────────────────

    def record_interaction(self, action: str, route: str, outcome: str) -> None:
        key = hash_text(f"{route}|{action}", 12)
        if key not in self.interaction_records:
            self.interaction_records[key] = InteractionRecord(action, route)
        self.interaction_records[key].record(outcome)
        self.coverage.unique_actions_seen.add(f"{route}::{action}")

    def top_flaky_actions(self, n: int = 10) -> list[dict[str, Any]]:
        recs = sorted(
            self.interaction_records.values(),
            key=lambda r: (-r.flakiness, -r.attempts),
        )
        return [r.to_dict() for r in recs[:n] if r.flakiness > 0]

    def top_no_change_actions(self, n: int = 10) -> list[dict[str, Any]]:
        recs = sorted(
            self.interaction_records.values(),
            key=lambda r: (-(r.neutral / max(r.attempts, 1)), -r.attempts),
        )
        return [r.to_dict() for r in recs[:n] if r.neutral > 0]

    def all_interaction_stats(self) -> list[dict[str, Any]]:
        return sorted(
            [r.to_dict() for r in self.interaction_records.values()],
            key=lambda r: (-r["attempts"], r["route"]),
        )

    # ── Graph ─────────────────────────────────────────────────────────────

    def update_graph(self, page_data: dict[str, Any]) -> None:
        self.state_graph.rebuild(self.pages_data)

    # ── Coverage updates ──────────────────────────────────────────────────

    def update_coverage(self, page_data: dict[str, Any]) -> None:
        self.coverage.total_pages += 1
        if page_data.get("interaction_results"):
            self.coverage.pages_with_interactions += 1
        self.update_graph(page_data)

    # ── Snapshot ──────────────────────────────────────────────────────────

    def save_snapshot(self, path: str) -> None:
        data = {
            "visited": list(self.visited),
            "queue": list(self.queue),
            "pages_data": self.pages_data,
            "error_map": self.error_map,
            "api_log": self.api_log[-500:],
            "retries": self.retries,
            "retry_classification": self.retry_classification,
            "interaction_records": {
                k: v.to_dict() for k, v in self.interaction_records.items()
            },
            "state_graph": self.state_graph.to_dict(),
            "coverage": self.coverage.to_dict(),
            "workflow_results": self.workflow_results,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    @classmethod
    def load_snapshot(cls, path: str) -> "CrawlState":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        s = cls()
        s.visited = set(data.get("visited", []))
        s.queue = data.get("queue", [])
        s.pages_data = data.get("pages_data", [])
        s.error_map = data.get("error_map", {})
        s.api_log = data.get("api_log", [])
        s.retries = data.get("retries", {})
        s.retry_classification = data.get("retry_classification", {})
        for k, v in data.get("interaction_records", {}).items():
            s.interaction_records[k] = InteractionRecord.from_dict(v)
        if "state_graph" in data:
            s.state_graph = StateGraph.from_dict(data["state_graph"])
        if "workflow_results" in data:
            s.workflow_results = data["workflow_results"]
        # Rebuild coverage counters from pages_data
        s.coverage.total_pages = len(s.pages_data)
        s.coverage.pages_with_interactions = sum(
            1 for p in s.pages_data if p.get("interaction_results")
        )
        s.coverage.unique_actions_seen = set(data.get("coverage", {}).get("unique_actions", []))
        return s
