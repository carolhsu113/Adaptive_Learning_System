from __future__ import annotations

import json
import os
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from app.core.record_schema import validate_record


class DataDirLease:
    """Hold an OS lock for the lifetime of one writable application process."""

    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / ".writer.lock"
        self._handle: BinaryIO | None = None

    def __enter__(self) -> DataDirLease:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise RuntimeError("data directory is already in use") from error
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class AtomicJsonFileStore:
    """Durably write JSON through a same-directory atomic replacement."""

    @staticmethod
    def _prepare(path: Path, payload: dict[str, object]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            with temporary.open("r", encoding="utf-8") as handle:
                reloaded = json.load(handle)
            if not isinstance(reloaded, dict):
                raise ValueError("JSON record must be an object")
            validate_record(path, reloaded)
            return temporary
        except BaseException:
            if temporary.exists():
                temporary.unlink()
            raise

    @staticmethod
    def write(path: Path, payload: dict[str, object]) -> None:
        temporary = AtomicJsonFileStore._prepare(path, payload)
        try:
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def write_new(path: Path, payload: dict[str, object]) -> None:
        """Atomically publish a new immutable file, never replacing an existing one."""
        temporary = AtomicJsonFileStore._prepare(path, payload)
        try:
            os.link(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def read(path: Path) -> dict[str, object]:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("JSON record must be an object")
        validate_record(path, payload)
        return payload
