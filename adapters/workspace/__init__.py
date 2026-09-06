"""Sandboxed project workspaces (SPEC §12, Phase 7): files, diffs and runs behind the gate."""

from adapters.workspace.capabilities import WORKSPACE_MANIFESTS, register_workspace
from adapters.workspace.manager import WorkspaceError, WorkspaceManager

__all__ = ["WORKSPACE_MANIFESTS", "WorkspaceError", "WorkspaceManager", "register_workspace"]
