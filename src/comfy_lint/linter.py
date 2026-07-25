"""Orchestration: run every rule over every node of a workflow."""

import json

from .rules import (
    ALL_RULES,
    ERROR,
    WARNING,
    Context,
    Diagnostic,
    rule_node_shape,
)


class WorkflowError(Exception):
    """Raised when the workflow file cannot be read or is not API format."""


def _node_sort_key(node_id):
    try:
        return (0, int(node_id), "")
    except (TypeError, ValueError):
        return (1, 0, str(node_id))


def load_workflow(path):
    """Read a workflow JSON file and return its API-format node mapping."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (IOError, OSError) as exc:
        raise WorkflowError("cannot read workflow %s: %s" % (path, exc))
    except ValueError as exc:
        raise WorkflowError("%s is not valid JSON: %s" % (path, exc))
    return coerce_workflow(data, path)


def coerce_workflow(data, path="<workflow>"):
    """Accept the shapes ComfyUI hands out and return a node mapping."""
    if isinstance(data, dict) and isinstance(data.get("prompt"), dict):
        data = data["prompt"]
    if not isinstance(data, dict):
        raise WorkflowError(
            "%s must contain a JSON object of nodes, got %s"
            % (path, type(data).__name__)
        )
    if isinstance(data.get("nodes"), list):
        raise WorkflowError(
            "%s looks like a saved UI workflow, not API format. In ComfyUI use "
            "Workflow > Export (API) and lint that file instead." % path
        )
    if not data:
        raise WorkflowError("%s contains no nodes" % path)
    return data


def lint_workflow(workflow, schema, rules=ALL_RULES):
    """Return every :class:`Diagnostic` found in ``workflow``.

    Results are ordered by node id and then by the order rules are declared,
    so output is stable across runs (important for CI diffs).
    """
    ctx = Context(schema, workflow)
    diagnostics = []
    for node_id in sorted(workflow.keys(), key=_node_sort_key):
        node = workflow[node_id]
        if not isinstance(node, dict):
            diagnostics.extend(rule_node_shape(node_id, node, ctx))
            continue
        for rule in rules:
            diagnostics.extend(rule(node_id, node, ctx))
    return diagnostics


def apply_strict(diagnostics):
    """Promote every warning to an error (``--strict``)."""
    promoted = []
    for diag in diagnostics:
        if diag.severity == WARNING:
            diag = Diagnostic(
                ERROR, diag.rule, diag.node_id, diag.class_type,
                diag.field, diag.message,
            )
        promoted.append(diag)
    return promoted


def counts(diagnostics):
    """Return ``(error_count, warning_count)``."""
    errors = sum(1 for d in diagnostics if d.severity == ERROR)
    return errors, len(diagnostics) - errors
