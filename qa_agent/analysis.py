"""
analysis.py — LLM-based page analysis with optional screenshot vision.

If the model supports vision (e.g. qwen2.5vl, llava, bakllava) AND
screenshot_b64 is present in the summary, we pass the image alongside
the text context so the model can spot visual bugs, broken layouts,
missing images, and UX issues a text-only analysis would miss.
"""
from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .heuristics import FAIL_OUTCOMES, classify_route, summarize_interactions
from .llm import call_chat, extract_json_array, extract_json_object, llm_log
from .utils import coerce_health_score


# ── JSON repair helpers ────────────────────────────────────────────────────

# ── Debug logging ──────────────────────────────────────────────────────────

# ── LLM call ──────────────────────────────────────────────────────────────

def _call(settings: Settings, messages: list[dict[str, Any]]) -> str:
    return call_chat(
        settings,
        messages,
        purpose="analysis",
        json_mode=True,
        temperature=0,
        max_tokens=2048,
    )


def _build_messages(system: str, prompt: str, screenshot_b64: str = "") -> list[dict[str, Any]]:
    """
    Build message list. If a screenshot is provided, attach it as a vision
    content block so models that support it (qwen2.5vl, llava, etc.) use it.
    """
    if screenshot_b64:
        user_content: Any = [
            {"type": "text",      "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
        ]
    else:
        user_content = prompt
    return [
        {"role": "system",  "content": system},
        {"role": "user",    "content": user_content},
    ]


# ── Normalisation ──────────────────────────────────────────────────────────

def _as_str_list(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if x and str(x).strip()]


def _dedupe(items: list[str], limit: int = 8) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _round_subscores(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for key, raw in value.items():
        try:
            out[str(key)] = round(float(raw), 2)
        except Exception:
            continue
    return out


def normalize_analysis(raw: dict[str, Any] | None) -> dict[str, Any]:
    d = raw if isinstance(raw, dict) else {}
    raw_score = d.get("health_score", 5)
    score = round(max(0.0, min(10.0, coerce_health_score(raw_score, default=5.0))), 1)

    health_label = d.get("health_label", "")
    if not health_label and isinstance(raw_score, str):
        lbl = raw_score.strip().lower().replace(" ", "_")
        if lbl and not lbl.replace(".", "", 1).isdigit():
            health_label = lbl

    return {
        "health_score":        score,
        "health_label":        health_label or "",
        "raw_health_score":    str(raw_score),
        "analysis_source":     str(d.get("analysis_source", "llm") or "llm"),
        "llm_error":           str(d.get("llm_error", "") or ""),
        "route_kind":          str(d.get("route_kind", "") or ""),
        "business_area":       str(d.get("business_area", "") or ""),
        "subscores":           _round_subscores(d.get("subscores", {})),
        "heuristic_flags":     _as_str_list(d.get("heuristic_flags", [])),
        "recommendations":     _as_str_list(d.get("recommendations", [])),
        "coverage_findings":   _as_str_list(d.get("coverage_findings", [])),
        "bugs":                _as_str_list(d.get("bugs",                [])),
        "broken_links":        _as_str_list(d.get("broken_links",        [])),
        "performance_issues":  _as_str_list(d.get("performance_issues",  [])),
        "ux_issues":           _as_str_list(d.get("ux_issues",           [])),
        "api_issues":          _as_str_list(d.get("api_issues",          [])),
        "visual_issues":       _as_str_list(d.get("visual_issues",       [])),  # new: from vision
        "broken_interactions": _as_str_list(d.get("broken_interactions", [])),  # new: from click analysis
        "interaction_results": _as_str_list(d.get("interaction_results", [])),
        "summary":             str(d.get("summary", "") or ""),
    }


# ── Prompts ────────────────────────────────────────────────────────────────

SYSTEM = "Return JSON only. No markdown. No trailing commas. No prose outside JSON."

def _batch_prompt(summaries: list[dict[str, Any]]) -> str:
    # Strip b64 screenshots from batch prompt — too large; vision used in single calls only
    clean = [{k: v for k, v in s.items() if k != "screenshot_b64"} for s in summaries]
    return f"""You are a senior QA engineer auditing a web app.
Analyze all pages below and return ONLY a valid JSON array — one object per page.
health_score MUST be a NUMBER 0-10. Never use strings like "good" or "8/10".
Required keys per object:
  health_score, bugs, broken_links, performance_issues, ux_issues,
  api_issues, visual_issues, broken_interactions, interaction_results, summary

Focus on: HTTP errors, JS errors, slow LCP/CLS, failing API calls, broken clicks.

Pages:
{json.dumps(clean, indent=2)}
"""


def _single_prompt(summary: dict[str, Any], has_vision: bool) -> str:
    clean = {k: v for k, v in summary.items() if k != "screenshot_b64"}
    vision_note = (
        "\nA screenshot of this page is attached — use it to identify visual bugs, "
        "broken layouts, missing images, placeholder text left in UI, and UX issues "
        "that are not visible in the raw metrics.\n"
        if has_vision else ""
    )
    return f"""Analyze this page and return ONLY one JSON object.{vision_note}
Required keys:
  health_score (NUMBER 0-10), bugs, broken_links, performance_issues,
  ux_issues, api_issues, visual_issues, broken_interactions, interaction_results, summary

broken_interactions: list any elements that caused JS errors or crashes when clicked.
visual_issues: list layout/design problems visible in the screenshot.

Page data:
{json.dumps(clean, indent=2)}
"""


# ── Public API ─────────────────────────────────────────────────────────────

async def analyze_batch(
    settings:  Settings,
    summaries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Try batch analysis first (fast, no vision).
    Falls back to per-page single calls (with vision if screenshot available).
    """
    if settings.llm_preflight_ok is False:
        return [fallback_analysis(summary, reason=settings.llm_last_error) for summary in summaries]

    try:
        prompt   = _batch_prompt(summaries)
        messages = _build_messages(SYSTEM, prompt)  # no vision in batch
        raw = _call(settings, messages)
        arr = extract_json_array(raw)
        if arr is not None and len(arr) == len(summaries):
            return [normalize_analysis(a if isinstance(a, dict) else {}) for a in arr]
        llm_log(
            settings,
            f"[LLM][WARN] Batch mismatch: expected {len(summaries)} got {len(arr) if arr else 'None'}",
            raw=raw,
            force=True,
        )
    except Exception as e:
        llm_log(settings, f"[LLM][ERROR] Batch failed: {type(e).__name__}: {e}", force=True)

    # Fall back to single calls (enables vision per page)
    results: list[dict[str, Any]] = []
    for s in summaries:
        results.append(await analyze_single(settings, s))
    return results


async def analyze_single(
    settings: Settings,
    summary:  dict[str, Any],
) -> dict[str, Any]:
    """
    Single-page analysis. Attaches screenshot for vision-capable models.
    """
    if settings.llm_preflight_ok is False:
        return fallback_analysis(summary, reason=settings.llm_last_error)

    b64       = summary.get("screenshot_b64", "")
    has_vision = bool(b64)
    prompt    = _single_prompt(summary, has_vision)
    messages  = _build_messages(SYSTEM, prompt, b64 if has_vision else "")
    try:
        raw = _call(settings, messages)
        obj = extract_json_object(raw)
        if obj is None:
            raise ValueError("No JSON object in response")
        return normalize_analysis(obj)
    except Exception as e:
        llm_log(
            settings,
            f"[LLM][ERROR] Single failed for {summary.get('url','')}: {type(e).__name__}: {e}",
            force=True,
        )
        return fallback_analysis(summary, reason=str(e))


def fallback_analysis(summary: dict[str, Any], reason: str = "") -> dict[str, Any]:
    """
    Heuristic analysis used when LLM is unavailable or times out.
    Also surfaces broken interactions from the explorer's click log.
    """
    perf = summary.get("perf_metrics", {})
    failed_apis = summary.get("failed_apis", [])
    js_errors = summary.get("js_errors", [])
    net_failures = summary.get("network_failures", [])
    http_errors = summary.get("http_errors", [])
    mutation = summary.get("dom_mutations", {})
    interactions = summary.get("interaction_results", [])
    load_time_ms = int(summary.get("load_time_ms", 0) or 0)
    http_status = int(summary.get("http_status", 0) or 0)
    links_found = int(summary.get("links_found", 0) or 0)
    states_seen = int(summary.get("states_seen", 0) or 0)
    state_transitions = int(summary.get("state_transitions", 0) or 0)

    interaction_stats = summarize_interactions(interactions)
    route_meta = classify_route(
        str(summary.get("route") or summary.get("url") or "/"),
        title=str(summary.get("title", "") or ""),
        interactions=interactions,
        links_found=links_found,
        states_seen=states_seen,
    )
    expected = route_meta["expected"]

    bugs: list[str] = []
    perf_issues: list[str] = []
    api_issues: list[str] = []
    ux_issues: list[str] = []
    broken_int: list[str] = []
    coverage_findings: list[str] = []
    recommendations: list[str] = []
    heuristic_flags: list[str] = [f"route:{route_meta['page_kind']}"]
    broken_links = [
        f"{item.get('status', '?')} {item.get('url', '')}".strip()
        for item in http_errors
        if int(item.get("status", 0) or 0) >= 400
    ]

    availability = 10.0
    runtime = 10.0
    api_health = 10.0
    performance = 10.0
    interaction = 10.0
    discovery = 10.0

    if http_status >= 500:
        availability -= 7.5
        bugs.append(f"HTTP {http_status} response blocks page availability")
        recommendations.append("Fix the server-side failure before deeper UI issues are triaged.")
    elif http_status >= 400:
        availability -= 5.5
        bugs.append(f"HTTP {http_status} response returned for the page")
        recommendations.append("Resolve the page-level request failure or redirect loop.")
    if net_failures:
        availability -= min(3.0, len(net_failures) * 0.8)
        bugs.append(f"{len(net_failures)} network request(s) failed while loading the page")
    if load_time_ms > 12000:
        availability -= 1.6
        performance -= 2.3
        perf_issues.append(f"Page took {load_time_ms}ms to settle, which is beyond acceptable QA latency.")
    elif load_time_ms > 6000:
        availability -= 0.6
        performance -= 1.2
        perf_issues.append(f"Page took {load_time_ms}ms to settle.")

    if js_errors:
        runtime -= min(4.8, len(js_errors) * 0.75)
        bugs.append(f"{len(js_errors)} console/page error(s) were raised")
        recommendations.append("Reproduce the first console error locally and fix the throwing component or data contract.")
    if interaction_stats["fail"]:
        runtime -= min(2.5, interaction_stats["fail"] * 0.45)
    if interaction_stats["timeouts"]:
        runtime -= min(1.5, interaction_stats["timeouts"] * 0.35)

    if failed_apis:
        api_health -= min(5.0, len(failed_apis) * 0.85)
        first_api = str(failed_apis[0].get("url", "") or failed_apis[0].get("endpoint", "") or "")[:100]
        api_issues.append(f"{len(failed_apis)} failing API call(s){f' - first: {first_api}' if first_api else ''}")
        recommendations.append("Inspect the failing API request and verify auth, payload shape, and error handling.")
    if interaction_stats["api_failures"]:
        api_health -= min(2.5, interaction_stats["api_failures"] * 0.25)

    lcp = float(perf.get("lcp", 0) or 0)
    cls = float(perf.get("cls", 0) or 0)
    fcp = float(perf.get("fcp", 0) or 0)
    if lcp > 4000:
        performance -= 2.4
        perf_issues.append(f"LCP {int(lcp)}ms is slow.")
    elif lcp > 2500:
        performance -= 1.2
        perf_issues.append(f"LCP {int(lcp)}ms needs improvement.")
    if fcp > 2500:
        performance -= 0.9
        perf_issues.append(f"FCP {int(fcp)}ms indicates a slow first render.")
    if cls > 0.25:
        performance -= 1.4
        ux_issues.append(f"CLS {cls:.3f} indicates severe layout shift.")
    elif cls > 0.1:
        performance -= 0.8
        ux_issues.append(f"CLS {cls:.3f} is above the stable threshold.")
    if float(mutation.get("max_per_second", 0) or 0) > 120:
        performance -= 1.1
        perf_issues.append(f"Heavy re-renders were observed ({mutation['max_per_second']}/s).")

    if interaction_stats["attempts"] == 0:
        interaction -= 3.5
        coverage_findings.append("No interactive elements were exercised on this page.")
    else:
        interaction -= interaction_stats["failure_rate"] * 4.8
        interaction -= interaction_stats["no_change_rate"] * 2.0
        if interaction_stats["success"] == 0 and interaction_stats["discovery_actions"] == 0:
            interaction -= 2.2
            coverage_findings.append("Actions were attempted, but none produced navigation or meaningful same-page change.")
        if route_meta["page_kind"] in {"settings", "form", "editor"} and interaction_stats["form_actions"] == 0:
            interaction -= 1.3
            coverage_findings.append("This route looks form-heavy, but the crawler did not exercise any form fields.")
        elif (
            route_meta["page_kind"] in {"settings", "form", "editor"}
            and interaction_stats["form_actions"] > 0
            and interaction_stats.get("form_submissions", 0) == 0
            and interaction_stats.get("validation_feedback", 0) == 0
        ):
            interaction -= 0.7
            coverage_findings.append("Form fields were touched, but no validation feedback or safe form submission was observed.")
        if interaction_stats["chrome_actions"] and interaction_stats["chrome_actions"] >= max(2, interaction_stats["attempts"] // 2):
            interaction -= 0.8
            heuristic_flags.append("chrome-heavy")
            coverage_findings.append("Most actions happened in shared navigation chrome instead of page body content.")

    link_ratio = min(1.0, links_found / max(expected["links"], 1))
    state_ratio = min(1.0, states_seen / max(expected["states"], 1))
    discovery = round(
        4.0
        + link_ratio * 2.2
        + state_ratio * 2.0
        + min(interaction_stats["discovery_actions"] / max(expected["states"], 1), 1.0) * 1.8,
        2,
    )
    if route_meta["page_kind"] in {"dashboard", "list", "help"} and links_found < expected["links"]:
        discovery -= 1.4
        coverage_findings.append(f"{route_meta['page_kind'].title()} route exposed only {links_found} links; expected at least {expected['links']}.")
    if route_meta["page_kind"] in {"detail", "form", "editor", "settings"} and states_seen < expected["states"]:
        discovery -= 1.2
        coverage_findings.append(
            f"{route_meta['page_kind'].title()} route stayed shallow ({states_seen} states, {state_transitions} transitions)."
        )
    if interaction_stats["same_page_transitions"] == 0 and route_meta["page_kind"] in {"detail", "settings", "editor"}:
        discovery -= 0.9
    discovery = max(0.0, min(10.0, discovery))

    for row in interactions:
        outcome = str(row.get("outcome", "") or "")
        if not (row.get("broke") or outcome in FAIL_OUTCOMES):
            continue
        evidence = list(row.get("js_errors", [])) + list(row.get("net_failures", []))
        detail = evidence[0][:100] if evidence else outcome or "interaction failure"
        broken_int.append(f"{row.get('action', '?')} -> {detail}")
    broken_int = _dedupe(broken_int, 8)

    if route_meta["page_kind"] in {"dashboard", "list"} and links_found < expected["links"]:
        recommendations.append("Inspect the page shell and lazy-loaded sections to ensure list data and links render for authenticated users.")
    if route_meta["page_kind"] in {"detail", "settings", "editor"} and state_transitions < max(1, expected["states"] - 1):
        recommendations.append("Add or fix section-level interactions so deeper panels, drawers, or forms become reachable.")
    if interaction_stats["failure_rate"] > 0.2:
        recommendations.append("Prioritize unstable actions first; several interactions are failing or timing out.")
    if interaction_stats["no_change_rate"] > 0.7 and interaction_stats["attempts"] >= 4:
        recommendations.append("Reduce dead-click UI or ensure actions reveal state, data, or validation feedback.")

    subscores = {
        "availability": max(0.0, min(10.0, round(availability, 2))),
        "runtime": max(0.0, min(10.0, round(runtime, 2))),
        "api": max(0.0, min(10.0, round(api_health, 2))),
        "performance": max(0.0, min(10.0, round(performance, 2))),
        "interaction": max(0.0, min(10.0, round(interaction, 2))),
        "discovery": max(0.0, min(10.0, round(discovery, 2))),
    }
    weighted_score = (
        subscores["availability"] * 0.22
        + subscores["runtime"] * 0.18
        + subscores["api"] * 0.16
        + subscores["performance"] * 0.16
        + subscores["interaction"] * 0.16
        + subscores["discovery"] * 0.12
    )
    score = max(0.0, min(10.0, round(weighted_score, 1)))

    reason_text = " ".join(str(reason or "").split())[:140]
    summary_parts = []
    if score < 4:
        summary_parts.append(f"{route_meta['page_kind'].title()} route is in poor shape")
    elif score < 7:
        summary_parts.append(f"{route_meta['page_kind'].title()} route needs follow-up")
    else:
        summary_parts.append(f"{route_meta['page_kind'].title()} route looks stable")
    if bugs:
        summary_parts.append(f"{len(bugs)} runtime/availability issue(s)")
    if api_issues:
        summary_parts.append(api_issues[0])
    if coverage_findings:
        summary_parts.append(coverage_findings[0])
    fallback_summary = ". ".join(_dedupe(summary_parts, 3)) + "."
    if reason_text:
        fallback_summary = f"{fallback_summary} Heuristic mode reason: {reason_text}"

    heuristic_flags.extend([
        f"hub:{route_meta['hub_score']}",
        f"links:{links_found}",
        f"states:{states_seen}",
        f"actions:{interaction_stats['attempts']}",
    ])

    return normalize_analysis({
        "health_score":        score,
        "analysis_source":     "fallback",
        "llm_error":           reason_text,
        "route_kind":          route_meta["page_kind"],
        "business_area":       route_meta["business_area"],
        "subscores":           subscores,
        "heuristic_flags":     _dedupe(heuristic_flags, 10),
        "recommendations":     _dedupe(recommendations, 6),
        "coverage_findings":   _dedupe(coverage_findings, 6),
        "bugs":                _dedupe(bugs, 8),
        "broken_links":        _dedupe(broken_links, 6),
        "performance_issues":  _dedupe(perf_issues, 8),
        "ux_issues":           _dedupe(ux_issues, 8),
        "api_issues":          _dedupe(api_issues, 8),
        "visual_issues":       [],
        "broken_interactions": broken_int,
        "interaction_results": [],
        "summary":             fallback_summary,
    })
