"""Hash- and unique-match-guarded source editing."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from ..domain import ToolStatus, sha256_bytes
from ..runtime.workspace import WorkspaceError, WorkspaceState, file_hash


class EditTool:
    def __init__(self, workspace: WorkspaceState, *, max_file_bytes: int) -> None:
        self.workspace = workspace
        self.max_file_bytes = max_file_bytes

    def edit_file(self, arguments: dict[str, Any]) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        relative = str(arguments.get("path", ""))
        expected_hash = str(arguments.get("expected_hash", ""))
        old_text = str(arguments.get("old_text", ""))
        new_text = str(arguments.get("new_text", ""))
        if not expected_hash or not old_text:
            return ToolStatus.ERROR, None, (relative,), {}, False, "expected_hash and non-empty old_text are required"
        try:
            path = self.workspace.resolve(relative, write=True)
        except WorkspaceError as exc:
            return ToolStatus.ERROR, None, (relative,), {}, False, str(exc)
        if not path.is_file():
            return ToolStatus.UNSUPPORTED, None, (relative,), {}, False, "file creation and deletion are not supported by edit_file"
        if path.stat().st_size > self.max_file_bytes:
            return ToolStatus.ERROR, None, (relative,), {}, False, "file exceeds edit limit"
        original = path.read_bytes()
        actual_hash = sha256_bytes(original)
        if actual_hash != expected_hash:
            return ToolStatus.VERSION_CHANGED, None, (relative,), {relative: actual_hash}, False, "content hash does not match"
        text = original.decode("utf-8")
        matches = text.count(old_text)
        if matches == 0:
            return ToolStatus.EMPTY, None, (relative,), {relative: actual_hash}, False, "old_text was not found"
        if matches != 1:
            return ToolStatus.AMBIGUOUS, None, (relative,), {relative: actual_hash}, False, f"old_text matched {matches} times"
        updated = text.replace(old_text, new_text, 1).encode("utf-8")
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            with os.fdopen(fd, "wb") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, mode)
            os.replace(temp_name, path)
        except OSError as exc:
            return ToolStatus.ERROR, None, (relative,), {relative: actual_hash}, False, f"atomic edit failed: {exc}"
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        self.workspace.mark_edit((relative,))
        new_hash = file_hash(path)
        return ToolStatus.OK, {"path": relative, "old_hash": actual_hash, "new_hash": new_hash, "changed_bytes": len(updated) - len(original)}, (relative,), {relative: new_hash}, True, None
