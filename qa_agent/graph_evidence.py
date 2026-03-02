from __future__ import annotations

from collections import defaultdict
from typing import Any

from .utils import canonicalize_path_from_url, hash_text


def interaction_creates_transition(interaction: dict[str, Any]) -> bool:
    from_route = str(interaction.get("from_route", "") or "")
    to_route = str(interaction.get("to_route", "") or from_route)
    scope_kind = str(interaction.get("scope_kind", "") or "page")
    from_state_id = str(interaction.get("from_state_id", "") or "")
    to_state_id = str(interaction.get("to_state_id", "") or from_state_id)
    outcome = str(interaction.get("outcome", "") or "unknown")

    if not from_route:
        return False
    if to_route and to_route != from_route:
        return True
    if scope_kind == "chrome":
        return False
    if not to_state_id or to_state_id == from_state_id:
        return False
    if interaction.get("same_page_transition") or interaction.get("state_changed"):
        return True
    if outcome in {"modal_open", "dom_mutation"}:
        return True
    if interaction.get("value_changed") or interaction.get("submitted"):
        return True
    if interaction.get("validation_errors"):
        return True
    if int(interaction.get("surface_delta", 0) or 0) > 0:
        return True
    return False


def build_state_graph_evidence(pages: list[dict[str, Any]]) -> dict[str, Any]:
    states: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    actions: dict[tuple[str, str, str, str, str, str, str, str], dict[str, Any]] = {}
    route_links: dict[tuple[str, str], dict[str, Any]] = {}
    routes: dict[str, set[str]] = defaultdict(set)
    roots: list[dict[str, Any]] = []
    form_touches = 0

    def ensure_state(
        state_id: str,
        route: str,
        label: str,
        kind: str,
        depth: int,
        url: str,
    ) -> None:
        if not state_id:
            return
        node = states.setdefault(state_id, {
            "state_id": state_id,
            "route": route,
            "label": label or "root",
            "kind": kind or "page",
            "depth": int(depth or 0),
            "url": url or "",
            "visits": 0,
            "incoming": 0,
            "outgoing": 0,
        })
        node["visits"] += 1
        if label and len(label) > len(str(node.get("label", ""))):
            node["label"] = label
        if url:
            node["url"] = url
        node["depth"] = max(int(node.get("depth", 0) or 0), int(depth or 0))
        if kind and node.get("kind") == "page":
            node["kind"] = kind

    for page in pages:
        route = canonicalize_path_from_url(page.get("url", ""))
        root = page.get("root_state") or {}
        root_id = str(root.get("state_id", "") or "")
        if root_id:
            ensure_state(
                root_id,
                route,
                str(root.get("label", "") or "root"),
                str(root.get("kind", "") or "page"),
                int(root.get("depth", 0) or 0),
                str(page.get("url", "") or ""),
            )
            routes[route].add(root_id)
            roots.append({
                "route": route,
                "root_state": {
                    "state_id": root_id,
                    "label": str(root.get("label", "") or "root"),
                    "kind": str(root.get("kind", "") or "page"),
                    "depth": int(root.get("depth", 0) or 0),
                },
                "url": str(page.get("url", "") or ""),
                "score": float((page.get("analysis") or {}).get("health_score", 5) or 5),
            })

        for link in page.get("discovered_links", []) or []:
            to_route = canonicalize_path_from_url(link)
            if not to_route or to_route == route:
                continue
            pair = (route, to_route)
            row = route_links.setdefault(pair, {
                "from_route": route,
                "to_route": to_route,
                "count": 0,
            })
            row["count"] += 1

        for ir in page.get("interaction_results", []) or []:
            action_kind = str(ir.get("action_kind", "") or "")
            form_intent = str(ir.get("form_intent", "") or "")
            if (
                action_kind in {"input", "select", "date", "checkbox", "radio"}
                or form_intent
                or ir.get("form_context")
                or ir.get("submitted")
                or ir.get("validation_errors")
            ):
                form_touches += 1

            from_state_id = str(ir.get("from_state_id", "") or root_id or "")
            to_state_id = str(ir.get("to_state_id", "") or from_state_id or "")
            from_route = str(ir.get("from_route", "") or route)
            to_route = str(ir.get("to_route", "") or from_route)
            ensure_state(
                from_state_id,
                from_route,
                str(ir.get("from_state_label", "") or "root"),
                str(ir.get("from_state_kind", "") or "page"),
                int(ir.get("state_depth", 0) or 0),
                str(ir.get("from_state", "") or page.get("url", "") or ""),
            )
            ensure_state(
                to_state_id,
                to_route,
                str(ir.get("to_state_label", "") or "root"),
                str(ir.get("to_state_kind", "") or "page"),
                int(ir.get("next_state_depth", 0) or 0),
                str(ir.get("to_state", "") or page.get("url", "") or ""),
            )
            routes[from_route].add(from_state_id)
            routes[to_route].add(to_state_id)

            action_key = (
                from_route,
                to_route,
                from_state_id,
                to_state_id,
                str(ir.get("action", "") or "?"),
                str(ir.get("outcome", "") or "unknown"),
                action_kind,
                str(ir.get("scope_kind", "") or "page"),
            )
            action = actions.setdefault(action_key, {
                "interaction_id": str(ir.get("interaction_id", "") or hash_text("|".join(action_key), 14)),
                "route": from_route,
                "from_route": from_route,
                "to_route": to_route,
                "from_state_id": from_state_id,
                "to_state_id": to_state_id,
                "from_state_label": str(ir.get("from_state_label", "") or "root"),
                "to_state_label": str(ir.get("to_state_label", "") or "root"),
                "from_state_kind": str(ir.get("from_state_kind", "") or "page"),
                "to_state_kind": str(ir.get("to_state_kind", "") or "page"),
                "state_depth": int(ir.get("state_depth", 0) or 0),
                "next_state_depth": int(ir.get("next_state_depth", 0) or 0),
                "action": str(ir.get("action", "") or "?"),
                "action_kind": action_kind or "click",
                "outcome": str(ir.get("outcome", "") or "unknown"),
                "scope_kind": str(ir.get("scope_kind", "") or "page"),
                "scope_label": str(ir.get("scope_label", "") or ""),
                "section": str(ir.get("section", "") or ""),
                "chrome_context": str(ir.get("chrome_context", "") or ""),
                "form_context": str(ir.get("form_context", "") or ""),
                "form_intent": form_intent,
                "is_new": bool(ir.get("is_new")),
                "same_page_transition": bool(ir.get("same_page_transition")),
                "state_changed": bool(ir.get("state_changed")),
                "value_changed": bool(ir.get("value_changed")),
                "submitted": bool(ir.get("submitted")),
                "submit_action": str(ir.get("submit_action", "") or ""),
                "submit_kind": str(ir.get("submit_kind", "") or ""),
                "validation_errors": list(ir.get("validation_errors", []) or [])[:5],
                "surface_delta": int(ir.get("surface_delta", 0) or 0),
                "creates_transition": False,
                "count": 0,
            })
            action["count"] += 1
            action["state_depth"] = max(int(action.get("state_depth", 0) or 0), int(ir.get("state_depth", 0) or 0))
            action["next_state_depth"] = max(int(action.get("next_state_depth", 0) or 0), int(ir.get("next_state_depth", 0) or 0))
            action["surface_delta"] = max(int(action.get("surface_delta", 0) or 0), int(ir.get("surface_delta", 0) or 0))
            if ir.get("submitted"):
                action["submitted"] = True
            if ir.get("submit_action") and not action.get("submit_action"):
                action["submit_action"] = str(ir.get("submit_action", "") or "")
            if ir.get("validation_errors") and not action.get("validation_errors"):
                action["validation_errors"] = list(ir.get("validation_errors", []) or [])[:5]

            if not interaction_creates_transition(ir):
                continue

            action["creates_transition"] = True
            edge_key = (
                from_state_id,
                to_state_id,
                str(ir.get("action", "") or "?"),
                str(ir.get("outcome", "") or "unknown"),
                action_kind or "click",
            )
            edge = edges.setdefault(edge_key, {
                "from_state_id": from_state_id,
                "to_state_id": to_state_id,
                "from_route": from_route,
                "to_route": to_route,
                "from_label": str(ir.get("from_state_label", "") or "root"),
                "to_label": str(ir.get("to_state_label", "") or "root"),
                "from_kind": str(ir.get("from_state_kind", "") or "page"),
                "to_kind": str(ir.get("to_state_kind", "") or "page"),
                "action": str(ir.get("action", "") or "?"),
                "action_kind": action_kind or "click",
                "outcome": str(ir.get("outcome", "") or "unknown"),
                "count": 0,
                "api_failures": 0,
                "broken": 0,
                "discoveries": 0,
                "surface_delta": 0,
                "submitted": 0,
                "validation": 0,
                "max_depth": 0,
            })
            edge["count"] += 1
            edge["api_failures"] += int(ir.get("api_failures", 0) or 0)
            edge["broken"] += 1 if ir.get("broke") or ir.get("outcome") in {"broken", "timeout"} else 0
            edge["discoveries"] += len(ir.get("discovered_urls", []) or [])
            edge["surface_delta"] += int(ir.get("surface_delta", 0) or 0)
            edge["submitted"] += 1 if ir.get("submitted") else 0
            edge["validation"] += len(ir.get("validation_errors", []) or [])
            edge["max_depth"] = max(
                int(edge.get("max_depth", 0) or 0),
                int(ir.get("state_depth", 0) or 0),
                int(ir.get("next_state_depth", 0) or 0),
            )

    for edge in edges.values():
        if edge["from_state_id"] in states:
            states[edge["from_state_id"]]["outgoing"] += edge["count"]
        if edge["to_state_id"] in states:
            states[edge["to_state_id"]]["incoming"] += edge["count"]

    state_rows = sorted(
        states.values(),
        key=lambda item: (-(item["incoming"] + item["outgoing"]), -item["visits"], item["route"], item["label"]),
    )
    edge_rows = sorted(
        edges.values(),
        key=lambda item: (
            -(item["broken"] * 4 + item["api_failures"] * 2 + item["validation"] + item["count"]),
            item["from_route"],
            item["action"],
        ),
    )
    action_rows = sorted(
        actions.values(),
        key=lambda item: (
            -int(bool(item.get("creates_transition"))),
            -int(bool(item.get("submitted"))),
            -len(item.get("validation_errors", []) or []),
            item["route"],
            item["action"],
        ),
    )
    route_link_rows = sorted(route_links.values(), key=lambda item: (-item["count"], item["from_route"], item["to_route"]))
    max_depth = max((int(node.get("depth", 0) or 0) for node in state_rows), default=0)
    return {
        "summary": {
            "routes": len(routes),
            "states": len(states),
            "transitions": len(edge_rows),
            "actions": len(action_rows),
            "deepest_state": max_depth,
            "modal_states": sum(1 for node in state_rows if node.get("kind") == "modal"),
            "form_touches": form_touches,
        },
        "states": state_rows,
        "edges": edge_rows,
        "actions": action_rows,
        "roots": roots,
        "route_links": route_link_rows,
    }
