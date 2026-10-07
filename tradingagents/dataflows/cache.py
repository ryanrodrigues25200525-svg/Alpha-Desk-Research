"""TTL file cache for routed vendor calls (spec §1g).

Keys are opaque strings built by the caller (see ``router._vendor_cache_key``);
each maps to one JSON file under ``data_cache_dir``. Only successful returns
are cached — an ``fn`` that raises stores nothing, so a later call retries.
Writes go through ``files.replace_file`` so concurrent tool calls never leave
a partial file behind.
"""

import hashlib
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.files import replace_file

logger = logging.getLogger(__name__)


def _cache_path(cache_key: str) -> Path:
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    cache_dir = Path(get_config().get("data_cache_dir") or ".")
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"router-{digest}.json"


def get_or_fetch(cache_key: str, ttl_s: int, fn: Callable[[], Any]) -> Any:
    """Return the cached value for ``cache_key``, or ``fn()`` when missing/stale.

    A fresh entry (younger than ``ttl_s`` seconds) is returned without calling
    ``fn``. Anything unreadable — missing file, corrupt JSON, foreign shape —
    is treated as a miss. Values must be JSON-serializable; anything else is
    returned uncached rather than failing the call.
    """
    path = _cache_path(cache_key)
    try:
        if time.time() - path.stat().st_mtime < ttl_s:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict) and "value" in payload:
                return payload["value"]
    except (OSError, ValueError):
        pass

    value = fn()
    try:
        text = json.dumps({"value": value})
    except (TypeError, ValueError):
        return value
    try:
        replace_file(path, lambda tmp: Path(tmp).write_text(text, encoding="utf-8"))
    except OSError as exc:
        logger.warning("Vendor cache write skipped for %s: %s", path.name, exc)
    return value
