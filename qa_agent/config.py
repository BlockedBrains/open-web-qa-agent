from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


def _slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _derive_site_id(base_url: str) -> str:
    raw = (base_url or "").strip()
    if not raw:
        return "default-site"
    try:
        parsed = urlparse(raw)
        host = parsed.netloc or parsed.path or raw
        path = (parsed.path or "").strip("/")
        parts = [host]
        if path:
            parts.append(path.replace("/", "-"))
        site_id = _slugify("-".join(parts))
        return site_id or "default-site"
    except Exception:
        site_id = _slugify(raw)
        return site_id or "default-site"


def _load_json_file(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _as_relpath(path: str) -> str:
    return str(path or "").replace("\\", "/")


@dataclass
class Settings:
    base_url: str = "https://example.com"
    email: str = ""
    password: str = ""
    site_id: str = ""
    site_name: str = ""
    sites_dir: str = "sites"
    site_dir: str = ""
    site_config_file: str = ""
    start_path: str = "/dashboard"

    # Crawl limits
    max_pages: int = 300
    crawl_public: bool = True
    public_max_pages: int = 40
    parallel_limit: int = 10
    retry_threshold: float = 4.0
    retry_delay_seconds: float = 2.0
    strict_route_dedupe: bool = True
    page_state_depth: int = 2
    max_state_actions: int = 18
    max_page_states: int = 14
    max_form_fields: int = 10
    discovery_seed_routes: list[str] = field(default_factory=lambda: [
        "/",
        "/dashboard",
        "/project-details",
    ])
    public_routes: list[str] = field(default_factory=lambda: [
        "/",
        "/auth/sign-in",
        "/auth/sign-up",
        "/auth/forgot-password",
        "/forgot-password",
    ])

    # Auth
    auth_login_url: str = ""
    auth_login_paths: list[str] = field(default_factory=lambda: [
        "/auth/sign-in",
        "/login",
        "/sign-in",
        "/signin",
    ])
    auth_email_selectors: list[str] = field(default_factory=lambda: [
        'input[type="email"]',
        'input[name="email"]',
        'input[id*="email" i]',
        'input[autocomplete="username"]',
        'input[placeholder*="email" i]',
    ])
    auth_password_selectors: list[str] = field(default_factory=lambda: [
        'input[type="password"]',
        'input[name="password"]',
        'input[id*="password" i]',
        'input[autocomplete="current-password"]',
    ])
    auth_submit_selectors: list[str] = field(default_factory=lambda: [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Login")',
        'button:has-text("Log in")',
        'button:has-text("Sign in")',
        'button:has-text("Continue")',
    ])
    auth_success_paths: list[str] = field(default_factory=lambda: [
        "/dashboard",
        "/project",
    ])
    auth_blocking_paths: list[str] = field(default_factory=lambda: [
        "/auth/",
        "/login",
        "/signin",
        "/sign-in",
        "/verify",
        "/otp",
        "/forgot-password",
        "/reset-password",
    ])

    # Paths to skip
    skip_paths: list[str] = field(default_factory=lambda: [
        "/logout", "/signout", "/auth/logout", "/api/",
        ".pdf", ".zip", ".png", ".jpg", ".svg",
    ])

    # Interaction selectors
    interaction_selectors: list[str] = field(default_factory=lambda: [
        "button", '[role="button"]', 'a[href="#"]',
        '[data-testid*="btn"]', '[data-testid*="button"]',
        '[data-testid*="action"]',
    ])
    action_keywords: list[str] = field(default_factory=lambda: [
        "create", "add", "new", "save", "submit", "delete", "edit",
        "update", "upload", "invite", "open", "start", "launch",
        "publish", "share", "export", "import",
    ])
    explorable_selectors: list[str] = field(default_factory=lambda: [
        "a[href]", "[data-href]", "[data-link]",
    ])

    # Ports
    http_port: int = 8766
    ws_port: int = 8765

    # Files
    session_file: str = ""
    state_file: str = ""
    log_file: str = ""
    report_file: str = ""
    history_file: str = ""
    screenshot_dir: str = ""
    workflows_file: str = ""
    knowledge_file: str = ""
    sidecar_file: str = "qa_data.js"

    # LLM
    llm_provider: str = "ollama"
    llm_url: str = "http://localhost:11434/v1/chat/completions"
    llm_model: str = "qwen2.5:14b"
    llm_analysis_model: str = ""
    llm_report_model: str = ""
    llm_api_key: str = ""
    llm_report_enabled: bool = True
    llm_preflight: bool = True
    llm_fail_fast: bool = False
    llm_preflight_ok: bool | None = None
    llm_last_error: str = ""
    ollama_url: str = "http://localhost:11434/v1/chat/completions"
    ollama_model: str = "qwen2.5:14b"
    llm_timeout_seconds: int = 90
    llm_debug: bool = False
    llm_debug_file: str = "llm_debug.log"

    # Alerts
    slack_webhook: str = ""

    # Scenarios
    run_scenarios: bool = True
    scenario_timeout_seconds: int = 30

    # LangGraph
    use_langgraph: bool = False

    @property
    def artifact_paths(self) -> dict[str, str]:
        return {
            "site_config": _as_relpath(self.site_config_file),
            "session": _as_relpath(self.session_file),
            "state": _as_relpath(self.state_file),
            "log": _as_relpath(self.log_file),
            "report": _as_relpath(self.report_file),
            "history": _as_relpath(self.history_file),
            "knowledge": _as_relpath(self.knowledge_file),
            "workflows": _as_relpath(self.workflows_file),
            "screenshots": _as_relpath(self.screenshot_dir),
            "sidecar": _as_relpath(self.sidecar_file),
        }

    @property
    def workspace_info(self) -> dict:
        return {
            "site_id": self.site_id,
            "site_name": self.site_name or self.site_id,
            "base_url": self.base_url,
            "site_dir": _as_relpath(self.site_dir),
            "site_config_file": _as_relpath(self.site_config_file),
            "commands": {
                "crawl": f"python agent.py --site {self.site_id}",
                "resume": f"python agent.py --site {self.site_id} --resume",
                "serve": f"python serve.py --site {self.site_id}",
                "record_workflow": f"python agent.py --site {self.site_id} --record-workflow",
                "record_guided_tour": f"python agent.py --site {self.site_id} --record-guided-tour",
            },
            "artifacts": self.artifact_paths,
        }

    def persist_site_config(self) -> None:
        os.makedirs(self.site_dir, exist_ok=True)
        payload = {
            "site_id": self.site_id,
            "site_name": self.site_name or self.site_id,
            "base_url": self.base_url,
            "paths": self.artifact_paths,
            "crawl": {
                "max_pages": self.max_pages,
                "start_path": self.start_path,
                "public_crawl": self.crawl_public,
                "public_max_pages": self.public_max_pages,
                "parallel_limit": self.parallel_limit,
                "retry_threshold": self.retry_threshold,
                "strict_route_dedupe": self.strict_route_dedupe,
                "page_state_depth": self.page_state_depth,
                "max_state_actions": self.max_state_actions,
                "max_page_states": self.max_page_states,
                "max_form_fields": self.max_form_fields,
            },
            "routes": {
                "discovery_seed_routes": self.discovery_seed_routes,
                "public_routes": self.public_routes,
            },
            "scenarios": {
                "enabled": self.run_scenarios,
                "timeout_seconds": self.scenario_timeout_seconds,
            },
            "llm": {
                "provider": self.llm_provider,
                "model": self.llm_model,
                "analysis_model": self.llm_analysis_model or self.llm_model,
                "report_model": self.llm_report_model or self.llm_model,
                "report_enabled": self.llm_report_enabled,
            },
            "auth": {
                "login_url": self.auth_login_url,
                "login_paths": self.auth_login_paths,
                "email_selectors": self.auth_email_selectors,
                "password_selectors": self.auth_password_selectors,
                "submit_selectors": self.auth_submit_selectors,
                "success_paths": self.auth_success_paths,
                "blocking_paths": self.auth_blocking_paths,
                "password_saved": False,
                "has_credentials": bool(self.email and self.password),
            },
            "workspace": self.workspace_info,
        }
        with open(self.site_config_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()

        s.sites_dir = os.getenv("QA_SITES_DIR", s.sites_dir).strip() or "sites"
        env_site_id = _slugify(os.getenv("QA_SITE_ID", ""))
        env_site_config = os.getenv("QA_SITE_CONFIG_FILE", "").strip()

        site_config = {}
        if env_site_config:
            site_config = _load_json_file(env_site_config)
        elif env_site_id:
            site_config = _load_json_file(os.path.join(s.sites_dir, env_site_id, "site.json"))

        seeded_base_url = os.getenv("QA_BASE_URL", str(site_config.get("base_url", s.base_url))).strip().rstrip("/")
        s.base_url = seeded_base_url or s.base_url
        s.site_id = env_site_id or _slugify(str(site_config.get("site_id", ""))) or _derive_site_id(s.base_url)
        s.site_name = os.getenv(
            "QA_SITE_NAME",
            str(site_config.get("site_name") or site_config.get("name") or s.site_id),
        ).strip() or s.site_id
        s.site_dir = os.path.join(s.sites_dir, s.site_id)
        s.site_config_file = env_site_config or os.path.join(s.site_dir, "site.json")

        if not site_config:
            site_config = _load_json_file(s.site_config_file)
            if not os.getenv("QA_BASE_URL"):
                s.base_url = str(site_config.get("base_url", s.base_url)).strip().rstrip("/") or s.base_url
            if not os.getenv("QA_SITE_NAME"):
                s.site_name = str(site_config.get("site_name") or site_config.get("name") or s.site_name).strip() or s.site_name

        crawl_cfg = site_config.get("crawl", {}) if isinstance(site_config.get("crawl"), dict) else {}
        routes_cfg = site_config.get("routes", {}) if isinstance(site_config.get("routes"), dict) else {}
        auth_cfg = site_config.get("auth", {}) if isinstance(site_config.get("auth"), dict) else {}

        s.email = os.getenv("QA_EMAIL", str(auth_cfg.get("email", s.email))).strip()
        s.password = os.getenv("QA_PASSWORD", s.password).strip()

        s.max_pages = int(os.getenv("QA_MAX_PAGES", str(crawl_cfg.get("max_pages", s.max_pages))))
        s.start_path = os.getenv("QA_START_PATH", str(crawl_cfg.get("start_path", s.start_path))).strip() or s.start_path
        s.crawl_public = os.getenv(
            "QA_PUBLIC_CRAWL",
            str(crawl_cfg.get("public_crawl", "1")),
        ).lower() not in ("0", "false", "no")
        s.public_max_pages = int(os.getenv("QA_PUBLIC_MAX_PAGES", str(crawl_cfg.get("public_max_pages", s.public_max_pages))))
        s.parallel_limit = int(os.getenv("QA_PARALLEL", str(crawl_cfg.get("parallel_limit", s.parallel_limit))))
        s.retry_threshold = float(os.getenv("QA_RETRY_THRESHOLD", str(crawl_cfg.get("retry_threshold", s.retry_threshold))))
        s.strict_route_dedupe = os.getenv(
            "QA_STRICT_ROUTE_DEDUPE",
            str(crawl_cfg.get("strict_route_dedupe", "1")),
        ).lower() not in ("0", "false", "no")
        s.page_state_depth = int(os.getenv("QA_PAGE_STATE_DEPTH", str(crawl_cfg.get("page_state_depth", s.page_state_depth))))
        s.max_state_actions = int(os.getenv("QA_MAX_STATE_ACTIONS", str(crawl_cfg.get("max_state_actions", s.max_state_actions))))
        s.max_page_states = int(os.getenv("QA_MAX_PAGE_STATES", str(crawl_cfg.get("max_page_states", s.max_page_states))))
        s.max_form_fields = int(os.getenv("QA_MAX_FORM_FIELDS", str(crawl_cfg.get("max_form_fields", s.max_form_fields))))

        s.llm_provider = os.getenv("QA_LLM_PROVIDER", s.llm_provider).strip()
        s.llm_url = os.getenv("QA_LLM_URL", os.getenv("OLLAMA_URL", s.llm_url)).strip()
        s.llm_model = os.getenv("QA_LLM_MODEL", os.getenv("OLLAMA_MODEL", s.llm_model)).strip()
        s.llm_analysis_model = os.getenv("QA_LLM_ANALYSIS_MODEL", s.llm_analysis_model or s.llm_model).strip()
        s.llm_report_model = os.getenv("QA_LLM_REPORT_MODEL", s.llm_report_model or s.llm_model).strip()
        s.llm_api_key = os.getenv("QA_LLM_API_KEY", os.getenv("OPENAI_API_KEY", s.llm_api_key)).strip()
        s.llm_report_enabled = os.getenv("QA_LLM_REPORTING", "1").lower() not in ("0", "false", "no")
        s.llm_preflight = os.getenv("QA_LLM_PREFLIGHT", "1").lower() not in ("0", "false", "no")
        s.llm_fail_fast = os.getenv("QA_LLM_FAIL_FAST", "0").lower() in ("1", "true", "yes")
        s.ollama_url = s.llm_url
        s.ollama_model = s.llm_model
        s.llm_debug = os.getenv("QA_LLM_DEBUG", "").lower() in ("1", "true", "yes")

        s.slack_webhook = os.getenv("QA_SLACK_WEBHOOK", "")
        s.run_scenarios = os.getenv("QA_SCENARIOS", "1").lower() not in ("0", "false", "no")
        s.use_langgraph = os.getenv("QA_LANGGRAPH", "").lower() in ("1", "true", "yes")

        raw_seed_routes = os.getenv("QA_DISCOVERY_SEED_ROUTES", "")
        if raw_seed_routes.strip():
            s.discovery_seed_routes = _split_csv(raw_seed_routes)
        elif routes_cfg.get("discovery_seed_routes"):
            s.discovery_seed_routes = _split_csv(",".join(routes_cfg.get("discovery_seed_routes", [])))

        raw_public_routes = os.getenv("QA_PUBLIC_ROUTES", "")
        if raw_public_routes.strip():
            s.public_routes = _split_csv(raw_public_routes)
        elif routes_cfg.get("public_routes"):
            s.public_routes = _split_csv(",".join(routes_cfg.get("public_routes", [])))

        s.auth_login_url = os.getenv("QA_LOGIN_URL", str(auth_cfg.get("login_url", s.auth_login_url))).strip()

        raw_login_paths = os.getenv("QA_LOGIN_PATHS", "")
        if raw_login_paths.strip():
            s.auth_login_paths = _split_csv(raw_login_paths)
        elif auth_cfg.get("login_paths"):
            s.auth_login_paths = _split_csv(",".join(auth_cfg.get("login_paths", [])))

        raw_email_selectors = os.getenv("QA_AUTH_EMAIL_SELECTORS", "")
        if raw_email_selectors.strip():
            s.auth_email_selectors = _split_csv(raw_email_selectors)
        elif auth_cfg.get("email_selectors"):
            s.auth_email_selectors = _split_csv(",".join(auth_cfg.get("email_selectors", [])))

        raw_password_selectors = os.getenv("QA_AUTH_PASSWORD_SELECTORS", "")
        if raw_password_selectors.strip():
            s.auth_password_selectors = _split_csv(raw_password_selectors)
        elif auth_cfg.get("password_selectors"):
            s.auth_password_selectors = _split_csv(",".join(auth_cfg.get("password_selectors", [])))

        raw_submit_selectors = os.getenv("QA_AUTH_SUBMIT_SELECTORS", "")
        if raw_submit_selectors.strip():
            s.auth_submit_selectors = _split_csv(raw_submit_selectors)
        elif auth_cfg.get("submit_selectors"):
            s.auth_submit_selectors = _split_csv(",".join(auth_cfg.get("submit_selectors", [])))

        raw_success_paths = os.getenv("QA_AUTH_SUCCESS_PATHS", "")
        if raw_success_paths.strip():
            s.auth_success_paths = _split_csv(raw_success_paths)
        elif auth_cfg.get("success_paths"):
            s.auth_success_paths = _split_csv(",".join(auth_cfg.get("success_paths", [])))

        raw_blocking_paths = os.getenv("QA_AUTH_BLOCKING_PATHS", "")
        if raw_blocking_paths.strip():
            s.auth_blocking_paths = _split_csv(raw_blocking_paths)
        elif auth_cfg.get("blocking_paths"):
            s.auth_blocking_paths = _split_csv(",".join(auth_cfg.get("blocking_paths", [])))

        s.session_file = os.getenv("QA_SESSION_FILE", os.path.join(s.site_dir, "session.json"))
        s.state_file = os.getenv("QA_STATE_FILE", os.path.join(s.site_dir, "crawl_state.json"))
        s.log_file = os.getenv("QA_LOG_FILE", os.path.join(s.site_dir, "crawl_log.json"))
        s.report_file = os.getenv("QA_REPORT_FILE", os.path.join(s.site_dir, "report.html"))
        s.history_file = os.getenv("QA_HISTORY_FILE", os.path.join(s.site_dir, "history.json"))
        s.screenshot_dir = os.getenv("QA_SCREENSHOT_DIR", os.path.join(s.site_dir, "screenshots"))
        s.workflows_file = os.getenv("QA_WORKFLOWS_FILE", os.path.join(s.site_dir, "workflows.json"))
        s.knowledge_file = os.getenv("QA_KNOWLEDGE_FILE", os.path.join(s.site_dir, "qa_knowledge.json"))
        s.llm_debug_file = os.getenv("QA_LLM_DEBUG_FILE", os.path.join(s.site_dir, "llm_debug.log"))
        s.sidecar_file = os.getenv("QA_SIDECAR_FILE", s.sidecar_file)

        os.makedirs(s.site_dir, exist_ok=True)
        os.makedirs(s.screenshot_dir, exist_ok=True)
        s.persist_site_config()
        return s
