"""comfy-lint: a fast static linter for ComfyUI API-format workflow JSON.

Catch broken workflows in milliseconds instead of discovering them after a
long GPU run has already been queued.
"""

__version__ = "0.1.0"

from .linter import Diagnostic, lint_workflow
from .schema import NodeSchema, Schema, SchemaError, load_schema

__all__ = [
    "__version__",
    "Diagnostic",
    "lint_workflow",
    "Schema",
    "NodeSchema",
    "SchemaError",
    "load_schema",
]
