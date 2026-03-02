from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse


def _endpoint_key(url: str) -> str:
    try:
        p = urlparse(url)
        return p.path or "/"
    except Exception:
        return url


class ApiTelemetry:
    def __init__(self) -> None:
        self._pending: dict[int, float] = {}
        self.all_calls: list[dict[str, Any]] = []

    def attach(self, page, page_calls: list[dict[str, Any]]) -> None:
        def on_request(req) -> None:
            if req.resource_type in ("xhr", "fetch"):
                self._pending[id(req)] = time.time()

        def on_response(resp) -> None:
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            started = self._pending.pop(id(req), time.time())
            entry = {
                "endpoint": _endpoint_key(resp.url),
                "url": resp.url,
                "method": req.method,
                "status": int(resp.status),
                "latency_ms": round((time.time() - started) * 1000),
                "response_size": int(resp.headers.get("content-length", "0") or 0),
                "failed": int(resp.status) >= 400,
            }
            page_calls.append(entry)
            self.all_calls.append(entry)

        def on_failed(req) -> None:
            if req.resource_type not in ("xhr", "fetch"):
                return
            entry = {
                "endpoint": _endpoint_key(req.url),
                "url": req.url,
                "method": req.method,
                "status": 0,
                "latency_ms": 0,
                "response_size": 0,
                "failed": True,
            }
            page_calls.append(entry)
            self.all_calls.append(entry)

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_failed)

    def summarize(self) -> list[dict[str, Any]]:
        buckets: dict[tuple[str, str], dict[str, Any]] = {}
        for c in self.all_calls:
            key = (c.get("endpoint", ""), c.get("method", "GET"))
            row = buckets.setdefault(
                key,
                {
                    "endpoint": key[0],
                    "method": key[1],
                    "calls": 0,
                    "failures": 0,
                    "latency_total": 0,
                    "bytes_total": 0,
                },
            )
            row["calls"] += 1
            row["failures"] += 1 if c.get("failed") else 0
            row["latency_total"] += int(c.get("latency_ms", 0))
            row["bytes_total"] += int(c.get("response_size", 0))

        out: list[dict[str, Any]] = []
        for row in buckets.values():
            calls = max(row["calls"], 1)
            out.append(
                {
                    "endpoint": row["endpoint"],
                    "method": row["method"],
                    "calls": row["calls"],
                    "failures": row["failures"],
                    "error_rate": round(row["failures"] / calls, 3),
                    "avg_latency_ms": round(row["latency_total"] / calls),
                    "avg_response_size": round(row["bytes_total"] / calls),
                }
            )
        out.sort(key=lambda r: (-r["failures"], -r["calls"], r["endpoint"]))
        return out


MUTATION_OBSERVER_SCRIPT = """() => {
  if (window.__qa_mutation_ready__) return;
  window.__qa_mutation_ready__ = true;
  window.__qa_mutations__ = {total:0, adds:0, removes:0, attrs:0, bySecond:{}};
  const obs = new MutationObserver((records) => {
    const now = Math.floor(performance.now() / 1000).toString();
    if (!window.__qa_mutations__.bySecond[now]) window.__qa_mutations__.bySecond[now] = 0;
    window.__qa_mutations__.bySecond[now] += records.length;
    for (const r of records) {
      window.__qa_mutations__.total += 1;
      if (r.type === 'attributes') window.__qa_mutations__.attrs += 1;
      if (r.type === 'childList') {
        window.__qa_mutations__.adds += r.addedNodes ? r.addedNodes.length : 0;
        window.__qa_mutations__.removes += r.removedNodes ? r.removedNodes.length : 0;
      }
    }
  });
  obs.observe(document.documentElement || document.body, {
    childList: true, subtree: true, attributes: true
  });
}"""


async def enable_dom_mutation_watcher(page) -> None:
    try:
        await page.evaluate(MUTATION_OBSERVER_SCRIPT)
    except Exception:
        pass


async def read_dom_mutations(page) -> dict[str, Any]:
    try:
        raw = await page.evaluate("""() => {
          const m = window.__qa_mutations__ || {total:0, adds:0, removes:0, attrs:0, bySecond:{}};
          let maxPerSecond = 0;
          for (const k in m.bySecond) if (m.bySecond[k] > maxPerSecond) maxPerSecond = m.bySecond[k];
          return {...m, max_per_second: maxPerSecond};
        }""")
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


async def collect_perf_metrics(page) -> dict[str, Any]:
    try:
        return await page.evaluate(
            """() => {
              const nav = performance.getEntriesByType('navigation')[0] || {};
              const paints = {};
              performance.getEntriesByType('paint').forEach(p => paints[p.name] = Math.round(p.startTime));
              const lcpEntries = performance.getEntriesByType('largest-contentful-paint');
              const clsEntries = performance.getEntriesByType('layout-shift');
              let cls = 0;
              for (const e of clsEntries) cls += e.hadRecentInput ? 0 : (e.value || 0);
              const tti = Math.round(Math.max(nav.domInteractive || 0, paints['first-contentful-paint'] || 0));
              return {
                fcp: paints['first-contentful-paint'] || 0,
                lcp: lcpEntries.length ? Math.round(lcpEntries[lcpEntries.length - 1].startTime) : 0,
                cls: Math.round(cls * 1000) / 1000,
                dom_content_loaded: Math.round(nav.domContentLoadedEventEnd || 0),
                dom_interactive: Math.round(nav.domInteractive || 0),
                tti_approx: tti,
                ttfb: Math.round((nav.responseStart || 0) - (nav.requestStart || 0)),
              };
            }"""
        )
    except Exception:
        return {}
