"""Persisted model capability probes for the active Hermes profile."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Iterator

from credentials import state_dir

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback still keeps atomic writes.
    fcntl = None

_LOCK = RLock()
_CACHE_NAME = "model-capabilities.json"
_KEY = "additionalModelRequestFieldsUnsupportedModels"
_LOG = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    signature: tuple[int, int, int] | None
    disk_models: set[str]
    pending_models: set[str]


_MEMORY: dict[Path, _CacheEntry] = {}


def _cache_path() -> Path:
    return state_dir() / _CACHE_NAME


def _file_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


def _read_unsupported_models(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    models = data.get(_KEY)
    # ponytail: reject the whole malformed cache; a partial capability list is worse than a clean retry.
    if not isinstance(models, list) or not all(isinstance(model, str) for model in models):
        return set()
    return set(models)


def _entry(path: Path, signature: tuple[int, int, int] | None) -> _CacheEntry:
    entry = _MEMORY.get(path)
    if entry is None or entry.signature != signature:
        entry = _CacheEntry(signature, _read_unsupported_models(path), entry.pending_models if entry else set())
        _MEMORY[path] = entry
    return entry


def _unsupported_models(path: Path | None = None) -> set[str]:
    path = path or _cache_path()
    signature = _file_signature(path)
    with _LOCK:
        entry = _entry(path, signature)
        return entry.disk_models | entry.pending_models


def _remember_unsupported(path: Path, model_id: str) -> None:
    signature = _file_signature(path)
    with _LOCK:
        _entry(path, signature).pending_models.add(model_id)


def _remember_persisted(path: Path, models: set[str]) -> None:
    signature = _file_signature(path)
    with _LOCK:
        entry = _MEMORY.get(path)
        pending = entry.pending_models if entry else set()
        pending.difference_update(models)
        _MEMORY[path] = _CacheEntry(signature, models, pending)


@contextmanager
def _write_lock(path: Path) -> Iterator[None]:
    # ponytail: one profile-local lock prevents lost updates; split locks only if capabilities multiply.
    with _LOCK:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (path.parent / ".model-capabilities.lock").open("a+") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, models: set[str]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".model-capabilities.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({_KEY: sorted(models)}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def additional_model_request_fields_supported(model_id: str) -> bool:
    """Return False only for a model explicitly rejected by Kiro."""
    return model_id not in _unsupported_models()


def mark_additional_model_request_fields_unsupported(model_id: str) -> None:
    """Persist an exact Kiro model ID without recording request data or credentials."""
    if not model_id:
        return
    path = _cache_path()
    _remember_unsupported(path, model_id)
    try:
        with _write_lock(path):
            models = _read_unsupported_models(path) | _unsupported_models(path)
            _atomic_write(path, models)
            _remember_persisted(path, models)
    except OSError:
        _LOG.warning("Could not persist Kiro capability cache; using process memory.")
        return
