"""
reporting.py — QA report generation, history tracking, delta analytics.

Produces:
  report.html  — rich standalone dev report:
    1. KPI banner (score, bugs, broken interactions, API failures, slow pages)
    2. Executive summary paragraph (plain-English what to do)
    3. Dev Focus Areas — prioritised accordion, each with:
         · What to fix  · Effort estimate  · Issue details  · Broken interactions
    4. Route Tree   — GitHub-branch style, score dot per segment
    5. Pages table  — screenshot thumbnail, score, HTTP, load, bugs, broken, summary
    6. API table    — endpoint, method, calls, failure rate, avg latency
    7. Interaction reliability — action, route, attempts, reliability %, flakiness %
    8. Workflow scenarios — pass/fail
  history.json — run history for trend/delta tracking
"""
from __future__ import annotations

import html
import json
import os
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from .config import Settings
from .graph_evidence import build_state_graph_evidence
from .heuristics import FORM_ACTION_KINDS, build_route_snapshot
from .llm import call_chat, extract_json_object, llm_log
from .utils import canonicalize_path_from_url, coerce_health_score


# ═══════════════════════════════════════════════════════════════════════════
#  HISTORY
# ═══════════════════════════════════════════════════════════════════════════

def _load_history(settings: Settings) -> list[dict[str, Any]]:
    if not os.path.exists(settings.history_file):
        return []
    try:
        with open(settings.history_file, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _run_summary(pages: list[dict[str, Any]], api_summary: list[dict[str, Any]]) -> dict[str, Any]:
    scores    = [coerce_health_score(p.get("analysis", {}).get("health_score", 5), 5.0) for p in pages]
    all_bugs: list[str] = []
    phase_counts: dict[str, int] = defaultdict(int)
    for p in pages:
        all_bugs.extend((p.get("analysis") or {}).get("bugs", []))
        phase = str(p.get("crawl_phase") or "authenticated")
        phase_counts[phase] += 1
    broken  = sum(p.get("broken_interactions", 0) for p in pages)
    slow    = sum(1 for p in pages if p.get("load_time_ms", 0) > 3000)
    api_fail = sum(c.get("failures", 0) for c in api_summary)
    total_api = max(sum(c.get("calls", 1) for c in api_summary), 1)
    avg_lat   = sum(c.get("avg_latency_ms", 0) * c.get("calls", 1) for c in api_summary) / total_api
    return {
        "timestamp":           time.time(),
        "date":                datetime.utcnow().isoformat(timespec="seconds"),
        "page_count":          len(pages),
        "avg_score":           round(sum(scores) / max(len(scores), 1), 2),
        "bug_count":           len(all_bugs),
        "broken_interactions": broken,
        "slow_count":          slow,
        "api_failure_count":   api_fail,
        "avg_api_latency_ms":  round(avg_lat),
        "routes":              list({canonicalize_path_from_url(p.get("url", "")) for p in pages}),
        "route_metrics":       build_route_snapshot(pages),
        "phase_counts":        dict(phase_counts),
    }


def save_history(settings: Settings, pages: list[dict[str, Any]], api_summary: list[dict[str, Any]]) -> None:
    history = _load_history(settings)
    history.append(_run_summary(pages, api_summary))
    with open(settings.history_file, "w", encoding="utf-8") as f:
        json.dump(history[-50:], f, indent=2)


def compute_deltas(history: list[dict[str, Any]]) -> dict[str, Any]:
    if len(history) < 2:
        return {}
    prev, curr = history[-2], history[-1]
    pr, cr = set(prev.get("routes", [])), set(curr.get("routes", []))
    regressions: list[dict[str, Any]] = []
    prev_routes = prev.get("route_metrics", {}) or {}
    curr_routes = curr.get("route_metrics", {}) or {}
    for route, current in curr_routes.items():
        old = prev_routes.get(route)
        if not isinstance(old, dict):
            continue
        score_delta = round(float(current.get("avg_score", 0)) - float(old.get("avg_score", 0)), 2)
        broken_delta = int(current.get("broken", 0) or 0) - int(old.get("broken", 0) or 0)
        api_delta = int(current.get("api_failures", 0) or 0) - int(old.get("api_failures", 0) or 0)
        discovery_delta = int(current.get("links_found", 0) or 0) - int(old.get("links_found", 0) or 0)
        states_delta = int(current.get("states_seen", 0) or 0) - int(old.get("states_seen", 0) or 0)
        actions_delta = int(current.get("actions_seen", 0) or 0) - int(old.get("actions_seen", 0) or 0)
        depth_delta = int(current.get("max_state_depth", 0) or 0) - int(old.get("max_state_depth", 0) or 0)
        severity = (
            max(0.0, -score_delta) * 2.0
            + max(0, broken_delta) * 1.5
            + max(0, api_delta) * 1.2
            + max(0.0, -discovery_delta) * 0.35
            + max(0.0, -states_delta) * 0.45
            + max(0.0, -actions_delta) * 0.08
            + max(0.0, -depth_delta) * 0.9
        )
        if severity <= 0:
            continue
        regressions.append({
            "route": route,
            "score_delta": score_delta,
            "broken_delta": broken_delta,
            "api_delta": api_delta,
            "discovery_delta": discovery_delta,
            "states_delta": states_delta,
            "actions_delta": actions_delta,
            "depth_delta": depth_delta,
            "severity": round(severity, 2),
            "route_kind": str(current.get("route_kind", "") or ""),
        })
    regressions.sort(key=lambda row: (-row["severity"], row["route"]))
    return {
        "score_delta":          round(curr.get("avg_score", 0)          - prev.get("avg_score", 0), 2),
        "bug_delta":            curr.get("bug_count", 0)                - prev.get("bug_count", 0),
        "broken_delta":         curr.get("broken_interactions", 0)      - prev.get("broken_interactions", 0),
        "page_delta":           curr.get("page_count", 0)               - prev.get("page_count", 0),
        "api_latency_delta_ms": curr.get("avg_api_latency_ms", 0)       - prev.get("avg_api_latency_ms", 0),
        "new_routes":           list(cr - pr),
        "removed_routes":       list(pr - cr),
        "route_regressions":    regressions[:8],
    }


# ═══════════════════════════════════════════════════════════════════════════
#  SLACK
# ═══════════════════════════════════════════════════════════════════════════

def send_slack_alert(settings: Settings, pages: list[dict[str, Any]]) -> None:
    if not settings.slack_webhook:
        return
    import urllib.request
    critical = [p for p in pages
                if coerce_health_score(p.get("analysis", {}).get("health_score", 5), 5.0) < 4.0]
    if not critical:
        return
    text = f"*Open Web QA Alert* — {len(critical)} critical page(s)\n"
    for p in critical[:5]:
        text += f"• `{p.get('url','')}` — score {p.get('analysis',{}).get('health_score','?')}\n"
    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(settings.slack_webhook, data=payload,
                                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10): pass
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
#  ANALYSIS HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _sc(score: float) -> str:
    """Score → hex colour."""
    return "#22d37a" if score >= 7 else "#f5c842" if score >= 4 else "#ff4a6e"

def _label(score: float) -> str:
    if score >= 8: return "Excellent"
    if score >= 7: return "Good"
    if score >= 5: return "Fair"
    if score >= 3: return "Poor"
    return "Critical"

def _effort(n: int) -> str:
    if n == 0:   return "—"
    if n <= 2:   return "~1 hour"
    if n <= 5:   return "~half day"
    if n <= 10:  return "~1 day"
    return "~1+ days"


# ═══════════════════════════════════════════════════════════════════════════
#  FOCUS AREAS  (the core "what to fix" engine)
# ═══════════════════════════════════════════════════════════════════════════

def _focus_areas(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Group pages by canonical route, score each, surface issues,
    produce actionable recommendations, sort worst-first.
    """
    by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in pages:
        by_route[canonicalize_path_from_url(p.get("url", ""))].append(p)

    areas = []
    for route, rp in by_route.items():
        bugs, perf, ux, api_iss, visual, broken_int = [], [], [], [], [], []
        recs_pool, coverage_pool, route_kinds = [], [], []
        scores, api_fail_n, broken_n = [], 0, 0
        subscore_totals = defaultdict(float)
        subscore_counts = defaultdict(int)

        for p in rp:
            a = p.get("analysis") or {}
            scores.append(coerce_health_score(a.get("health_score", 5), 5.0))
            bugs.extend(a.get("bugs", []))
            perf.extend(a.get("performance_issues", []))
            ux.extend(a.get("ux_issues", []))
            api_iss.extend(a.get("api_issues", []))
            visual.extend(a.get("visual_issues", []))
            broken_int.extend(a.get("broken_interactions", []))
            recs_pool.extend(a.get("recommendations", []))
            coverage_pool.extend(a.get("coverage_findings", []))
            if a.get("route_kind"):
                route_kinds.append(str(a.get("route_kind")))
            for key, value in (a.get("subscores") or {}).items():
                try:
                    subscore_totals[str(key)] += float(value)
                    subscore_counts[str(key)] += 1
                except Exception:
                    continue
            api_fail_n += p.get("api_failures", 0)
            broken_n   += p.get("broken_interactions", 0)
            # Capture broken interactions from click log
            for ir in p.get("interaction_results", []):
                if ir.get("broke") or ir.get("outcome") == "broken":
                    errs = ir.get("js_errors", [ir.get("outcome", "error")])
                    broken_int.append(f"'{ir.get('action','?')}' → {errs[0][:80] if errs else 'crash'}")

        avg  = round(sum(scores) / max(len(scores), 1), 2)
        n    = len(bugs) + broken_n + api_fail_n + len(perf)

        # Build actionable recommendations
        recs: list[str] = []
        if broken_n:
            recs.append(f"Fix {broken_n} interactive element(s) that crash or throw JS errors on click")
        if bugs:
            recs.append(f"Resolve {len(bugs)} JS/runtime bug(s) — worst: {bugs[0][:90]}")
        if api_fail_n:
            recs.append(f"Investigate {api_fail_n} failing API request(s) causing blank/broken data")
        if perf:
            recs.append(f"Performance: {perf[0][:90]}")
        if ux:
            recs.append(f"UX issue: {ux[0][:90]}")
        if visual:
            recs.append(f"Visual bug: {visual[0][:90]}")
        recs.extend(recs_pool[:4])
        if not recs:
            recs.append("No actionable issues detected — route looks clean ✓")

        avg_subscores = {
            key: round(subscore_totals[key] / max(subscore_counts[key], 1), 2)
            for key in subscore_totals
        }

        areas.append({
            "route":        route,
            "route_kind":   route_kinds[0] if route_kinds else "",
            "avg_score":    avg,
            "priority":     avg - broken_n * 2 - api_fail_n * 1.5 - len(bugs) * 0.4,
            "total_issues": n,
            "bug_count":    len(bugs),
            "broken_count": broken_n,
            "api_fail":     api_fail_n,
            "perf_count":   len(perf),
            "visual_count": len(visual),
            "effort":       _effort(n),
            "recs":         list(dict.fromkeys(recs))[:5],
            "bugs":         list(dict.fromkeys(bugs))[:5],
            "perf":         list(dict.fromkeys(perf))[:3],
            "ux":           list(dict.fromkeys(ux))[:3],
            "api":          list(dict.fromkeys(api_iss))[:3],
            "visual":       list(dict.fromkeys(visual))[:3],
            "broken_int":   list(dict.fromkeys(broken_int))[:6],
            "coverage":     list(dict.fromkeys(coverage_pool))[:4],
            "subscores":    avg_subscores,
        })

    areas.sort(key=lambda a: (a["priority"], -a["total_issues"]))
    return areas


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTE TREE  (GitHub-branch style HTML)
# ═══════════════════════════════════════════════════════════════════════════

def _build_tree(pages: list[dict[str, Any]]) -> dict:
    """Nest pages into a segment tree keyed by route parts."""
    root: dict = {}
    for p in pages:
        route = canonicalize_path_from_url(p.get("url", ""))
        parts = [s for s in route.split("/") if s] or ["(root)"]
        a      = p.get("analysis") or {}
        score  = coerce_health_score(a.get("health_score", 5), 5.0)
        bugs   = len(a.get("bugs", [])) + len(a.get("visual_issues", []))
        broken = p.get("broken_interactions", 0)
        node   = root
        for part in parts:
            node = node.setdefault(part, {"__pages": [], "__ch": {}})["__ch"]
        # store at leaf
        nd = root
        for part in parts[:-1]:
            nd = nd[part]["__ch"]
        nd.setdefault(parts[-1], {"__pages": [], "__ch": {}})
        nd[parts[-1]]["__pages"].append({"score": score, "bugs": bugs, "broken": broken,
                                          "url": p.get("url",""), "shot": p.get("screenshot","")})
    return root


def _agg(node: dict) -> tuple[float, int, int]:
    """Aggregate score/bugs/broken recursively across all descendants."""
    all_sc, all_b, all_brk = [], 0, 0
    for p in node.get("__pages", []):
        all_sc.append(p["score"]); all_b += p["bugs"]; all_brk += p["broken"]
    for child in node.get("__ch", {}).values():
        cs, cb, cbk = _agg(child)
        all_sc.append(cs); all_b += cb; all_brk += cbk
    return (round(sum(all_sc)/max(len(all_sc),1),1) if all_sc else 5.0), all_b, all_brk


def _tree_html(tree: dict, depth: int = 0, prefix: str = "", is_last_list: list | None = None) -> str:
    """Render tree nodes as GitHub-style branch lines."""
    if is_last_list is None:
        is_last_list = []
    items  = sorted(tree.items())
    result = ""
    for idx, (name, node) in enumerate(items):
        is_last = idx == len(items) - 1
        ch      = node.get("__ch", {})
        score, bugs, broken = _agg(node)
        color   = _sc(score)

        # Build the indent using │ / └ / ├ connectors
        prefix_str = ""
        for anc_last in is_last_list:
            prefix_str += "    " if anc_last else "│   "
        connector  = "└── " if is_last else "├── "
        score_dot  = f'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:{color};vertical-align:middle;flex-shrink:0;margin-right:4px"></span>'
        bug_badge  = f'<span style="font-size:.58rem;background:#ff4a6e22;color:#ff4a6e;border-radius:3px;padding:1px 5px;margin-left:4px">{bugs}🐛</span>' if bugs else ""
        brk_badge  = f'<span style="font-size:.58rem;background:#ff8c4222;color:#ff8c42;border-radius:3px;padding:1px 5px;margin-left:2px">{broken}💥</span>' if broken else ""
        ok_badge   = '<span style="font-size:.58rem;background:#22d37a22;color:#22d37a;border-radius:3px;padding:1px 5px;margin-left:4px">✓</span>' if not bugs and not broken else ""
        sc_txt     = f'<span style="font-size:.6rem;color:{color};font-weight:700;margin-left:6px">{score}</span>'

        result += (
            f'<div style="display:flex;align-items:center;line-height:1.9;font-size:.72rem">'
            f'<span style="font-family:\'JetBrains Mono\',monospace;color:#253045;white-space:pre">{prefix_str}{connector}</span>'
            f'{score_dot}'
            f'<span style="font-family:\'JetBrains Mono\',monospace;color:#c2cfe0">/{name}</span>'
            f'{sc_txt}{bug_badge}{brk_badge}{ok_badge}'
            f'</div>\n'
        )
        if ch:
            result += _tree_html(ch, depth + 1, prefix_str + connector,
                                  is_last_list + [is_last])
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  HTML FRAGMENTS
# ═══════════════════════════════════════════════════════════════════════════

def _focus_html(areas: list[dict[str, Any]]) -> str:
    if not areas:
        return "<p style='color:#3a4d6a;padding:1rem'>No routes found.</p>"
    out = ""
    for i, a in enumerate(areas):
        if a["total_issues"] == 0 and i >= 8:
            break   # don't list dozens of clean routes
        color  = _sc(a["avg_score"])
        label  = _label(a["avg_score"])
        prio   = ("🔴 Critical" if a["avg_score"] < 4
                  else "🟡 Needs work" if a["avg_score"] < 7
                  else "🟢 Clean")

        def badge(n, cls, txt):
            return f'<span class="fa-badge fa-{cls}">{n} {txt}</span>' if n else ""

        kind_badge = f'<span class="fa-badge" style="background:#4a8cff22;color:#4a8cff">{_esc(a["route_kind"])}</span>' if a.get("route_kind") else ""
        badges = (kind_badge
                + badge(a["bug_count"],    "bug",    "bug"    + ("s" if a["bug_count"]    != 1 else ""))
                + badge(a["broken_count"], "broken", "broken" + ("" if a["broken_count"] == 1 else " elements"))
                + badge(a["api_fail"],     "api",    "API fail" + ("s" if a["api_fail"]  != 1 else ""))
                + badge(a["perf_count"],   "perf",   "perf issue" + ("s" if a["perf_count"] != 1 else ""))
                + badge(a["visual_count"], "vis",    "visual"))

        recs_li  = "".join(f"<li>{r}</li>" for r in a["recs"])

        def detail_group(title, items):
            if not items: return ""
            rows = "".join(f'<div class="fa-di">• {it[:110]}</div>' for it in items)
            return f'<div class="fa-dg"><div class="fa-dt">{title}</div>{rows}</div>'

        score_detail = ""
        if a.get("subscores"):
            score_detail = detail_group(
                "📊 Heuristic subscores",
                [f"{k}: {v}/10" for k, v in sorted(a["subscores"].items())],
            )

        details = (score_detail
                 + detail_group("🐛 Bugs",                a["bugs"])
                 + detail_group("💥 Broken interactions", a["broken_int"])
                 + detail_group("🔌 API issues",          a["api"])
                 + detail_group("⚡ Performance",          a["perf"])
                 + detail_group("🎨 Visual / UX",         a["visual"] + a["ux"])
                 + detail_group("🧭 Coverage gaps",       a["coverage"]))

        out += f"""
        <div class="fa-card">
          <div class="fa-hd" onclick="toggleFa({i})">
            <span class="fa-rank">#{i+1}</span>
            <code class="fa-route">{a['route'] or '/'}</code>
            <span class="fa-prio">{prio}</span>
            {badges}
            <span style="flex:1"></span>
            <span class="fa-score" style="color:{color}">{a['avg_score']}/10</span>
            <span class="fa-lbl" style="color:{color}">{label}</span>
            <span class="fa-eff">{a['effort']}</span>
            <span class="fa-chev" id="fac{i}">▶</span>
          </div>
          <div class="fa-bd" id="fab{i}" style="display:none">
            <div class="fa-cols">
              <div>
                <div class="fa-dt" style="margin-bottom:.4rem">What to fix</div>
                <ul class="fa-recs">{recs_li}</ul>
              </div>
              <div>
                <div class="fa-dt" style="margin-bottom:.4rem">Issue details</div>
                {details or "<span style='color:#3a4d6a;font-size:.72rem'>No specific details.</span>"}
              </div>
            </div>
          </div>
        </div>"""
    return out


def _pages_html(pages: list[dict[str, Any]]) -> str:
    rows = ""
    for p in sorted(pages, key=lambda x: coerce_health_score(x.get("analysis",{}).get("health_score",5),5.0)):
        a     = p.get("analysis") or {}
        score = coerce_health_score(a.get("health_score", 5), 5.0)
        color = _sc(score)
        shot  = p.get("screenshot", "")
        fname = shot.replace("\\","/").split("/")[-1] if shot else ""
        thumb = f'<img src="screenshots/{fname}" style="width:64px;height:38px;object-fit:cover;object-position:top;border-radius:3px;vertical-align:middle;opacity:.8" onerror="this.style.display=\'none\'">' if fname else '<span style="color:#253045;font-size:.6rem">—</span>'
        load  = p.get("load_time_ms", 0)
        lc    = "#ff4a6e" if load > 5000 else "#f5c842" if load > 3000 else "#22d37a"
        broken = p.get("broken_interactions", 0)
        bugs  = len(a.get("bugs",[])) + len(a.get("visual_issues",[]))
        kind = str(a.get("route_kind", "") or "")
        summary_text = a.get("recommendations", [a.get("summary", "")])[0] if a.get("recommendations") else a.get("summary", "")
        rows += (f'<tr><td>{thumb}</td>'
                 f'<td><a href="{p.get("url","")}" target="_blank" '
                 f'style="font-family:monospace;font-size:.68rem;max-width:300px;display:inline-block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:middle"'
                 f' title="{p.get("url","")}">{p.get("url","")}</a></td>'
                 f'<td style="color:{color};font-weight:700;text-align:center">{score}</td>'
                 f'<td style="text-align:center">{p.get("http_status","?")}</td>'
                 f'<td style="color:{lc};text-align:center;font-family:monospace">{load}ms</td>'
                 f'<td style="color:#ff6b8a;text-align:center">{bugs}</td>'
                 f'<td style="color:{"#ff4a6e" if broken else "#3a4d6a"};text-align:center;font-weight:{"700" if broken else "400"}">{broken}</td>'
                 f'<td style="font-size:.68rem;color:#8a9bb5">{(f"[{kind}] " if kind else "") + str(summary_text)[:120]}</td></tr>')
    return rows


def _api_html(api_summary: list[dict[str, Any]]) -> str:
    rows = ""
    for c in sorted(api_summary, key=lambda x: -x.get("failures",0))[:80]:
        er  = c.get("error_rate", 0)
        ec  = "#ff4a6e" if er > 0.1 else "#f5c842" if er > 0 else "#22d37a"
        lat = c.get("avg_latency_ms", 0)
        lc  = "#ff4a6e" if lat > 3000 else "#f5c842" if lat > 1000 else "#22d37a"
        m   = c.get("method","").upper()
        mc  = {"GET":"#22d37a","POST":"#4a8cff","PUT":"#f5c842","PATCH":"#ff8c42","DELETE":"#ff4a6e"}.get(m,"#9b6dff")
        rows += (f'<tr>'
                 f'<td><code style="background:{mc}22;color:{mc};padding:1px 5px;border-radius:3px;font-size:.62rem;font-weight:700">{m}</code></td>'
                 f'<td style="font-family:monospace;word-break:break-all;font-size:.68rem">{c.get("endpoint","")}</td>'
                 f'<td style="text-align:center">{c.get("calls",0)}</td>'
                 f'<td style="color:{ec};text-align:center">{c.get("failures",0)} <small style="color:#3a4d6a">({er*100:.0f}%)</small></td>'
                 f'<td style="color:{lc};text-align:center;font-family:monospace">{lat}ms</td></tr>')
    return rows


def _int_html(stats: list[dict[str, Any]]) -> str:
    rows = ""
    for r in sorted(stats, key=lambda x: -(x.get("fail",0)+x.get("neutral",0)))[:80]:
        rel = r.get("reliability", 0)
        rc  = "#22d37a" if rel >= 0.7 else "#f5c842" if rel >= 0.3 else "#ff4a6e"
        flk = r.get("flakiness", 0)
        fc  = "#ff4a6e" if flk > 0.5 else "#f5c842" if flk > 0.2 else "#3a4d6a"
        rows += (f'<tr>'
                 f'<td style="font-family:monospace;font-size:.65rem;color:#4a8cff">{r.get("route","")}</td>'
                 f'<td style="font-size:.7rem">{r.get("action","")[:70]}</td>'
                 f'<td style="text-align:center">{r.get("attempts",0)}</td>'
                 f'<td style="color:#22d37a;text-align:center">{r.get("success",0)}</td>'
                 f'<td style="color:#3a4d6a;text-align:center">{r.get("neutral",0)}</td>'
                 f'<td style="color:#ff4a6e;text-align:center">{r.get("fail",0)}</td>'
                 f'<td style="color:{rc};text-align:center;font-weight:600">{rel*100:.0f}%</td>'
                 f'<td style="color:{fc};text-align:center">{flk*100:.0f}%</td></tr>')
    return rows


def _wf_html(results: list[dict[str, Any]]) -> str:
    rows = ""
    for w in results:
        c   = "#22d37a" if w.get("passed") else "#ff4a6e"
        lbl = "✓ PASS"  if w.get("passed") else "✗ FAIL"
        rows += (f'<tr>'
                 f'<td style="font-weight:600">{w.get("scenario","")}</td>'
                 f'<td style="font-family:monospace;font-size:.68rem">{w.get("route","")}</td>'
                 f'<td style="color:{c};font-weight:700">{lbl}</td>'
                 f'<td style="font-family:monospace">{w.get("duration_ms",0)}ms</td>'
                 f'<td>{len(w.get("steps",[]))}</td>'
                 f'<td style="color:#ff4a6e;font-size:.68rem">{w.get("error","")[:100]}</td></tr>')
    return rows


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


def _broken_element_rows(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    severity_rank = {"broken": 0, "api_error": 1, "timeout": 2, "navigation": 3, "dom_mutation": 4, "no_change": 5}
    for page in pages:
        route = canonicalize_path_from_url(page.get("url", ""))
        for ir in page.get("interaction_results", []):
            outcome = str(ir.get("outcome", ""))
            if not (ir.get("broke") or outcome in {"broken", "api_error", "timeout"}):
                continue
            evidence = list(ir.get("js_errors", [])) + list(ir.get("net_failures", []))
            if not evidence and ir.get("api_failures"):
                evidence.append(f"{ir.get('api_failures', 0)} API failures during action")
            if not evidence:
                evidence.append(outcome or "interaction failure")
            rows.append({
                "route": route,
                "url": page.get("url", ""),
                "action": ir.get("action", "?"),
                "action_kind": ir.get("action_kind", ""),
                "selector": ir.get("selector", ""),
                "scope": ir.get("scope_label") or ir.get("chrome_context") or ir.get("section") or "",
                "state": ir.get("from_state_label", ""),
                "outcome": outcome,
                "evidence": evidence[0][:140],
                "to_route": ir.get("to_route", route),
                "state_depth": ir.get("state_depth", 0),
                "api_failures": ir.get("api_failures", 0),
                "surface_delta": ir.get("surface_delta", 0),
                "severity": severity_rank.get(outcome, 9),
            })
    rows.sort(key=lambda row: (row["severity"], row["route"], row["action"]))
    return rows[:120]


def _broken_elements_html(rows: list[dict[str, Any]]) -> str:
    out = ""
    for row in rows:
        outcome_color = "#ff4a6e" if row["outcome"] in {"broken", "timeout"} else "#ff8c42"
        out += (
            f"<tr>"
            f"<td style='font-family:monospace;font-size:.66rem;color:#4a8cff'>{_esc(row['route'])}</td>"
            f"<td style='font-size:.72rem'>{_esc(row['action'])}<div style='color:#8a9bb5;font-size:.6rem'>{_esc(row['action_kind'])}</div></td>"
            f"<td style='font-size:.68rem;color:#8a9bb5'>{_esc(row['scope']) or 'page'}</td>"
            f"<td style='font-size:.68rem;color:#8a9bb5'>{_esc(row['state'])}<div style='font-size:.58rem;color:#3a4d6a'>depth {row['state_depth']}</div></td>"
            f"<td style='color:{outcome_color};font-weight:700'>{_esc(row['outcome'])}</td>"
            f"<td style='font-size:.68rem;color:#c2cfe0'>{_esc(row['evidence'])}<div style='font-size:.58rem;color:#3a4d6a'>{_esc(row['selector'])[:70]}</div></td>"
            f"<td style='font-family:monospace;font-size:.66rem'>{_esc(row['to_route'])}<div style='font-size:.58rem;color:#3a4d6a'>{row['api_failures']} api fail · +{row['surface_delta']} surface</div></td>"
            f"</tr>"
        )
    return out


def _line_chart_svg(values: list[float], color: str, y_max: float, labels: list[str], suffix: str = "") -> str:
    if not values:
        return "<div class='empty-note'>No trend data yet.</div>"
    width, height = 520, 170
    left, top, bottom = 30, 18, 28
    usable_w = width - left - 12
    usable_h = height - top - bottom
    upper = max(float(y_max or 0), max(values), 1.0)
    points = []
    area = [f"{left},{height - bottom}"]
    for idx, value in enumerate(values):
        x = left + (usable_w * idx / max(len(values) - 1, 1))
        y = top + usable_h - (float(value) / upper) * usable_h
        points.append((x, y, value))
        area.append(f"{x:.1f},{y:.1f}")
    area.append(f"{points[-1][0]:.1f},{height - bottom}")
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in points)
    dots = "".join(
        f"<circle cx='{x:.1f}' cy='{y:.1f}' r='3.5' fill='{color}' />"
        for x, y, _ in points
    )
    x_labels = "".join(
        f"<text x='{x:.1f}' y='{height - 8}' text-anchor='middle'>{_esc(label)}</text>"
        for (x, _, _), label in zip(points, labels)
    )
    value_labels = "".join(
        f"<text x='{x:.1f}' y='{max(y - 8, 10):.1f}' text-anchor='middle'>{value:.1f}{suffix}</text>"
        for x, y, value in points[-3:]
    )
    return (
        f"<svg viewBox='0 0 {width} {height}' class='trend-svg' role='img' aria-label='trend chart'>"
        f"<line x1='{left}' y1='{height - bottom}' x2='{width - 8}' y2='{height - bottom}' stroke='#1e2840' stroke-width='1' />"
        f"<line x1='{left}' y1='{top}' x2='{left}' y2='{height - bottom}' stroke='#161d2e' stroke-width='1' />"
        f"<polygon points='{' '.join(area)}' fill='{color}18' />"
        f"<polyline points='{poly}' fill='none' stroke='{color}' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round' />"
        f"{dots}{x_labels}<g fill='#8a9bb5' font-size='9'>{value_labels}</g></svg>"
    )


def _history_graphs_html(history: list[dict[str, Any]]) -> str:
    if not history:
        return "<p class='empty-note'>Not enough history yet for trend charts.</p>"
    recent = history[-8:]
    labels = [str(i + 1) for i in range(len(recent))]
    score_values = [float(run.get("avg_score", 0)) for run in recent]
    defect_values = [
        float(run.get("bug_count", 0) + run.get("broken_interactions", 0) + run.get("api_failure_count", 0))
        for run in recent
    ]
    return f"""
    <div class="chart-grid">
      <div class="chart-card">
        <div class="chart-title">Run Score Trend</div>
        <div class="chart-sub">Last {len(recent)} runs. Higher is better.</div>
        {_line_chart_svg(score_values, "#00d4aa", 10.0, labels, "/10")}
      </div>
      <div class="chart-card">
        <div class="chart-title">Defect Trend</div>
        <div class="chart-sub">Bugs + broken interactions + API failures per run.</div>
        {_line_chart_svg(defect_values, "#ff8c42", max(defect_values) or 1.0, labels)}
      </div>
    </div>"""


def _route_risk_chart_html(focus_areas: list[dict[str, Any]]) -> str:
    if not focus_areas:
        return "<p class='empty-note'>No route risk data.</p>"
    top = sorted(focus_areas, key=lambda area: area["total_issues"], reverse=True)[:8]
    max_issues = max(area["total_issues"] for area in top) or 1
    rows = ""
    for area in top:
        width = (area["total_issues"] / max_issues) * 100
        color = _sc(area["avg_score"])
        rows += f"""
        <div class="bar-row">
          <div class="bar-label"><code>{_esc(area['route'] or '/')}</code></div>
          <div class="bar-track"><div class="bar-fill" style="width:{width:.1f}%;background:{color}"></div></div>
          <div class="bar-meta">{area['total_issues']} issues · {area['avg_score']}/10</div>
        </div>"""
    return rows


def _exploration_chart_html(pages: list[dict[str, Any]]) -> str:
    by_route: dict[str, dict[str, int]] = defaultdict(lambda: {"states": 0, "actions": 0, "broken": 0})
    for page in pages:
        route = canonicalize_path_from_url(page.get("url", ""))
        by_route[route]["states"] += int(page.get("states_seen", 0) or 0)
        by_route[route]["actions"] += len(page.get("interaction_results", []) or [])
        by_route[route]["broken"] += int(page.get("broken_interactions", 0) or 0)
    ranked = sorted(by_route.items(), key=lambda item: (item[1]["states"] + item[1]["actions"]), reverse=True)[:8]
    if not ranked:
        return "<p class='empty-note'>No exploration depth data.</p>"
    max_actions = max(stats["actions"] for _, stats in ranked) or 1
    max_states = max(stats["states"] for _, stats in ranked) or 1
    rows = ""
    for route, stats in ranked:
        state_w = (stats["states"] / max_states) * 100
        action_w = (stats["actions"] / max_actions) * 100
        rows += f"""
        <div class="depth-row">
          <div class="depth-route"><code>{_esc(route or '/')}</code></div>
          <div class="depth-metrics">
            <div class="mini-track"><span class="mini-fill mini-fill-state" style="width:{state_w:.1f}%"></span></div>
            <div class="mini-track"><span class="mini-fill mini-fill-action" style="width:{action_w:.1f}%"></span></div>
          </div>
          <div class="depth-meta">{stats['states']} states · {stats['actions']} actions · {stats['broken']} broken</div>
        </div>"""
    return rows


def _route_regressions_html(deltas: dict[str, Any]) -> str:
    regressions = list(deltas.get("route_regressions", []) or [])
    if not regressions:
        return "<p class='empty-note'>No route regressions were detected against the previous run.</p>"
    rows = ""
    for row in regressions[:8]:
        score_color = "#ff4a6e" if row["score_delta"] < 0 else "#22d37a"
        rows += f"""
        <div class="bar-row">
          <div class="bar-label"><code>{_esc(row['route'])}</code></div>
          <div class="bar-track"><div class="bar-fill" style="width:{min(100, row['severity'] * 12):.1f}%;background:#ff8c42"></div></div>
          <div class="bar-meta" style="color:#c2cfe0">
            <span style="color:{score_color}">score {row['score_delta']:+.1f}</span>
            · broken {row['broken_delta']:+d}
            · api {row['api_delta']:+d}
            · links {row['discovery_delta']:+d}
          </div>
        </div>"""
    return rows


def _route_regressions_html_v2(deltas: dict[str, Any]) -> str:
    regressions = list(deltas.get("route_regressions", []) or [])
    if not regressions:
        return "<p class='empty-note'>No route regressions were detected against the previous run.</p>"
    rows = ""
    for row in regressions[:8]:
        score_color = "#ff4a6e" if row["score_delta"] < 0 else "#22d37a"
        rows += f"""
        <div class="bar-row">
          <div class="bar-label"><code>{_esc(row['route'])}</code></div>
          <div class="bar-track"><div class="bar-fill" style="width:{min(100, row['severity'] * 12):.1f}%;background:#ff8c42"></div></div>
          <div class="bar-meta" style="color:#c2cfe0">
            <span style="color:{score_color}">score {row['score_delta']:+.1f}</span>
            · broken {row['broken_delta']:+d}
            · api {row['api_delta']:+d}
            · links {row['discovery_delta']:+d}
            · states {row['states_delta']:+d}
            · actions {row['actions_delta']:+d}
            · depth {row['depth_delta']:+d}
          </div>
        </div>"""
    return rows


def _build_state_graph_evidence(pages: list[dict[str, Any]]) -> dict[str, Any]:
    states: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    routes: dict[str, set[str]] = defaultdict(set)
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

        for ir in page.get("interaction_results", []):
            if (
                str(ir.get("action_kind", "") or "") in FORM_ACTION_KINDS
                or ir.get("form_context")
                or ir.get("form_intent")
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
            edge_key = (
                from_state_id,
                to_state_id,
                str(ir.get("action", "") or "?"),
                str(ir.get("outcome", "") or "unknown"),
                str(ir.get("action_kind", "") or "click"),
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
                "action_kind": str(ir.get("action_kind", "") or "click"),
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
    max_depth = max((int(node.get("depth", 0) or 0) for node in state_rows), default=0)
    return {
        "summary": {
            "routes": len(routes),
            "states": len(states),
            "transitions": len(edge_rows),
            "deepest_state": max_depth,
            "modal_states": sum(1 for node in state_rows if node.get("kind") == "modal"),
            "form_touches": form_touches,
        },
        "states": state_rows,
        "edges": edge_rows,
    }


def _state_hotspots_html(graph: dict[str, Any]) -> str:
    states = list(graph.get("states", []) or [])
    if not states:
        return "<p class='empty-note'>No state graph evidence was captured.</p>"
    top = states[:8]
    max_conn = max((row["incoming"] + row["outgoing"]) for row in top) or 1
    rows = ""
    for row in top:
        width = ((row["incoming"] + row["outgoing"]) / max_conn) * 100
        kind_color = {"modal": "#ff8c42", "section": "#4a8cff", "page": "#22d37a"}.get(str(row.get("kind", "")), "#9b6dff")
        rows += f"""
        <div class="bar-row">
          <div class="bar-label"><code>{_esc(row['route'])}</code><div class="sg-sub">{_esc(row['label'])}</div></div>
          <div class="bar-track"><div class="bar-fill" style="width:{width:.1f}%;background:{kind_color}"></div></div>
          <div class="bar-meta">{row['incoming'] + row['outgoing']} links · depth {row['depth']} · {row['visits']} seen</div>
        </div>"""
    return rows


def _state_path_cards_html(graph: dict[str, Any]) -> str:
    edges = list(graph.get("edges", []) or [])
    if not edges:
        return "<p class='empty-note'>No route-state transitions were recorded.</p>"
    out = ""
    for edge in edges[:8]:
        outcome = str(edge.get("outcome", "") or "unknown")
        outcome_color = "#ff4a6e" if outcome in {"broken", "timeout"} else "#ff8c42" if outcome == "api_error" else "#22d37a"
        meta = [
            f"{edge['count']}x",
            f"depth {edge['max_depth']}",
        ]
        if edge.get("submitted"):
            meta.append(f"{edge['submitted']} submit")
        if edge.get("validation"):
            meta.append(f"{edge['validation']} validation")
        if edge.get("api_failures"):
            meta.append(f"{edge['api_failures']} api fail")
        out += f"""
        <div class="path-card">
          <div class="path-route"><code>{_esc(edge['from_route'])}</code><span class="path-badge" style="color:{outcome_color};border-color:{outcome_color}">{_esc(outcome)}</span></div>
          <div class="path-main">
            <span class="path-state">{_esc(edge['from_label'])}</span>
            <span class="path-arrow">→</span>
            <span class="path-action">{_esc(edge['action'])}</span>
            <span class="path-arrow">→</span>
            <span class="path-state">{_esc(edge['to_label'])}</span>
          </div>
          <div class="path-meta">{_esc(' · '.join(meta))}</div>
          <div class="path-sub">{_esc(edge['from_kind'])} → {_esc(edge['to_kind'])} · target {_esc(edge['to_route'])}</div>
        </div>"""
    return out


def _state_transition_rows_html(graph: dict[str, Any]) -> str:
    rows = ""
    for edge in list(graph.get("edges", []) or [])[:40]:
        meta = []
        if edge.get("submitted"):
            meta.append(f"{edge['submitted']} submit")
        if edge.get("validation"):
            meta.append(f"{edge['validation']} validation")
        if edge.get("api_failures"):
            meta.append(f"{edge['api_failures']} api fail")
        if edge.get("surface_delta"):
            meta.append(f"+{edge['surface_delta']} surface")
        rows += (
            f"<tr>"
            f"<td style='font-family:monospace;font-size:.66rem;color:#4a8cff'>{_esc(edge['from_route'])}</td>"
            f"<td style='font-size:.68rem;color:#c2cfe0'>{_esc(edge['from_label'])}<div style='font-size:.58rem;color:#3a4d6a'>{_esc(edge['from_kind'])}</div></td>"
            f"<td style='font-size:.7rem'>{_esc(edge['action'])}<div style='font-size:.58rem;color:#8a9bb5'>{_esc(edge['action_kind'])}</div></td>"
            f"<td style='color:{'#ff4a6e' if edge['outcome'] in {'broken','timeout'} else '#ff8c42' if edge['outcome']=='api_error' else '#22d37a'};font-weight:700'>{_esc(edge['outcome'])}</td>"
            f"<td style='font-family:monospace;font-size:.66rem'>{_esc(edge['to_route'])}</td>"
            f"<td style='font-size:.68rem;color:#c2cfe0'>{_esc(edge['to_label'])}<div style='font-size:.58rem;color:#3a4d6a'>{_esc(edge['to_kind'])}</div></td>"
            f"<td style='text-align:center'>{edge['count']}</td>"
            f"<td style='font-size:.66rem;color:#8a9bb5'>{_esc(' · '.join(meta) or 'state transition')}</td>"
            f"</tr>"
        )
    return rows


def _form_evidence_rows(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str, str, str], dict[str, Any]] = {}
    for page in pages:
        route = canonicalize_path_from_url(page.get("url", ""))
        for ir in page.get("interaction_results", []):
            action_kind = str(ir.get("action_kind", "") or "")
            form_intent = str(ir.get("form_intent", "") or "")
            if not (
                action_kind in FORM_ACTION_KINDS
                or form_intent
                or ir.get("form_context")
                or ir.get("submitted")
                or ir.get("validation_errors")
            ):
                continue
            form_scope = str(ir.get("form_context") or ir.get("modal_context") or ir.get("section") or ir.get("scope_label") or "page")
            key = (
                route,
                form_scope,
                str(ir.get("action", "") or "?"),
                form_intent or "general",
                str(ir.get("submit_action", "") or ""),
                str(ir.get("outcome", "") or "unknown"),
                str(ir.get("to_route", "") or route),
            )
            row = grouped.setdefault(key, {
                "route": route,
                "form_scope": form_scope,
                "field": str(ir.get("action", "") or "?"),
                "intent": form_intent or "general",
                "submit_action": str(ir.get("submit_action", "") or ""),
                "outcome": str(ir.get("outcome", "") or "unknown"),
                "target": str(ir.get("to_route", "") or route),
                "attempts": 0,
                "submits": 0,
                "validation": 0,
                "validation_sample": "",
                "state": str(ir.get("from_state_label", "") or ""),
            })
            row["attempts"] += 1
            row["submits"] += 1 if ir.get("submitted") else 0
            row["validation"] += len(ir.get("validation_errors", []) or [])
            if not row["validation_sample"] and ir.get("validation_errors"):
                row["validation_sample"] = str((ir.get("validation_errors") or [""])[0])
    rows = sorted(
        grouped.values(),
        key=lambda item: (-item["submits"], -item["validation"], item["route"], item["field"]),
    )
    return rows[:80]


def _form_evidence_html(rows: list[dict[str, Any]]) -> str:
    out = ""
    for row in rows:
        submit_text = row["submit_action"] or ("submitted" if row["submits"] else "not submitted")
        validation_text = row["validation_sample"] or (f"{row['validation']} validation signal(s)" if row["validation"] else "none")
        out += (
            f"<tr>"
            f"<td style='font-family:monospace;font-size:.66rem;color:#4a8cff'>{_esc(row['route'])}</td>"
            f"<td style='font-size:.68rem;color:#8a9bb5'>{_esc(row['form_scope'])}<div style='font-size:.58rem;color:#3a4d6a'>{_esc(row['state'])}</div></td>"
            f"<td style='font-size:.72rem'>{_esc(row['field'])}</td>"
            f"<td style='font-size:.66rem;color:#c2cfe0'>{_esc(row['intent'])}</td>"
            f"<td style='font-size:.66rem;color:#00d4aa'>{_esc(submit_text)}<div style='font-size:.58rem;color:#3a4d6a'>{row['attempts']} attempt(s)</div></td>"
            f"<td style='font-size:.66rem;color:#f5c842'>{_esc(validation_text)[:110]}</td>"
            f"<td style='color:{'#ff4a6e' if row['outcome'] in {'broken','timeout'} else '#ff8c42' if row['outcome']=='api_error' else '#22d37a'};font-weight:700'>{_esc(row['outcome'])}</td>"
            f"<td style='font-family:monospace;font-size:.66rem'>{_esc(row['target'])}</td>"
            f"</tr>"
        )
    return out


def _normalize_report_brief(raw: dict[str, Any] | None, fallback: dict[str, Any]) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    findings = []
    for item in data.get("top_findings", []):
        if not isinstance(item, dict):
            continue
        findings.append({
            "route": str(item.get("route", "") or ""),
            "problem": str(item.get("problem", "") or ""),
            "evidence": str(item.get("evidence", "") or ""),
            "likely_fix": str(item.get("likely_fix", "") or ""),
            "severity": str(item.get("severity", "") or ""),
        })
    if not findings:
        findings = fallback["top_findings"]
    return {
        "headline": str(data.get("headline", "") or fallback["headline"]),
        "release_readiness": str(data.get("release_readiness", "") or fallback["release_readiness"]),
        "summary": str(data.get("summary", "") or fallback["summary"]),
        "top_findings": findings[:5],
        "developer_notes": [str(x) for x in data.get("developer_notes", []) if str(x).strip()][:5] or fallback["developer_notes"],
        "coverage_gaps": [str(x) for x in data.get("coverage_gaps", []) if str(x).strip()][:4] or fallback["coverage_gaps"],
    }


def _heuristic_report_brief(
    focus_areas: list[dict[str, Any]],
    broken_rows: list[dict[str, Any]],
    workflow_results: list[dict[str, Any]],
    coverage: dict[str, Any],
    deltas: dict[str, Any],
) -> dict[str, Any]:
    failing_workflows = [wf for wf in workflow_results if not wf.get("passed")]
    top_areas = focus_areas[:4]
    if failing_workflows or any(area["avg_score"] < 4 for area in top_areas):
        readiness = "blocker"
    elif any(area["total_issues"] > 0 for area in top_areas):
        readiness = "caution"
    else:
        readiness = "ship"
    findings = []
    for area in top_areas[:3]:
        evidence_parts = [f"score {area['avg_score']}/10"]
        if area["bug_count"]:
            evidence_parts.append(f"{area['bug_count']} bugs")
        if area["broken_count"]:
            evidence_parts.append(f"{area['broken_count']} broken interactions")
        if area["api_fail"]:
            evidence_parts.append(f"{area['api_fail']} API failures")
        findings.append({
            "route": area["route"],
            "problem": area["recs"][0] if area["recs"] else "Route needs review",
            "evidence": ", ".join(evidence_parts),
            "likely_fix": area["recs"][1] if len(area["recs"]) > 1 else area["recs"][0] if area["recs"] else "Inspect the failing page flow and reproduce locally.",
            "severity": "critical" if area["avg_score"] < 4 else "warning" if area["avg_score"] < 7 else "info",
        })
    developer_notes = []
    if failing_workflows:
        developer_notes.append(f"{len(failing_workflows)} workflow regression(s) failed and should be treated as release blockers.")
    if broken_rows:
        developer_notes.append(f"{len(broken_rows)} element-level failures were captured with route and action evidence.")
    if deltas:
        developer_notes.append(
            f"Compared with the previous run: score delta {deltas.get('score_delta', 0)}, bug delta {deltas.get('bug_delta', 0)}, broken delta {deltas.get('broken_delta', 0)}."
        )
        if deltas.get("route_regressions"):
            worst = deltas["route_regressions"][0]
            developer_notes.append(
                f"Biggest route regression: {worst['route']} (score {worst['score_delta']:+.1f}, broken {worst['broken_delta']:+d}, api {worst['api_delta']:+d})."
            )
    coverage_gaps = []
    if coverage and coverage.get("pct_pages_with_interactions", 0) < 70:
        coverage_gaps.append("Interaction coverage is still shallow on part of the crawl. Add more safe exploration or targeted workflows.")
    if not workflow_results:
        coverage_gaps.append("No workflow regressions ran, so route discovery is stronger than business-flow validation.")
    if not coverage_gaps:
        coverage_gaps.append("Coverage is acceptable for the current crawl depth, but responsive breakpoint testing is still not part of this report.")
    return {
        "headline": "QA handoff generated from crawl evidence",
        "release_readiness": readiness,
        "summary": "This run combines crawler evidence, interaction results, API telemetry, and workflow outcomes into a fix-oriented handoff for engineering.",
        "top_findings": findings or [{
            "route": "/",
            "problem": "No critical issues were detected in the current run.",
            "evidence": "No failing workflows, no broken interactions, and route scores remained stable.",
            "likely_fix": "Use the remaining tables to review low-severity polish items.",
            "severity": "info",
        }],
        "developer_notes": developer_notes or ["No additional developer notes."],
        "coverage_gaps": coverage_gaps,
    }


def _llm_report_brief(
    settings: Settings,
    pages: list[dict[str, Any]],
    api_summary: list[dict[str, Any]],
    focus_areas: list[dict[str, Any]],
    workflow_results: list[dict[str, Any]],
    coverage: dict[str, Any],
    deltas: dict[str, Any],
    broken_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    fallback = _heuristic_report_brief(focus_areas, broken_rows, workflow_results, coverage, deltas)
    if not settings.llm_report_enabled:
        return fallback

    failing_workflows = [
        {
            "scenario": wf.get("scenario", ""),
            "route": wf.get("route", ""),
            "error": wf.get("error", ""),
        }
        for wf in workflow_results if not wf.get("passed")
    ][:6]
    failed_apis = [
        {
            "endpoint": row.get("endpoint", ""),
            "method": row.get("method", ""),
            "failures": row.get("failures", 0),
            "error_rate": row.get("error_rate", 0),
            "avg_latency_ms": row.get("avg_latency_ms", 0),
        }
        for row in sorted(api_summary, key=lambda item: (-item.get("failures", 0), -item.get("avg_latency_ms", 0)))[:8]
        if row.get("failures", 0) or row.get("avg_latency_ms", 0) > 2000
    ]
    context = {
        "run_summary": _run_summary(pages, api_summary),
        "deltas": deltas,
        "coverage": coverage,
        "top_routes": [
            {
                "route": area["route"],
                "avg_score": area["avg_score"],
                "total_issues": area["total_issues"],
                "recommendations": area["recs"][:3],
                "bugs": area["bugs"][:3],
                "broken_interactions": area["broken_int"][:3],
                "api_issues": area["api"][:2],
                "performance_issues": area["perf"][:2],
                "visual_issues": area["visual"][:2],
            }
            for area in focus_areas[:6]
        ],
        "workflow_failures": failing_workflows,
        "failed_apis": failed_apis,
        "broken_elements": broken_rows[:8],
        "route_regressions": deltas.get("route_regressions", [])[:6],
    }
    prompt = f"""You are writing a developer-facing QA handoff.
Return JSON only with keys:
headline, release_readiness, summary, top_findings, developer_notes, coverage_gaps.

Rules:
- release_readiness must be one of: ship, caution, blocker
- top_findings must be a list of up to 5 objects with keys:
  route, problem, evidence, likely_fix, severity
- severity should be one of: critical, warning, info
- Be specific and evidence-based. Do not invent bugs or breakpoints not present in the data.
- Focus on what developers should fix first.

Evidence:
{json.dumps(context, indent=2)}
"""
    messages = [
        {"role": "system", "content": "Return JSON only. No markdown. No prose outside JSON."},
        {"role": "user", "content": prompt},
    ]
    try:
        raw = call_chat(
            settings,
            messages,
            purpose="report",
            json_mode=True,
            temperature=0,
            max_tokens=2200,
        )
        parsed = extract_json_object(raw)
        if parsed is None:
            raise ValueError("No JSON object in report brief")
        return _normalize_report_brief(parsed, fallback)
    except Exception as exc:
        llm_log(settings, f"[LLM][ERROR] Report brief failed: {type(exc).__name__}: {exc}", force=True)
        return fallback


def _brief_html(brief: dict[str, Any]) -> str:
    readiness = str(brief.get("release_readiness", "caution")).lower()
    badge_color = {
        "ship": "#22d37a",
        "caution": "#f5c842",
        "blocker": "#ff4a6e",
    }.get(readiness, "#f5c842")
    findings_html = ""
    for finding in brief.get("top_findings", []):
        severity = str(finding.get("severity", "info")).lower()
        sev_color = {"critical": "#ff4a6e", "warning": "#ff8c42", "info": "#4a8cff"}.get(severity, "#4a8cff")
        findings_html += f"""
        <div class="brief-item">
          <div class="brief-route"><code>{_esc(finding.get('route') or '/')}</code><span class="brief-sev" style="color:{sev_color}">{_esc(severity)}</span></div>
          <div class="brief-problem">{_esc(finding.get('problem'))}</div>
          <div class="brief-evidence">{_esc(finding.get('evidence'))}</div>
          <div class="brief-fix"><strong>Likely fix:</strong> {_esc(finding.get('likely_fix'))}</div>
        </div>"""
    notes = "".join(f"<li>{_esc(note)}</li>" for note in brief.get("developer_notes", []))
    gaps = "".join(f"<li>{_esc(gap)}</li>" for gap in brief.get("coverage_gaps", []))
    return f"""
    <div class="brief-shell">
      <div class="brief-head">
        <div>
          <div class="brief-title">{_esc(brief.get('headline'))}</div>
          <div class="brief-summary">{_esc(brief.get('summary'))}</div>
        </div>
        <div class="brief-badge" style="border-color:{badge_color};color:{badge_color}">{_esc(readiness)}</div>
      </div>
      <div class="brief-grid">{findings_html}</div>
      <div class="brief-foot">
        <div>
          <div class="brief-sub">Developer notes</div>
          <ul>{notes}</ul>
        </div>
        <div>
          <div class="brief-sub">Coverage gaps</div>
          <ul>{gaps}</ul>
        </div>
      </div>
    </div>"""


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN REPORT
# ═══════════════════════════════════════════════════════════════════════════

def generate_report(
    settings:          Settings,
    pages:             list[dict[str, Any]],
    api_summary:       list[dict[str, Any]],
    error_map:         dict[str, list[str]],
    interaction_stats: list[dict[str, Any]] | None = None,
    workflow_results:  list[dict[str, Any]] | None = None,
    coverage:          dict[str, Any]       | None = None,
) -> None:
    interaction_stats = interaction_stats or []
    workflow_results  = workflow_results  or []
    coverage          = coverage          or {}
    broken_rows       = _broken_element_rows(pages)
    current_run       = _run_summary(pages, api_summary)
    history           = (_load_history(settings) + [current_run])[-12:]
    deltas            = compute_deltas(history)

    scores     = [coerce_health_score(p.get("analysis",{}).get("health_score",5),5.0) for p in pages]
    avg_score  = round(sum(scores)/max(len(scores),1), 2)
    all_bugs: list[str] = []
    for p in pages:
        all_bugs.extend((p.get("analysis") or {}).get("bugs",[]))
    total_broken  = sum(p.get("broken_interactions",0) for p in pages)
    total_api_fail = sum(p.get("api_failures",0) for p in pages)
    slow_pages    = [p for p in pages if p.get("load_time_ms",0) > 3000]
    unique_routes = len({canonicalize_path_from_url(p.get("url","")) for p in pages})

    focus_areas   = _focus_areas(pages)
    critical_n    = sum(1 for a in focus_areas if a["avg_score"] < 4)
    warning_n     = sum(1 for a in focus_areas if 4 <= a["avg_score"] < 7)
    report_brief  = _llm_report_brief(
        settings,
        pages,
        api_summary,
        focus_areas,
        workflow_results,
        coverage,
        deltas,
        broken_rows,
    )

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")

    # Executive summary (plain English, actionable)
    hc = _sc(avg_score)
    hl = _label(avg_score)
    exec_para = (
        f'Crawled <strong>{len(pages)} pages</strong> across <strong>{unique_routes} routes</strong>. '
        f'Overall platform health: <strong style="color:{hc}">{hl} ({avg_score}/10)</strong>. '
    )
    public_count = int(current_run.get("phase_counts", {}).get("public", 0) or 0)
    auth_count = int(current_run.get("phase_counts", {}).get("authenticated", 0) or 0)
    if public_count:
        exec_para += (
            f'Pre-auth coverage: <strong>{public_count} public page(s)</strong>; '
            f'authenticated coverage: <strong>{auth_count} page(s)</strong>. '
        )
    if critical_n:
        exec_para += f'<strong style="color:#ff4a6e">{critical_n} route(s) need immediate attention</strong> — score below 4/10. '
    if total_broken:
        exec_para += f'<strong style="color:#ff8c42">{total_broken} clickable element(s) crash on interaction</strong> — fix before any release. '
    if total_api_fail:
        exec_para += f'<strong style="color:#f5c842">{total_api_fail} API call(s) failing</strong> — likely causing blank screens or missing content. '
    if slow_pages:
        exec_para += f'{len(slow_pages)} page(s) exceed 3 s load time — review bundle size and data fetching. '
    if avg_score >= 8 and not critical_n:
        exec_para += 'Platform is in excellent shape overall. Focus on minor polish items below. '

    # Coverage KPIs
    cov_html = ""
    if coverage:
        cov_html = f"""
        <div class="kpi-row" style="margin-top:.6rem">
          <div class="kpi"><div class="kv" style="color:#00d4aa">{coverage.get('pct_pages_with_interactions',0):.0f}%</div><div class="kk">Pages w/ UI actions</div></div>
          <div class="kpi"><div class="kv">{coverage.get('unique_actions_count',0)}</div><div class="kk">Unique actions</div></div>
          <div class="kpi"><div class="kv">{coverage.get('pct_critical_workflows',0):.0f}%</div><div class="kk">Critical workflows</div></div>
          <div class="kpi"><div class="kv" style="color:#ff4a6e">{total_broken}</div><div class="kk">Broken interactions</div></div>
        </div>"""

    focus_html = _focus_html(focus_areas)
    tree_html  = _tree_html(_build_tree(pages))
    page_rows  = _pages_html(pages)
    api_rows   = _api_html(api_summary)
    int_rows   = _int_html(interaction_stats)
    wf_rows    = _wf_html(workflow_results)
    broken_rows_html = _broken_elements_html(broken_rows)
    form_rows = _form_evidence_rows(pages)
    form_rows_html = _form_evidence_html(form_rows)
    history_graphs_html = _history_graphs_html(history)
    route_risk_html = _route_risk_chart_html(focus_areas)
    exploration_html = _exploration_chart_html(pages)
    regression_html = _route_regressions_html_v2(deltas)
    brief_html = _brief_html(report_brief)
    graph_evidence = build_state_graph_evidence(pages)
    graph_summary = graph_evidence.get("summary", {})
    state_hotspots_html = _state_hotspots_html(graph_evidence)
    state_paths_html = _state_path_cards_html(graph_evidence)
    state_transition_rows = _state_transition_rows_html(graph_evidence)

    def table_section(title, icon, anchor, headers, rows_html, empty="No data."):
        if not rows_html:
            return f'<section id="{anchor}"><h2><span class="si">{icon}</span>{title}</h2><p class="empty-note">{empty}</p></section>'
        ths = "".join(f"<th>{h}</th>" for h in headers)
        return f"""<section id="{anchor}">
          <h2><span class="si">{icon}</span>{title}</h2>
          <div class="tbl-wrap"><table><thead><tr>{ths}</tr></thead><tbody>{rows_html}</tbody></table></div>
        </section>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QA Report · {now} UTC</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Syne:wght@400;600;700;800&display=swap');
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#060810;--surf:#0a0d16;--card:#0f1320;
  --b1:#161d2e;--b2:#1e2840;--dim:#253045;
  --text:#c2cfe0;--muted:#3a4d6a;
  --blue:#4a8cff;--cyan:#00d4aa;--green:#22d37a;
  --yellow:#f5c842;--orange:#ff8c42;--red:#ff4a6e;--purple:#9b6dff;
}}
body{{font-family:'Syne',system-ui,sans-serif;background:var(--bg);color:var(--text);line-height:1.5;min-height:100vh}}
a{{color:var(--blue);text-decoration:none}}
a:hover{{text-decoration:underline}}
code{{font-family:'IBM Plex Mono',monospace;background:var(--surf);padding:1px 5px;border-radius:3px;font-size:.82em}}

/* NAV */
.rnav{{
  position:sticky;top:0;z-index:100;
  background:#0a0d16dd;backdrop-filter:blur(10px);
  border-bottom:1px solid var(--b1);
  display:flex;align-items:center;gap:0;padding:0 1.5rem;
  overflow-x:auto;scrollbar-width:none;
}}
.rnav::-webkit-scrollbar{{display:none}}
	.rbrand{{font-weight:800;font-size:.95rem;color:#fff;margin-right:1.2rem;flex-shrink:0;letter-spacing:.05em}}
	.rbrand em{{color:var(--cyan);font-style:normal}}
	.rnav a{{
  color:var(--muted);font-size:.68rem;text-transform:uppercase;letter-spacing:.08em;
  padding:.72rem .8rem;border-bottom:2px solid transparent;
  white-space:nowrap;text-decoration:none;transition:color .15s,border-color .15s;
}}
.rnav a:hover,.rnav a.active{{color:#fff;border-bottom-color:var(--cyan)}}

/* WRAP */
.wrap{{max-width:1280px;margin:0 auto;padding:2rem 1.5rem 5rem}}
section{{margin-bottom:2.5rem;scroll-margin-top:56px}}

/* HEADINGS */
h2{{
  display:flex;align-items:center;gap:.5rem;
  font-size:1.05rem;font-weight:700;color:#fff;
  border-bottom:1px solid var(--b1);padding-bottom:.4rem;margin-bottom:1rem;
}}
h2 .sub{{font-size:.65rem;color:var(--muted);font-weight:400;margin-left:.4rem}}
.si{{font-size:1.1rem}}

/* KPI */
.kpi-row{{display:flex;gap:.7rem;flex-wrap:wrap;margin:.6rem 0}}
.kpi{{background:var(--card);border:1px solid var(--b1);border-radius:10px;padding:.8rem 1.2rem;min-width:120px;flex:1}}
.kv{{font-size:1.75rem;font-weight:800;color:#fff;line-height:1.1;font-family:'IBM Plex Mono',monospace}}
.kk{{font-size:.6rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-top:.2rem}}

/* EXEC BOX */
	.exec-box{{
	  background:var(--card);border:1px solid var(--b2);border-radius:10px;
	  padding:1.2rem 1.5rem;font-size:.88rem;line-height:1.85;color:var(--text);
	}}

	/* AI HANDOFF */
	.brief-shell{{background:var(--card);border:1px solid var(--b2);border-radius:12px;padding:1.1rem 1.2rem}}
	.brief-head{{display:flex;justify-content:space-between;gap:1rem;align-items:flex-start;margin-bottom:1rem}}
	.brief-title{{font-size:1.05rem;color:#fff;font-weight:700}}
	.brief-summary{{margin-top:.35rem;color:#8a9bb5;font-size:.82rem;line-height:1.7}}
	.brief-badge{{border:1px solid;padding:.32rem .7rem;border-radius:999px;text-transform:uppercase;font-size:.62rem;letter-spacing:.08em;font-weight:700;white-space:nowrap}}
	.brief-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:.7rem}}
	.brief-item{{background:var(--surf);border:1px solid var(--b1);border-radius:10px;padding:.8rem .9rem}}
	.brief-route{{display:flex;justify-content:space-between;gap:.5rem;align-items:center;margin-bottom:.45rem}}
	.brief-route code{{color:var(--blue)}}
	.brief-sev{{font-size:.58rem;text-transform:uppercase;letter-spacing:.08em;font-weight:700}}
	.brief-problem{{font-size:.8rem;color:#fff;font-weight:600;line-height:1.5}}
	.brief-evidence{{font-size:.72rem;color:#8a9bb5;line-height:1.55;margin-top:.3rem}}
	.brief-fix{{font-size:.72rem;color:var(--text);line-height:1.55;margin-top:.45rem}}
	.brief-foot{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem;margin-top:1rem}}
	.brief-sub{{font-size:.62rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:.35rem}}
	.brief-foot ul{{padding-left:1rem;color:#c2cfe0;font-size:.74rem;line-height:1.65}}

	/* CHARTS */
	.chart-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:.8rem}}
	.chart-card{{background:var(--card);border:1px solid var(--b1);border-radius:10px;padding:.9rem 1rem}}
	.chart-title{{font-size:.82rem;color:#fff;font-weight:700}}
	.chart-sub{{font-size:.66rem;color:var(--muted);margin-top:.18rem;margin-bottom:.6rem}}
	.trend-svg{{width:100%;height:auto;display:block}}
	.trend-svg text{{fill:#3a4d6a;font-size:9px;font-family:'IBM Plex Mono',monospace}}
	.bar-stack,.depth-stack{{display:flex;flex-direction:column;gap:.55rem}}
	.bar-row,.depth-row{{display:grid;grid-template-columns:minmax(180px,1.5fr) minmax(160px,2fr) minmax(120px,1fr);gap:.7rem;align-items:center}}
	.bar-label code,.depth-route code{{display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
	.bar-track,.mini-track{{height:10px;background:var(--surf);border:1px solid var(--b1);border-radius:999px;overflow:hidden}}
	.bar-fill,.mini-fill{{display:block;height:100%;border-radius:999px}}
		.bar-meta,.depth-meta{{font-size:.66rem;color:#8a9bb5}}
		.mini-fill-state{{background:#4a8cff}}
		.mini-fill-action{{background:#00d4aa}}
		.sg-sub{{font-size:.58rem;color:#3a4d6a;margin-top:.18rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
		.path-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:.7rem}}
		.path-card{{background:var(--surf);border:1px solid var(--b1);border-radius:10px;padding:.8rem .9rem}}
		.path-route{{display:flex;justify-content:space-between;gap:.6rem;align-items:center;margin-bottom:.45rem}}
		.path-badge{{border:1px solid;padding:.18rem .42rem;border-radius:999px;font-size:.56rem;text-transform:uppercase;letter-spacing:.08em;font-weight:700}}
		.path-main{{display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;color:#fff;font-size:.78rem;line-height:1.5}}
		.path-state{{background:#101728;border:1px solid var(--b1);border-radius:999px;padding:.2rem .5rem;color:#c2cfe0}}
		.path-action{{color:#4a8cff;font-weight:700}}
		.path-arrow{{color:#3a4d6a}}
		.path-meta{{font-size:.64rem;color:#8a9bb5;margin-top:.5rem}}
		.path-sub{{font-size:.62rem;color:#3a4d6a;margin-top:.25rem}}
		
		/* FOCUS AREAS */
	.fa-card{{background:var(--card);border:1px solid var(--b1);border-radius:10px;margin-bottom:.55rem;overflow:hidden;transition:border-color .15s}}
.fa-card:hover{{border-color:var(--b2)}}
.fa-hd{{display:flex;align-items:center;flex-wrap:wrap;gap:.5rem;padding:.7rem 1rem;cursor:pointer;user-select:none}}
.fa-rank{{font-size:.62rem;color:var(--muted);font-weight:700;min-width:22px;font-family:'IBM Plex Mono',monospace}}
.fa-route{{font-size:.76rem;color:var(--blue);font-family:'IBM Plex Mono',monospace}}
.fa-prio{{font-size:.62rem}}
.fa-badge{{font-size:.6rem;border-radius:4px;padding:2px 6px;font-weight:600}}
.fa-bug{{background:#ff4a6e22;color:#ff4a6e}}
.fa-broken{{background:#ff8c4222;color:#ff8c42}}
.fa-api{{background:#9b6dff22;color:#9b6dff}}
.fa-perf{{background:#f5c84222;color:#f5c842}}
.fa-vis{{background:#00d4aa22;color:#00d4aa}}
.fa-score{{font-size:.85rem;font-weight:700;font-family:'IBM Plex Mono',monospace}}
.fa-lbl{{font-size:.62rem;color:var(--muted)}}
.fa-eff{{font-size:.6rem;background:var(--surf);border:1px solid var(--b1);border-radius:4px;padding:2px 8px;color:var(--muted)}}
.fa-chev{{font-size:.6rem;color:var(--muted);transition:transform .2s;margin-left:.2rem}}
.fa-chev.open{{transform:rotate(90deg)}}
.fa-bd{{border-top:1px solid var(--b1);padding:.9rem 1rem 1.1rem}}
.fa-cols{{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem}}
@media(max-width:680px){{.fa-cols{{grid-template-columns:1fr}}}}
.fa-dt{{font-size:.6rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:600;margin-bottom:.35rem}}
.fa-recs{{padding-left:1.1rem;font-size:.78rem;color:var(--text)}}
.fa-recs li{{margin-bottom:.28rem;line-height:1.55}}
.fa-dg{{margin-bottom:.55rem}}
.fa-di{{font-size:.72rem;color:#8a9bb5;padding:.08rem 0;word-break:break-word;line-height:1.5}}

/* ROUTE TREE */
.tree-box{{
  background:var(--surf);border:1px solid var(--b1);border-radius:10px;
  padding:.8rem 1rem;max-height:58vh;overflow-y:auto;
  font-family:'IBM Plex Mono',monospace;
}}
.tree-box::-webkit-scrollbar{{width:3px}}
.tree-box::-webkit-scrollbar-thumb{{background:var(--b2);border-radius:2px}}

/* TABLE */
.tbl-wrap{{border-radius:9px;border:1px solid var(--b1);overflow:hidden;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:.72rem}}
thead th{{
  background:var(--surf);color:var(--muted);text-transform:uppercase;
  font-size:.58rem;letter-spacing:.07em;padding:.5rem .7rem;
  text-align:left;white-space:nowrap;position:sticky;top:0;
}}
td{{padding:.38rem .7rem;border-bottom:1px solid var(--b1);vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
tbody tr:hover td{{background:#0a0d1666}}

/* MISC */
.empty-note{{color:var(--muted);font-size:.8rem;padding:.5rem 0}}
</style>
</head>
<body>
	<nav class="rnav">
	  <span class="rbrand">Story<em>vord</em> QA</span>
		  <a href="#summary">Summary</a>
		  <a href="#handoff">AI Handoff</a>
		  <a href="#graphs">Graphs</a>
		  <a href="#state-graph">State Graph</a>
		  <a href="#focus">Focus Areas</a>
		  <a href="#tree">Route Tree</a>
		  <a href="#pages">Pages</a>
		  <a href="#elements">Elements</a>
		  <a href="#forms">Forms</a>
		  <a href="#state-transitions">Transitions</a>
		  <a href="#apis">APIs</a>
	  <a href="#interactions">Interactions</a>
	  <a href="#workflows">Workflows</a>
	</nav>

<div class="wrap">

  <!-- HEADER -->
  <div style="margin:.5rem 0 1.5rem">
    <div style="font-size:1.5rem;font-weight:800;color:#fff;margin-bottom:.2rem">🔬 QA Report</div>
    <div style="font-size:.7rem;color:var(--muted)">{now} UTC &nbsp;·&nbsp; {len(pages)} pages &nbsp;·&nbsp; {unique_routes} routes</div>
  </div>

  <!-- KPIs -->
  <div class="kpi-row">
    <div class="kpi"><div class="kv" style="color:{_sc(avg_score)}">{avg_score}</div><div class="kk">Avg score</div></div>
    <div class="kpi"><div class="kv">{len(pages)}</div><div class="kk">Pages crawled</div></div>
    <div class="kpi"><div class="kv" style="color:var(--red)">{len(all_bugs)}</div><div class="kk">Bugs</div></div>
    <div class="kpi"><div class="kv" style="color:var(--orange)">{total_broken}</div><div class="kk">Broken interactions</div></div>
    <div class="kpi"><div class="kv" style="color:var(--purple)">{total_api_fail}</div><div class="kk">API failures</div></div>
    <div class="kpi"><div class="kv" style="color:var(--yellow)">{len(slow_pages)}</div><div class="kk">Slow pages (&gt;3s)</div></div>
    <div class="kpi"><div class="kv" style="color:var(--red)">{critical_n}</div><div class="kk">Critical routes</div></div>
    <div class="kpi"><div class="kv" style="color:var(--yellow)">{warning_n}</div><div class="kk">Routes needing work</div></div>
  </div>
  {cov_html}

  <!-- SUMMARY -->
	  <section id="summary">
	    <h2><span class="si">📋</span>Executive Summary</h2>
	    <div class="exec-box">{exec_para}</div>
	  </section>

	  <section id="handoff">
	    <h2><span class="si">🧠</span>AI Handoff <span class="sub">— developer-facing problem, evidence, and likely fix</span></h2>
	    {brief_html}
	  </section>

	  <section id="graphs">
	    <h2><span class="si">📈</span>Evidence Graphs <span class="sub">— trend, route risk, and exploration depth</span></h2>
	    {history_graphs_html}
	    <div class="chart-grid" style="margin-top:.8rem">
	      <div class="chart-card">
	        <div class="chart-title">Highest-Risk Routes</div>
	        <div class="chart-sub">Routes with the most concentrated issues in this run.</div>
	        <div class="bar-stack">{route_risk_html}</div>
	      </div>
	      <div class="chart-card">
	        <div class="chart-title">Route Regressions</div>
	        <div class="chart-sub">Only routes that worsened compared with the previous run.</div>
	        <div class="bar-stack">{regression_html}</div>
	      </div>
	      <div class="chart-card">
	        <div class="chart-title">Exploration Depth</div>
	        <div class="chart-sub">Where the crawler actually spent time: states and actions per route.</div>
	        <div class="depth-stack">{exploration_html}</div>
	      </div>
	    </div>
		  </section>

		  <section id="state-graph">
		    <h2><span class="si">Graph</span>Route-State Graph Evidence <span class="sub">- how the crawler moved through routes, states, and actions</span></h2>
		    <div class="kpi-row">
		      <div class="kpi"><div class="kv" style="color:#4a8cff">{graph_summary.get('routes', 0)}</div><div class="kk">Routes in graph</div></div>
		      <div class="kpi"><div class="kv" style="color:#00d4aa">{graph_summary.get('states', 0)}</div><div class="kk">Unique states</div></div>
		      <div class="kpi"><div class="kv" style="color:#ff8c42">{graph_summary.get('transitions', 0)}</div><div class="kk">State transitions</div></div>
		      <div class="kpi"><div class="kv" style="color:#f5c842">{graph_summary.get('deepest_state', 0)}</div><div class="kk">Deepest state depth</div></div>
		      <div class="kpi"><div class="kv" style="color:#9b6dff">{graph_summary.get('modal_states', 0)}</div><div class="kk">Modal states</div></div>
		      <div class="kpi"><div class="kv" style="color:#22d37a">{graph_summary.get('form_touches', 0)}</div><div class="kk">Form touches</div></div>
		    </div>
		    <div class="chart-grid" style="margin-top:.8rem">
		      <div class="chart-card">
		        <div class="chart-title">Most Connected States</div>
		        <div class="chart-sub">States with the most incoming and outgoing transitions.</div>
		        <div class="bar-stack">{state_hotspots_html}</div>
		      </div>
		      <div class="chart-card">
		        <div class="chart-title">Representative Paths</div>
		        <div class="chart-sub">Top state transitions with action, outcome, and evidence.</div>
		        <div class="path-grid">{state_paths_html}</div>
		      </div>
		    </div>
		  </section>

		  <!-- FOCUS AREAS -->
	  <section id="focus">
	    <h2><span class="si">🎯</span>Dev Focus Areas <span class="sub">— sorted worst-first · click to expand</span></h2>
    <p style="font-size:.75rem;color:var(--muted);margin-bottom:.8rem">
      Each card tells you exactly what is broken, why it matters, and how long it should take to fix.
    </p>
    {focus_html}
  </section>

  <!-- ROUTE TREE -->
  <section id="tree">
    <h2><span class="si">🌿</span>Route Tree <span class="sub">— branch health at a glance</span></h2>
    <div class="tree-box">{tree_html or '<span style="color:#3a4d6a">No routes yet.</span>'}</div>
  </section>

  <!-- PAGES -->
	  {table_section("All Pages","📄","pages",
	    ["Preview","URL","Score","HTTP","Load","Bugs","Broken","Summary"],
	    page_rows,"No pages crawled.")}

	  {table_section("Broken Elements","🧩","elements",
	    ["Route","Action","Scope","State","Outcome","Evidence","Target"],
	    broken_rows_html,"No broken element evidence captured.")}

	  {table_section("Form Evidence","ðŸ§¾","forms",
	    ["Route","Form / Scope","Field","Intent","Submit","Validation","Outcome","Target"],
	    form_rows_html,"No form interactions were captured.")}

	  {table_section("State Transitions","ðŸ”—","state-transitions",
	    ["From Route","From State","Action","Outcome","To Route","To State","Count","Evidence"],
	    state_transition_rows,"No route-state transitions were captured.")}

	  <!-- APIS -->
	  {table_section("API Telemetry","🔌","apis",
    ["Method","Endpoint","Calls","Failures","Avg Latency"],
    api_rows,"No API calls recorded.")}

  <!-- INTERACTIONS -->
  {table_section("Interaction Reliability","🖱","interactions",
    ["Route","Action","Attempts","Success","Neutral","Fail","Reliability","Flakiness"],
    int_rows,"No interaction data.")}

  <!-- WORKFLOWS -->
  {table_section("Workflow Scenarios","⚙️","workflows",
    ["Scenario","Route","Status","Duration","Steps","Error"],
    wf_rows,"No workflow scenarios ran.")}

</div>

<script>
const secs = document.querySelectorAll('section[id]');
const links = document.querySelectorAll('.rnav a');
const obs = new IntersectionObserver(es=>{{
  es.forEach(e=>{{
    if(e.isIntersecting)
      links.forEach(l=>l.classList.toggle('active', l.getAttribute('href')==='#'+e.target.id));
  }});
}},{{rootMargin:'-25% 0px -65% 0px'}});
secs.forEach(s=>obs.observe(s));

function toggleFa(i){{
  const bd=document.getElementById('fab'+i), ch=document.getElementById('fac'+i);
  const open=bd.style.display==='none';
  bd.style.display=open?'':'none';
  ch.classList.toggle('open',open);
}}

// Auto-open the worst focus area on load
document.addEventListener('DOMContentLoaded',()=>{{
  const first=document.querySelector('.fa-card');
  if(first){{
    const id=first.id?.replace('fa-card-','');
    toggleFa(0);
  }}
}});
</script>
</body>
</html>"""

    with open(settings.report_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[REPORT] → {settings.report_file}  ({len(html)//1024}KB)")
