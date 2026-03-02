from __future__ import annotations

from typing import Any

from .utils import canonicalize_path_from_url


FORM_ACTION_KINDS = {"input", "select", "date", "checkbox", "radio"}
SUCCESS_OUTCOMES = {"navigation", "modal_open", "dom_mutation"}
FAIL_OUTCOMES = {"broken", "api_error", "timeout"}


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(pattern in lowered for pattern in patterns)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def summarize_interactions(interactions: list[dict[str, Any]] | None) -> dict[str, Any]:
    rows = interactions or []
    discovered_urls: set[str] = set()
    scopes: set[str] = set()
    states: set[str] = set()
    sections: set[str] = set()
    counts = {
        "attempts": 0,
        "success": 0,
        "fail": 0,
        "neutral": 0,
        "same_page_transitions": 0,
        "value_changes": 0,
        "form_actions": 0,
        "chrome_actions": 0,
        "deep_actions": 0,
        "api_failures": 0,
        "dom_delta": 0,
        "new_elements": 0,
        "modal_opens": 0,
        "navigations": 0,
        "dom_mutations": 0,
        "timeouts": 0,
        "form_submissions": 0,
        "validation_feedback": 0,
    }
    max_state_depth = 0
    for row in rows:
        outcome = str(row.get("outcome", "") or "")
        action_kind = str(row.get("action_kind", "") or "")
        scope_kind = str(row.get("scope_kind", "") or "")
        counts["attempts"] += 1
        counts["api_failures"] += int(row.get("api_failures", 0) or 0)
        counts["dom_delta"] += int(row.get("dom_delta", 0) or 0)
        max_state_depth = max(
            max_state_depth,
            int(row.get("state_depth", 0) or 0),
            int(row.get("next_state_depth", 0) or 0),
        )

        if outcome in SUCCESS_OUTCOMES:
            counts["success"] += 1
        elif outcome in FAIL_OUTCOMES:
            counts["fail"] += 1
        else:
            counts["neutral"] += 1

        if outcome == "navigation":
            counts["navigations"] += 1
        elif outcome == "modal_open":
            counts["modal_opens"] += 1
        elif outcome == "dom_mutation":
            counts["dom_mutations"] += 1
        elif outcome == "timeout":
            counts["timeouts"] += 1

        if row.get("same_page_transition"):
            counts["same_page_transitions"] += 1
        if row.get("value_changed"):
            counts["value_changes"] += 1
        if action_kind in FORM_ACTION_KINDS:
            counts["form_actions"] += 1
        if row.get("submitted"):
            counts["form_submissions"] += 1
        if row.get("validation_errors"):
            counts["validation_feedback"] += len(row.get("validation_errors", []) or [])
        if scope_kind == "chrome":
            counts["chrome_actions"] += 1
        if scope_kind in {"section", "tab", "modal", "form"}:
            counts["deep_actions"] += 1
        if row.get("is_new"):
            counts["new_elements"] += 1

        scope = str(
            row.get("scope_label")
            or row.get("section")
            or row.get("form_context")
            or row.get("tab_context")
            or row.get("modal_context")
            or ""
        ).strip()
        if scope:
            scopes.add(scope)
        section = str(row.get("section") or "").strip()
        if section:
            sections.add(section)
        for state_id in (row.get("from_state_id"), row.get("to_state_id")):
            if state_id:
                states.add(str(state_id))
        for url in row.get("discovered_urls", []) or []:
            if url:
                discovered_urls.add(str(url))

    attempts = counts["attempts"] or 1
    return {
        **counts,
        "discovered_urls": len(discovered_urls),
        "unique_scopes": len(scopes),
        "unique_sections": len(sections),
        "unique_states": len(states),
        "max_state_depth": max_state_depth,
        "reliability": round(counts["success"] / attempts, 3),
        "failure_rate": round(counts["fail"] / attempts, 3),
        "no_change_rate": round(counts["neutral"] / attempts, 3),
        "discovery_actions": sum(
            1
            for row in rows
            if row.get("same_page_transition")
            or row.get("value_changed")
            or int(row.get("surface_delta", 0) or 0) > 0
            or row.get("discovered_urls")
        ),
    }


def classify_route(
    route: str,
    *,
    title: str = "",
    interactions: list[dict[str, Any]] | None = None,
    links_found: int = 0,
    states_seen: int = 0,
) -> dict[str, Any]:
    route_text = (route or "/").lower()
    title_text = (title or "").lower()
    combined = f"{route_text} {title_text}"
    stats = summarize_interactions(interactions)
    segments = [segment for segment in route_text.split("/") if segment]
    business_area = segments[0] if segments else "root"

    if _contains_any(combined, ("login", "signin", "sign-in", "auth", "password", "otp")):
        page_kind = "auth"
        base_hub = 0.2
    elif _contains_any(combined, ("dashboard", "overview", "home")):
        page_kind = "dashboard"
        base_hub = 0.92
    elif _contains_any(combined, ("settings", "preferences", "configuration", "billing", "admin")):
        page_kind = "settings"
        base_hub = 0.7
    elif _contains_any(combined, ("help", "support", "docs", "faq", "knowledge")):
        page_kind = "help"
        base_hub = 0.66
    elif _contains_any(combined, ("new", "create", "invite", "edit", "compose", "builder", "wizard", "form")):
        page_kind = "form"
        base_hub = 0.48
    elif _contains_any(combined, ("editor", "canvas", "studio", "builder", "composer")):
        page_kind = "editor"
        base_hub = 0.45
    elif ":id" in route_text or stats["form_actions"] >= 3 or stats["unique_states"] >= 4:
        page_kind = "detail"
        base_hub = 0.34
    elif links_found >= 8 or _contains_any(combined, ("list", "index", "projects", "files", "members", "characters", "tasks", "library")):
        page_kind = "list"
        base_hub = 0.82
    else:
        page_kind = "page"
        base_hub = 0.4

    discovery_bonus = min(
        links_found * 0.025
        + stats["same_page_transitions"] * 0.05
        + stats["unique_sections"] * 0.03
        + states_seen * 0.025,
        0.38,
    )
    if stats["chrome_actions"] and stats["chrome_actions"] >= max(2, stats["attempts"] // 2):
        discovery_bonus -= 0.06
    hub_score = round(max(0.05, min(1.0, base_hub + discovery_bonus)), 3)
    expected = {
        "links": 6 if page_kind in {"dashboard", "list", "help"} else 2 if page_kind == "settings" else 1,
        "forms": 2 if page_kind in {"settings", "form", "editor"} else 0,
        "states": 4 if page_kind in {"detail", "editor", "form"} else 2 if page_kind in {"dashboard", "settings"} else 1,
    }
    return {
        "page_kind": page_kind,
        "business_area": business_area,
        "hub_score": hub_score,
        "expected": expected,
        "interaction_profile": stats,
    }


def build_route_snapshot(pages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        route = canonicalize_path_from_url(page.get("url", ""))
        grouped.setdefault(route, []).append(page)

    snapshot: dict[str, dict[str, Any]] = {}
    for route, rows in grouped.items():
        page_scores = [
            _to_float((row.get("analysis") or {}).get("health_score", 5), 5.0)
            for row in rows
        ]
        interactions = [ir for row in rows for ir in (row.get("interaction_results") or [])]
        stats = summarize_interactions(interactions)
        snapshot[route] = {
            "avg_score": round(sum(page_scores) / max(len(page_scores), 1), 2),
            "pages": len(rows),
            "broken": sum(int(row.get("broken_interactions", 0) or 0) for row in rows),
            "api_failures": sum(int(row.get("api_failures", 0) or 0) for row in rows),
            "links_found": sum(int(row.get("links_found", 0) or 0) for row in rows),
            "states_seen": sum(int(row.get("states_seen", 0) or 0) for row in rows),
            "actions_seen": stats["attempts"],
            "discovery_actions": stats["discovery_actions"],
            "same_page_transitions": stats["same_page_transitions"],
            "max_state_depth": stats["max_state_depth"],
            "form_actions": stats["form_actions"],
            "form_submissions": stats["form_submissions"],
            "validation_feedback": stats["validation_feedback"],
            "route_kind": str((rows[0].get("analysis") or {}).get("route_kind", "")),
        }
    return snapshot
