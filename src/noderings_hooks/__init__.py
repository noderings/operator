"""Shared VM lifecycle hook runner for NodeRings operators."""

from .runner import dispatch_hook, run_hook_for_operation, safe_run_hook
from .types import HookConcurrencyError

__all__ = [
    "HookConcurrencyError",
    "dispatch_hook",
    "run_hook_for_operation",
    "safe_run_hook",
]
