"""Persisted model capability probes for the active Hermes profile."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Iterator

from credentials import credential_path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback still keeps atomic writes.
    fcntl = None

_LOCK = Lock()
_CACHE_NAME = "model-capabilities.json"
_KEY = "additionalModelRequestFieldsUnsupportedModels"


def _cache_path() -> Path:
    return credential_path().parent / _CACHE_NAME


def _unsupported_models(path: Path | None = None) -> set[str]:
    try:
        data = json.loads((path or _cache_path()).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return set()
    models = data.get(_KEY) if isinstance(data, dict) else None
    return {model for model in models or [] if isinstance(model, str)}


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
    with _write_lock(path):
        models = _unsupported_models(path)
        if model_id not in models:
            _atomic_write(path, models | {model_id})
