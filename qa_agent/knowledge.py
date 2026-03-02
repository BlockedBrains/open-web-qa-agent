"""
knowledge.py — Cross-run learning + Dijkstra-style frontier management.

Philosophy
──────────
The QA agent is a graph explorer. Each URL is a node. Each link/button-that-
navigates is a weighted edge. We want to visit HIGH-VALUE nodes we haven't
seen yet, and SKIP nodes we already know well.

Dijkstra analogy:
  - Node cost      = how much we already know about that route
                     (visits × element coverage × score stability)
  - Edge weight    = novelty: 0 for never-visited, high for well-known
  - Priority queue = URLs ordered by (novelty_score DESC, known_issues ASC)
                     → always explore the most promising unknown territory first

Element learning:
  - Every element gets a "skip_score" that rises when outcome is no_change
  - Elements with skip_score ≥ SKIP_THRESHOLD are not re-clicked next run
  - Elements that produced navigation/modal stay priority forever
  - New elements (not in KB at all) always get clicked first
"""
from __future__ import annotations

import heapq
import json
import os
import time
from typing import Any

from .heuristics import FORM_ACTION_KINDS, classify_route, summarize_interactions

KNOWLEDGE_FILE   = "qa_knowledge.json"
SKIP_THRESHOLD   = 3      # skip element after this many consecutive no_change
MAX_ROUTE_VISITS = 3      # after this many full visits, a route is "known"


# ═══════════════════════════════════════════════════════════════════════════
#  ELEMENT RECORD
# ═══════════════════════════════════════════════════════════════════════════

class ElementRecord:
    """Tracks a single interactive element across multiple runs."""
    __slots__ = (
        "selector", "text", "route", "seen_runs", "outcomes",
        "discovered_urls", "first_seen", "last_seen", "last_useful",
        "broke", "priority", "skip_score", "new_elements_found",
        "attempts", "success_count", "neutral_count", "fail_count",
        "discovery_count", "same_page_count",
    )

    def __init__(self, selector: str, text: str, route: str) -> None:
        self.selector = selector
        self.text = text
        self.route = route
        self.seen_runs = 0
        self.outcomes: list[str] = []
        self.discovered_urls: list[str] = []
        self.first_seen = time.time()
        self.last_seen = time.time()
        self.last_useful = 0.0
        self.broke: list[str] = []
        self.priority = 1.0
        self.skip_score = 0
        self.new_elements_found = 0
        self.attempts = 0
        self.success_count = 0
        self.neutral_count = 0
        self.fail_count = 0
        self.discovery_count = 0
        self.same_page_count = 0

    @property
    def key(self) -> str:
        return f"{self.route}::{self.text[:60].lower()}"

    @property
    def reliability(self) -> float:
        if self.attempts == 0:
            return 0.0
        return round(self.success_count / self.attempts, 3)

    @property
    def no_change_rate(self) -> float:
        if self.attempts == 0:
            return 0.0
        return round(self.neutral_count / self.attempts, 3)

    @property
    def fail_rate(self) -> float:
        if self.attempts == 0:
            return 0.0
        return round(self.fail_count / self.attempts, 3)

    @property
    def should_skip(self) -> bool:
        return (
            self.attempts >= SKIP_THRESHOLD
            and self.skip_score >= SKIP_THRESHOLD
            and self.no_change_rate >= 0.72
            and self.priority < 1.7
            and self.fail_count == 0
            and not self.broke
        )

    def record(
        self,
        outcome: str,
        broke: list[str],
        discovered: list[str],
        meta: dict[str, Any] | None = None,
    ) -> None:
        meta = meta or {}
        self.seen_runs += 1
        self.attempts += 1
        now = time.time()
        self.last_seen = now
        self.outcomes.append(outcome)
        self.broke.extend(broke[:3])
        new_d = [u for u in discovered if u not in self.discovered_urls]
        self.discovered_urls.extend(new_d[:5])
        self.new_elements_found += len(new_d)
        useful_same_page = bool(meta.get("same_page_transition") or int(meta.get("surface_delta", 0) or 0) > 0)
        value_changed = bool(meta.get("value_changed"))
        useful_signal = bool(new_d or useful_same_page or value_changed)
        discovery_gain = len(new_d) + (1 if useful_same_page else 0) + (1 if value_changed else 0)
        self.discovery_count += discovery_gain

        if outcome in ("navigation", "modal_open", "dom_mutation"):
            self.success_count += 1
            self.priority = min(self.priority + 0.55 + len(new_d) * 0.08, 5.0)
            self.skip_score = 0
            self.last_useful = now
            if outcome in ("modal_open", "dom_mutation"):
                self.same_page_count += 1
        elif outcome in ("broken", "api_error"):
            self.fail_count += 1
            self.priority = max(self.priority - 0.12, 0.2)
            self.skip_score = 0
        elif outcome == "timeout":
            self.fail_count += 1
            self.priority = max(self.priority - 0.18, 0.2)
        else:
            self.neutral_count += 1
            if outcome == "no_change":
                self.skip_score += 1
                self.priority = max(self.priority - 0.1, 0.1)

        if useful_same_page and outcome not in ("modal_open", "dom_mutation"):
            self.same_page_count += 1
        if useful_signal:
            self.priority = min(
                self.priority + min(len(new_d) * 0.06, 0.3) + (0.12 if useful_same_page else 0.0) + (0.08 if value_changed else 0.0),
                5.0,
            )
            self.last_useful = now
        if meta.get("action_kind") in FORM_ACTION_KINDS and useful_signal:
            self.priority = min(self.priority + 0.08, 5.0)

    def to_dict(self) -> dict[str, Any]:
        return dict(
            selector=self.selector,
            text=self.text,
            route=self.route,
            seen_runs=self.seen_runs,
            outcomes=self.outcomes[-10:],
            discovered_urls=list(dict.fromkeys(self.discovered_urls))[:10],
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            last_useful=self.last_useful,
            broke=self.broke[-5:],
            priority=round(self.priority, 3),
            skip_score=self.skip_score,
            new_elements_found=self.new_elements_found,
            attempts=self.attempts,
            success_count=self.success_count,
            neutral_count=self.neutral_count,
            fail_count=self.fail_count,
            discovery_count=self.discovery_count,
            same_page_count=self.same_page_count,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "ElementRecord":
        r = cls(d.get("selector", ""), d.get("text", ""), d.get("route", ""))
        r.seen_runs = d.get("seen_runs", 0)
        r.outcomes = d.get("outcomes", [])
        r.discovered_urls = d.get("discovered_urls", [])
        r.first_seen = d.get("first_seen", time.time())
        r.last_seen = d.get("last_seen", time.time())
        r.last_useful = d.get("last_useful", 0.0)
        r.broke = d.get("broke", [])
        r.priority = d.get("priority", 1.0)
        r.skip_score = d.get("skip_score", 0)
        r.new_elements_found = d.get("new_elements_found", 0)
        r.attempts = d.get("attempts", r.seen_runs)
        r.success_count = d.get("success_count", 0)
        r.neutral_count = d.get("neutral_count", 0)
        r.fail_count = d.get("fail_count", 0)
        r.discovery_count = d.get("discovery_count", r.new_elements_found)
        r.same_page_count = d.get("same_page_count", 0)
        return r


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTE KNOWLEDGE
# ═══════════════════════════════════════════════════════════════════════════

class RouteKnowledge:
    def __init__(self, route: str) -> None:
        self.route           = route
        self.visit_count     = 0
        self.avg_score       = 5.0
        self.elements: dict[str, ElementRecord] = {}
        self.known_links: set[str] = set()     # every URL ever discovered from this route
        self.unvisited_links: set[str] = set() # subset: discovered but never crawled
        self.last_screenshot = ""
        self.last_visited    = 0.0
        self.element_count_history: list[int] = []  # how many elements found per visit
        self.action_count_history: list[int] = []
        self.state_count_history: list[int] = []
        self.discovery_action_history: list[int] = []
        self.link_yield_history: list[int] = []
        self.form_action_history: list[int] = []
        self.page_kind = ""
        self.business_area = ""
        self.hub_score = 0.35

    @property
    def novelty_score(self) -> float:
        """
        How much is left to learn about this route? 0 = fully known, 10 = brand new.
        Used by Dijkstra priority queue.
        """
        if self.visit_count == 0:
            return 10.0
        # Penalise for how many visits we've done
        visit_penalty = min(self.visit_count / MAX_ROUTE_VISITS, 1.0) * 4.0
        # Bonus if we're still finding new elements
        discovery_bonus = 0.0
        if len(self.element_count_history) >= 2:
            prev, curr = self.element_count_history[-2], self.element_count_history[-1]
            if curr > prev:
                discovery_bonus = min((curr - prev) * 0.5, 3.0)
        # Bonus for unvisited links we know about
        unvisited_bonus = min(len(self.unvisited_links) * 0.3, 2.0)
        # Penalty for stable (boring) score
        score_stability = abs(self.avg_score - 5.0) * 0.2

        action_growth = 0.0
        if len(self.action_count_history) >= 2:
            prev_a, curr_a = self.action_count_history[-2], self.action_count_history[-1]
            if curr_a > prev_a:
                action_growth = min((curr_a - prev_a) * 0.08, 1.2)
        raw = 10.0 - visit_penalty + discovery_bonus + unvisited_bonus + action_growth + self.hub_score * 1.8 - score_stability
        return max(0.0, min(10.0, round(raw, 2)))

    @property
    def is_exhausted(self) -> bool:
        """True when we've visited enough and found no new elements for 2+ visits."""
        if self.visit_count < 2:
            return False
        if len(self.element_count_history) < 2:
            return False
        if self.unvisited_links:
            return False
        if self.hub_score >= 0.65 and any(v > 0 for v in self.link_yield_history[-2:]):
            return False
        # No new elements in last 2 visits AND visited enough
        stable = all(
            self.element_count_history[-i] <= self.element_count_history[-i-1]
            for i in range(1, min(3, len(self.element_count_history)))
        )
        state_flat = len(self.state_count_history) < 2 or self.state_count_history[-1] <= max(self.state_count_history[-2], 1)
        return stable and state_flat and self.visit_count >= MAX_ROUTE_VISITS

    def skippable_elements(self) -> set[str]:
        """Keys of elements that should be skipped this run."""
        return {k for k, el in self.elements.items() if el.should_skip}

    def clickable_elements_for_run(self, limit: int = 30) -> list[str]:
        """
        Returns element *texts* worth clicking this run.
        - New elements (not skippable) first
        - High-priority known elements second
        - Skips boring no-change elements
        """
        skip = self.skippable_elements()
        active = [
            el for k, el in self.elements.items()
            if k not in skip
        ]
        active.sort(key=lambda e: (-(e.priority + e.reliability + e.discovery_count * 0.03), e.seen_runs))
        return [e.text for e in active[:limit]]

    def merge(self, page_data: dict[str, Any], n_elements_found: int = 0) -> None:
        self.visit_count += 1
        self.last_visited  = time.time()
        score = float((page_data.get("analysis") or {}).get("health_score") or 5)
        n = self.visit_count
        self.avg_score = round((self.avg_score * (n-1) + score) / n, 2)
        if page_data.get("screenshot"):
            self.last_screenshot = page_data["screenshot"]
        for url in page_data.get("discovered_links", []):
            self.known_links.add(url)
            self.unvisited_links.add(url)
        self.element_count_history.append(n_elements_found)
        interaction_stats = summarize_interactions(page_data.get("interaction_results", []))
        route_meta = classify_route(
            self.route,
            title=str(page_data.get("title", "") or ""),
            interactions=page_data.get("interaction_results", []),
            links_found=len(page_data.get("discovered_links", [])),
            states_seen=int(page_data.get("states_seen", 0) or 0),
        )
        self.page_kind = str((page_data.get("analysis") or {}).get("route_kind") or route_meta["page_kind"])
        self.business_area = str((page_data.get("analysis") or {}).get("business_area") or route_meta["business_area"])
        self.hub_score = round((self.hub_score * (n - 1) + route_meta["hub_score"]) / n, 3)
        self.action_count_history.append(interaction_stats["attempts"])
        self.state_count_history.append(int(page_data.get("states_seen", 0) or 0))
        self.discovery_action_history.append(interaction_stats["discovery_actions"])
        self.link_yield_history.append(len(page_data.get("discovered_links", [])))
        self.form_action_history.append(interaction_stats["form_actions"])
        if len(self.element_count_history) > 10:
            self.element_count_history = self.element_count_history[-10:]
        if len(self.action_count_history) > 10:
            self.action_count_history = self.action_count_history[-10:]
            self.state_count_history = self.state_count_history[-10:]
            self.discovery_action_history = self.discovery_action_history[-10:]
            self.link_yield_history = self.link_yield_history[-10:]
            self.form_action_history = self.form_action_history[-10:]

    def mark_visited(self, url: str) -> None:
        self.unvisited_links.discard(url)

    def to_dict(self) -> dict[str, Any]:
        return dict(
            route=self.route,
            visit_count=self.visit_count,
            avg_score=self.avg_score,
            elements={k: v.to_dict() for k,v in self.elements.items()},
            known_links=list(self.known_links)[:400],
            unvisited_links=list(self.unvisited_links)[:200],
            last_screenshot=self.last_screenshot,
            last_visited=self.last_visited,
            element_count_history=self.element_count_history,
            action_count_history=self.action_count_history,
            state_count_history=self.state_count_history,
            discovery_action_history=self.discovery_action_history,
            link_yield_history=self.link_yield_history,
            form_action_history=self.form_action_history,
            page_kind=self.page_kind,
            business_area=self.business_area,
            hub_score=round(self.hub_score, 3),
        )

    @classmethod
    def from_dict(cls, d: dict) -> "RouteKnowledge":
        rk = cls(d["route"])
        rk.visit_count              = d.get("visit_count", 0)
        rk.avg_score                = d.get("avg_score", 5.0)
        rk.known_links              = set(d.get("known_links", []))
        rk.unvisited_links          = set(d.get("unvisited_links", []))
        rk.last_screenshot          = d.get("last_screenshot","")
        rk.last_visited             = d.get("last_visited", 0.0)
        rk.element_count_history    = d.get("element_count_history", [])
        rk.action_count_history     = d.get("action_count_history", [])
        rk.state_count_history      = d.get("state_count_history", [])
        rk.discovery_action_history = d.get("discovery_action_history", [])
        rk.link_yield_history       = d.get("link_yield_history", [])
        rk.form_action_history      = d.get("form_action_history", [])
        rk.page_kind                = d.get("page_kind", "")
        rk.business_area            = d.get("business_area", "")
        rk.hub_score                = d.get("hub_score", 0.35)
        for k, v in d.get("elements", {}).items():
            rk.elements[k] = ElementRecord.from_dict(v)
        return rk


# ═══════════════════════════════════════════════════════════════════════════
#  DIJKSTRA-STYLE URL FRONTIER
# ═══════════════════════════════════════════════════════════════════════════

class UrlFrontier:
    """
    Priority queue of URLs to crawl, ordered by exploration value.

    Score formula (higher = crawl sooner):
      novelty        — routes never visited get full 10 points
      has_issues     — routes with known bugs get +2 (need re-checking)
      links_pending  — routes that have undiscovered branches get +1.5
      visit_penalty  — each visit subtracts 1.5 (diminishing returns)
      freshness      — if route changed recently (new elements found), +1

    This is Dijkstra-inspired: we always pick the cheapest un-explored node,
    where "cost" = how little we know (inverted novelty).
    """
    def __init__(self) -> None:
        self._heap: list[tuple[float, str]] = []  # (neg_score, url)
        self._in_heap: set[str] = set()

    def push(self, url: str, score: float) -> None:
        if url not in self._in_heap:
            heapq.heappush(self._heap, (-score, url))
            self._in_heap.add(url)

    def pop(self) -> str | None:
        while self._heap:
            _, url = heapq.heappop(self._heap)
            self._in_heap.discard(url)
            return url
        return None

    def snapshot(self, limit: int | None = None) -> list[tuple[float, str]]:
        items = sorted(((-score, url) for score, url in self._heap), key=lambda item: (-item[0], item[1]))
        if limit is not None:
            items = items[:limit]
        return items

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)


# ═══════════════════════════════════════════════════════════════════════════
#  KNOWLEDGE BASE
# ═══════════════════════════════════════════════════════════════════════════

class KnowledgeBase:
    """
    Persistent cross-run learning store + Dijkstra frontier advisor.

    The central brain of the agent. On run N it knows:
      - Every route ever visited and how many times
      - Every element ever clicked on every route and what happened
      - Which URLs were found but never crawled (frontier candidates)
      - Which elements are boring (should be skipped)
      - Which routes still have unexplored territory
    """

    def __init__(self, path: str = KNOWLEDGE_FILE) -> None:
        self.path               = path
        self.routes: dict[str, RouteKnowledge] = {}
        self.run_count          = 0
        self.global_visited: set[str] = set()        # all URLs ever crawled across all runs
        self.effective_selectors: set[str] = set()    # selectors that produced nav/modal

    # ── Persistence ───────────────────────────────────────────────────────

    def load(self) -> bool:
        if not os.path.exists(self.path):
            print("[KB] Fresh start — no knowledge base yet (run #1)")
            return False
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.run_count          = data.get("run_count", 0)
            self.global_visited     = set(data.get("global_visited", []))
            self.effective_selectors = set(data.get("effective_selectors", []))
            for route, rd in data.get("routes", {}).items():
                self.routes[route] = RouteKnowledge.from_dict(rd)
            total_el    = sum(len(rk.elements) for rk in self.routes.values())
            total_skip  = sum(len(rk.skippable_elements()) for rk in self.routes.values())
            total_links = sum(len(rk.unvisited_links) for rk in self.routes.values())
            print(f"[KB] Run #{self.run_count} → loaded {len(self.routes)} routes, "
                  f"{total_el} elements ({total_skip} skippable), "
                  f"{total_links} unvisited links queued")
            return True
        except Exception as e:
            print(f"[KB] Load error: {e} — starting fresh")
            return False

    def save(self) -> None:
        self.run_count += 1
        data = dict(
            run_count           = self.run_count,
            global_visited      = list(self.global_visited)[:2000],
            effective_selectors = list(self.effective_selectors)[:200],
            routes              = {r: rk.to_dict() for r, rk in self.routes.items()},
        )
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)
        total_el   = sum(len(rk.elements) for rk in self.routes.values())
        total_skip = sum(len(rk.skippable_elements()) for rk in self.routes.values())
        print(f"[KB] Saved run #{self.run_count}: {len(self.routes)} routes, "
              f"{total_el} elements, {total_skip} will be skipped next run")

    # ── Dijkstra frontier advisor ─────────────────────────────────────────

    def score_url(self, url: str) -> float:
        """
        Compute exploration priority score for a URL.
        Higher = crawl this sooner.
        """
        from .utils import canonicalize_path_from_url
        route = canonicalize_path_from_url(url)
        rk    = self.routes.get(route)

        if url not in self.global_visited:
            # Never visited at all → maximum priority
            base = 10.0
        elif rk is None:
            base = 8.0
        else:
            base = rk.novelty_score

        bonus = 0.0
        if rk:
            # Bonus: has known bugs (worth re-checking)
            if rk.avg_score < 5.0:
                bonus += 1.5
            # Bonus: has pending unvisited sub-links
            bonus += min(len(rk.unvisited_links) * 0.2, 2.0)
            bonus += rk.hub_score * 1.1
            if rk.page_kind in {"dashboard", "list", "help"}:
                bonus += 0.7
            elif rk.page_kind in {"detail", "editor"} and rk.hub_score < 0.45:
                bonus -= 0.4
            if rk.discovery_action_history:
                bonus += min(rk.discovery_action_history[-1] * 0.08, 1.0)
            # Penalty: exhausted route
            if rk.is_exhausted:
                bonus -= 3.0

        return max(0.0, base + bonus)

    def build_frontier(self, candidate_urls: list[str], visited_this_run: set[str]) -> UrlFrontier:
        """
        Build a Dijkstra priority queue from a list of discovered URLs.
        URLs already visited THIS run are excluded.
        Globally visited URLs get lower scores.
        Never-visited URLs get the highest scores.
        """
        frontier = UrlFrontier()
        for url in candidate_urls:
            if url in visited_this_run:
                continue
            score = self.score_url(url)
            frontier.push(url, score)
        return frontier

    def next_urls_to_crawl(
        self,
        known_urls: list[str],
        visited_this_run: set[str],
        limit: int = 50,
    ) -> list[tuple[float, str]]:
        """
        Return up to `limit` (score, url) pairs sorted highest-score-first.
        Injects previously-known but never-visited URLs automatically.
        """
        # Merge in all previously known unvisited links from every route
        all_candidates = set(known_urls)
        for rk in self.routes.values():
            all_candidates |= rk.unvisited_links

        scored: list[tuple[float, str]] = []
        for url in all_candidates:
            if url in visited_this_run:
                continue
            scored.append((self.score_url(url), url))

        scored.sort(key=lambda x: -x[0])
        return scored[:limit]

    # ── Element advisor ───────────────────────────────────────────────────

    def elements_to_skip(self, route: str) -> set[str]:
        """Element texts that should NOT be clicked on this route this run."""
        rk = self.routes.get(route)
        if not rk:
            return set()
        skip_keys = rk.skippable_elements()
        return {rk.elements[k].text for k in skip_keys if k in rk.elements}

    def priority_elements(self, route: str) -> set[str]:
        """Element texts that are known high-value — click these first."""
        rk = self.routes.get(route)
        if not rk:
            return set()
        skip = rk.skippable_elements()
        high = [
            el for k, el in rk.elements.items()
            if k not in skip and el.priority >= 1.5
        ]
        high.sort(key=lambda e: -(e.priority + e.reliability + e.discovery_count * 0.03))
        return {e.text for e in high[:20]}

    def is_route_exhausted(self, route: str) -> bool:
        """True when we know this route well and it has no new elements."""
        rk = self.routes.get(route)
        return rk.is_exhausted if rk else False

    # ── Write API ─────────────────────────────────────────────────────────

    def mark_url_visited(self, url: str) -> None:
        from .utils import canonicalize_path_from_url
        self.global_visited.add(url)
        route = canonicalize_path_from_url(url)
        # Mark this URL as visited in every route that knew about it
        for rk in self.routes.values():
            rk.mark_visited(url)

    def record_page(self, route: str, page_data: dict[str, Any], n_elements: int = 0) -> None:
        self._ensure(route).merge(page_data, n_elements_found=n_elements)

    def record_element(
        self,
        route: str,
        text: str,
        selector: str,
        outcome: str,
        broke: list[str],
        discovered: list[str],
        meta: dict[str, Any] | None = None,
    ) -> None:
        rk  = self._ensure(route)
        key = f"{route}::{text[:60].lower()}"
        if key not in rk.elements:
            rk.elements[key] = ElementRecord(selector, text, route)
        rk.elements[key].record(outcome, broke, discovered, meta=meta)
        if outcome in ("navigation", "modal_open", "dom_mutation") and selector:
            self.effective_selectors.add(selector)

    def record_link(self, from_route: str, url: str) -> None:
        rk = self._ensure(from_route)
        rk.known_links.add(url)
        if url not in self.global_visited:
            rk.unvisited_links.add(url)

    def summary(self) -> dict[str, Any]:
        total_el   = sum(len(rk.elements) for rk in self.routes.values())
        total_skip = sum(len(rk.skippable_elements()) for rk in self.routes.values())
        exhausted  = sum(1 for rk in self.routes.values() if rk.is_exhausted)
        unvisited  = sum(len(rk.unvisited_links) for rk in self.routes.values())
        return dict(
            run_count        = self.run_count,
            routes           = len(self.routes),
            routes_exhausted = exhausted,
            elements         = total_el,
            elements_skip    = total_skip,
            elements_active  = total_el - total_skip,
            global_visited   = len(self.global_visited),
            links_pending    = unvisited,
            hubs             = sum(1 for rk in self.routes.values() if rk.hub_score >= 0.65),
        )

    def route_stats(self) -> list[dict[str, Any]]:
        """Per-route stats for dashboard display."""
        out = []
        for route, rk in self.routes.items():
            skip = len(rk.skippable_elements())
            out.append({
                "route":         route,
                "visits":        rk.visit_count,
                "avg_score":     rk.avg_score,
                "novelty":       rk.novelty_score,
                "exhausted":     rk.is_exhausted,
                "elements":      len(rk.elements),
                "elements_skip": skip,
                "elements_new":  len(rk.elements) - skip,
                "links_pending": len(rk.unvisited_links),
                "page_kind":     rk.page_kind,
                "hub_score":     round(rk.hub_score, 3),
                "actions_seen":  rk.action_count_history[-1] if rk.action_count_history else 0,
                "states_seen":   rk.state_count_history[-1] if rk.state_count_history else 0,
            })
        out.sort(key=lambda r: -r["novelty"])
        return out

    def _ensure(self, route: str) -> RouteKnowledge:
        if route not in self.routes:
            self.routes[route] = RouteKnowledge(route)
        return self.routes[route]
