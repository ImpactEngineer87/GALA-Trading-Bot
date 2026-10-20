"""Absolute paths for runtime artifacts, independent of process working directory.

Do not discover/replace old models automatically: only the explicitly configured
model folder is trusted. Existing absolute operator-provided paths are retained.
"""
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_runtime_path(path, *, project_root=None):
    value = Path(str(path)).expanduser()
    if value.is_absolute():
        return str(value)
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    return str(root / value)
