"""Harman Code Quality Agent.

The package intentionally uses only the Python standard library.  External
CodeSonar, Gerrit, CI, and model protocols are explicit adapter boundaries;
they never silently downgrade to a successful fake implementation.
"""

from .domain import (
    ActionKind,
    Finding,
    RepairTask,
    Severity,
    Stage,
    ToolStatus,
    UTFailure,
)

__all__ = [
    "ActionKind",
    "Finding",
    "RepairTask",
    "Severity",
    "Stage",
    "ToolStatus",
    "UTFailure",
]
