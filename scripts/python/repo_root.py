"""Canonical repo-root resolver for Python entry points.

The shell counterpart is ``shared/repo-root.sh``; both derive the repository
root from their own location so a clone works from any directory under any
username, and both honour the same absolute ``VAULT_ROOT`` override with a
stderr diagnostic when it selects a different directory from this checkout.
"""

import os
import sys
from pathlib import Path

__all__ = ["resolve_vault_root"]


def resolve_vault_root() -> Path:
    """Return the repo root from the VAULT_ROOT env var or this file's location.

    An override is returned verbatim, never canonicalised: callers test whether
    a path is inside the vault by string prefix, and resolving would rewrite
    /var to /private/var on macOS and make every such comparison miss. A
    relative or nonexistent override is a configuration error and raises.
    Explicit overrides take precedence for fixture/data-only callers, but a
    mismatch with this checkout is always printed to stderr. Unset VAULT_ROOT
    to select this checkout. Symlink aliases of the same directory are quiet.
    """
    # scripts/python/repo_root.py -> the repo root is two parents up.
    derived = Path(__file__).resolve().parents[2]
    override = os.environ.get("VAULT_ROOT")
    if override:
        root = Path(override)
        if not root.is_absolute():
            raise ValueError(f"VAULT_ROOT must be an absolute path: {override}")
        if not root.is_dir():
            raise FileNotFoundError(f"VAULT_ROOT is not a directory: {override}")
        if not root.samefile(derived):
            print(
                f"repo_root.py: warning: VAULT_ROOT={override} overrides "
                f"location-derived root {derived}; unset VAULT_ROOT to use this checkout",
                file=sys.stderr,
            )
        return root
    return derived
