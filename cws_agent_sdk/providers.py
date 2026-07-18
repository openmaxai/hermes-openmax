"""Injection interfaces that keep the SDK free of host-environment concerns.

The SDK only speaks the CWS HTTP/WS contract. Anything that touches the host
(where tokens persist, how logs are emitted) is injected by the adapter.
Every provider has a default implementation so a missing provider degrades a
feature instead of crashing the message path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class StorageProvider(Protocol):
    """Small key-value persistence for tokens, sync watermarks, dedup state."""

    def read(self, key: str) -> Optional[bytes]: ...

    def write(self, key: str, data: bytes) -> None: ...

    def delete(self, key: str) -> None: ...


@runtime_checkable
class Logger(Protocol):
    def log(self, *args: Any) -> None: ...

    def warn(self, *args: Any) -> None: ...


class FileStorage:
    """Default StorageProvider: one file per key under a base directory."""

    def __init__(self, base_dir: str | Path):
        self._base = Path(base_dir).expanduser()
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = key.replace("/", "_")
        return self._base / safe

    def read(self, key: str) -> Optional[bytes]:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None

    def write(self, key: str, data: bytes) -> None:
        p = self._path(key)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)  # atomic on POSIX

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass

    def path_for(self, key: str) -> str:
        """Real filesystem path for a key (e.g. downloaded media for vision)."""
        return str(self._path(key))

    # JSON convenience wrappers used across the SDK.
    def read_json(self, key: str) -> Optional[Any]:
        raw = self.read(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None

    def write_json(self, key: str, value: Any) -> None:
        self.write(key, json.dumps(value, ensure_ascii=False).encode("utf-8"))


class StdLogger:
    """Default Logger: prefixed stdout prints."""

    def __init__(self, prefix: str = "[cws-sdk]"):
        self._prefix = prefix

    def log(self, *args: Any) -> None:
        print(self._prefix, *args)

    def warn(self, *args: Any) -> None:
        print(self._prefix, "⚠️", *args)
