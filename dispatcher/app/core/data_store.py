"""
dispatcher/app/core/data_store.py
─────────────────────────────────
JSON-backed persistent storage for dispatcher extension data:
  - Templates
  - Staging areas (named host-path mounts)
  - Schedules (deferred/triggered job submissions)

Design
──────
All data lives under a single directory ($CAAS_DATA_DIR or /srv/caas-data).
Each category is a separate JSON file.  Writes are atomic (write → tmp → rename)
so the dispatcher never reads a partially-written file.

Thread safety
─────────────
A per-file threading.Lock prevents concurrent readers/writers from corrupting
JSON on disk.  The in-process lock is *not* shared across processes, so this
module is safe for a single-process FastAPI deployment (which is how the
Docker Compose service is configured).

Public API
──────────
DataStore(path=)
    Create a store rooted at *path* (created if it doesn't exist).

    store.read(category) → list[dict] | None
    store.write(category, items: list[dict]) → None
    store.update(category, key_id: str, update_dict) → bool
    store.delete(category, key_id: str) → bool

    read() / write() / update() / delete() are all thread-safe.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import typing as t

logger = logging.getLogger("caas.data_store")

# Default data directory – must be bind-mounted so data survives container restarts.
DEFAULT_DATA_DIR = os.path.join("/srv", "caas-data")

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class DataStore:
    """JSON-backed persistent store for dispatcher extension data."""

    def __init__(self, data_dir: str | None = None) -> None:
        self._data_dir = data_dir or os.getenv("CAAS_DATA_DIR", DEFAULT_DATA_DIR)
        try:
            os.makedirs(self._data_dir, exist_ok=True)
        except OSError:
            # Non-writable directory (e.g. in tests or restricted environments);
            # operations that require persistence will fail with a clear message.
            logger.warning(
                "Could not create data directory %r – persistence is read-only.",
                self._data_dir,
            )
        self._data_dir = os.path.realpath(self._data_dir) if os.path.exists(self._data_dir) else self._data_dir
        # Per-file locks for thread safety.
        self._locks: dict[str, threading.Lock] = {}
        self._parent_lock = threading.Lock()

    def _file_path(self, category: str) -> str:
        return os.path.join(self._data_dir, f"{category}.json")

    def _load(self, category: str) -> list[dict]:
        path = self._file_path(category)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r") as f:
                data = json.load(f)
                if not isinstance(data, list):
                    logger.warning("Category %r has non-list JSON – resetting.", category)
                    return []
                return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read %s: %s – returning empty list.", path, exc)
            return []

    def _save(self, category: str, items: list[dict]) -> None:
        path = self._file_path(category)
        # Atomic write: write to temp file in same directory, then rename.
        dir_name = os.path.dirname(path)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(items, f, indent=2, default=str)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _get_lock(self, category: str) -> threading.Lock:
        with self._parent_lock:
            if category not in self._locks:
                self._locks[category] = threading.Lock()
            return self._locks[category]

    def read(self, category: str) -> list[dict]:
        """Read all items for *category*."""
        lock = self._get_lock(category)
        with lock:
            return self._load(category)

    def write(self, category: str, items: list[dict]) -> None:
        """Overwrite all items for *category*."""
        lock = self._get_lock(category)
        with lock:
            self._save(category, items)

    def fetch(self, category: str, key_id: str) -> dict | None:
        """Return the item with the matching 'id' field, or None."""
        lock = self._get_lock(category)
        with lock:
            items = self._load(category)
            for item in items:
                if item.get("id") == key_id:
                    return dict(item)
            return None

    def update(self, category: str, key_id: str, updates: dict) -> bool:
        """Update the item with matching *key_id* with *updates*. Returns True if found."""
        lock = self._get_lock(category)
        with lock:
            items = self._load(category)
            for i, item in enumerate(items):
                if item.get("id") == key_id:
                    items[i].update(updates)
                    self._save(category, items)
                    return True
            return False

    def create(self, category: str, item: dict) -> dict:
        """Append *item* (must already have an 'id' key). Returns the item."""
        lock = self._get_lock(category)
        with lock:
            items = self._load(category)
            items.append(item)
            self._save(category, items)
            return dict(item)

    def append_list(self, category: str, key_id: str, field: str, value: t.Any) -> bool:
        """Append *value* to the list field *field* on the item with matching *key_id*.

        Creates the list if it doesn't exist.
        """
        lock = self._get_lock(category)
        with lock:
            items = self._load(category)
            for i, item in enumerate(items):
                if item.get("id") == key_id:
                    if field not in item or not isinstance(item.get(field), list):
                        item[field] = []
                    item[field].append(value)
                    self._save(category, items)
                    return True
            return False

    def delete(self, category: str, key_id: str) -> bool:
        """Delete the item with matching *key_id*. Returns True if found."""
        lock = self._get_lock(category)
        with lock:
            items = self._load(category)
            before = len(items)
            items = [item for item in items if item.get("id") != key_id]
            if len(items) == before:
                return False
            self._save(category, items)
            return True
