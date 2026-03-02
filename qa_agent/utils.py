from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse


def same_origin(url: str, base_url: str) -> bool:
    try:
        return urlparse(url).netloc == urlparse(base_url).netloc
    except Exception:
        return False


def clean_url(url: str) -> str:
    try:
        return urlparse(url)._replace(fragment="", query="").geturl().rstrip("/")
    except Exception:
        return url


def should_skip(url: str, skip_paths: list[str]) -> bool:
    path = urlparse(url).path.lower()
    return any(s in path for s in skip_paths)


def safe_name_from_url(url: str, max_len: int = 100) -> str:
    return re.sub(r"[^\w]", "_", url)[:max_len]


def hash_text(text: str, size: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:size]


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX_LONG_RE = re.compile(r"^[0-9a-f]{16,}$", re.I)
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_NUMERIC_RE = re.compile(r"^\d{2,}$")


def _looks_dynamic_segment(seg: str) -> bool:
    if not seg:
        return False
    if _UUID_RE.match(seg) or _HEX_LONG_RE.match(seg) or _ULID_RE.match(seg) or _NUMERIC_RE.match(seg):
        return True
    if len(seg) >= 20 and any(ch.isdigit() for ch in seg):
        # catches long mixed IDs/tokens
        return True
    return False


def canonicalize_path(path: str) -> str:
    if not path:
        return "/"
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "/"
    norm = [":id" if _looks_dynamic_segment(p) else p for p in parts]
    return "/" + "/".join(norm)


def canonicalize_path_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        return canonicalize_path(parsed.path or "/")
    except Exception:
        return canonicalize_path(url)


def coerce_health_score(value, default: float = 5.0) -> float:
    try:
        return float(str(value))
    except Exception:
        pass
    text = str(value or "").strip().lower()
    mapping = {
        "excellent": 9.0,
        "good": 7.5,
        "ok": 6.0,
        "average": 6.0,
        "fair": 5.0,
        "warning": 4.0,
        "poor": 3.0,
        "bad": 2.5,
        "critical": 1.5,
        "fail": 1.0,
        "failed": 1.0,
    }
    for k, v in mapping.items():
        if k in text:
            return v
    return default
