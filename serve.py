"""
serve.py - Standalone dashboard server.

Run this independently of agent.py to keep the dashboard accessible
even after the agent has stopped.

Usage:
    python serve.py
    python serve.py --site my-site
    python serve.py --port 8766 --host 0.0.0.0
"""
from __future__ import annotations

import argparse
import json
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler

from qa_agent.config import Settings


class DashboardHandler(SimpleHTTPRequestHandler):
    settings: Settings | None = None

    def do_GET(self):
        settings = self.settings
        if self.path in ("/api/state", "/api/state/"):
            self._serve_json(settings.state_file if settings else "crawl_state.json")
            return
        if self.path in ("/api/history", "/api/history/"):
            self._serve_json(settings.history_file if settings else "history.json")
            return
        if self.path in ("/api/knowledge", "/api/knowledge/"):
            self._serve_json(settings.knowledge_file if settings else "qa_knowledge.json")
            return
        if self.path in ("/api/workspace", "/api/workspace/"):
            payload = json.dumps(settings.workspace_info if settings else {}, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def _serve_json(self, filename: str):
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        if not os.path.exists(filepath):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')
            return
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(exc)}).encode())

    def log_message(self, fmt, *args):
        path = args[0] if args else ""
        if any(ext in str(path) for ext in (".js", ".css", ".png", ".ico", ".woff")):
            return
        print(f"  {self.address_string()}  {fmt % args}")

    def log_error(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="Open Web QA Dashboard server")
    parser.add_argument("--port", type=int, default=8766, help="HTTP port (default: 8766)")
    parser.add_argument("--host", default="localhost", help="Bind host (default: localhost)")
    parser.add_argument("--site", default="", help="Optional site profile id under sites/<site_id>")
    args = parser.parse_args()

    if args.site:
        os.environ["QA_SITE_ID"] = args.site

    settings = Settings.from_env()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    DashboardHandler.settings = settings

    state_exists = os.path.exists(settings.state_file)
    history_exists = os.path.exists(settings.history_file)
    report_exists = os.path.exists(settings.report_file)

    print(f"\n{'='*50}")
    print("  Open Web QA - Dashboard Server")
    print(f"{'='*50}")
    print(f"  Site     : {settings.site_name} ({settings.site_id})")
    print(f"  URL      : http://{args.host}:{args.port}/dashboard.html")
    print(f"  State    : {'ok' if state_exists else 'missing'}  {settings.state_file}")
    print(f"  History  : {'ok' if history_exists else 'missing'}  {settings.history_file}")
    print(f"  Report   : {'ok' if report_exists else 'missing'}  {settings.report_file}")
    print(f"  Workspace: http://{args.host}:{args.port}/api/workspace")
    print(f"{'='*50}")
    print("  Press Ctrl+C to stop\n")

    server = HTTPServer((args.host, args.port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")


if __name__ == "__main__":
    main()
