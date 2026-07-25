"""Individual lint rules.

Every rule is a plain function ``rule(node_id, node, ctx) -> list[Diagnostic]``
so rules stay independently testable and easy to extend. ``ctx`` carries the
schema and the whole workflow, because some checks (links) are cross-node.
"""

ERROR = "error"
WARNING = "warning"

#: Numeric widening that ComfyUI performs silently.
_COMPATIBLE_TYPES = {
    ("INT", "FLOAT"),
    ("INT", "NUMBER"),
    ("FLOAT", "NUMBER"),
    ("NUMBER", "FLOAT"),
    ("NUMBER", "INT"),
}

WILDCARD = "*"

#: Normalized type name of a combo/enum widget; never link-type-checked.
COMBO = "COMBO"


class Diagnostic(object):
    """One finding, in a shape that both humans and CI can consume."""

    __slots__ = ("severity", "rule", "node_id", "class_type", "field", "message")

    def __init__(self, severity, rule, node_id, class_type, field, message):
        self.severity = severity
        self.rule = rule
        self.node_id = node_id
        self.class_type = class_type
        self.field = field
        self.message = message

    @property
    def is_error(self):
        return self.severity == ERROR

    def anchor(self, path=None):
        """``file:node:field`` style location string."""
        parts = []
        if path:
            parts.append(str(path))
        parts.append(str(self.node_id) if self.node_id is not None else "-")
        if self.field:
            parts.append(str(self.field))
        return ":".join(parts)

    def to_dict(self):
        return {
            "severity": self.severity,
            "rule": self.rule,
            "node_id": self.node_id,
            "class_type": self.class_type,
            "field": self.field,
            "message": self.message,
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Diagnostic(%s, %s, node=%s, field=%s)" % (
            self.severity,
            self.rule,
            self.node_id,
            self.field,
        )

    def _key(self):
        return (
            self.severity,
            self.rule,
            self.node_id,
            self.class_type,
            self.field,
            self.message,
        )

    def __eq__(self, other):
        if not isinstance(other, Diagnostic):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self):
        # Diagnostics are immutable in practice, so hashing by value lets
        # callers de-duplicate them with a set.
        return hash(self._key())


class Context(object):
    """Everything a rule may need beyond the node it is looking at."""

    __slots__ = ("schema", "workflow")

    def __init__(self, schema, workflow):
        self.schema = schema
        self.workflow = workflow

    def node_schema(self, node_id):
        node = self.workflow.get(node_id)
        if not isinstance(node, dict):
            return None
        return self.schema.get(node.get("class_type"))


def is_link(value):
    """True when an input value is a link ``[node_id, output_index]``."""
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and not isinstance(value[0], bool)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def _inputs_of(node):
    inputs = node.get("inputs")
    return inputs if isinstance(inputs, dict) else {}


def _summarize(choices, limit=6):
    shown = [repr(c) for c in choices[:limit]]
    if len(choices) > limit:
        shown.append("... (%d more)" % (len(choices) - limit))
    return ", ".join(shown)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def rule_node_shape(node_id, node, ctx):
    """The node itself must be a well-formed API-format entry."""
    if not isinstance(node, dict):
        return [
            Diagnostic(
                ERROR, "malformed-node", node_id, None, None,
                "node must be a JSON object, got %s" % type(node).__name__,
            )
        ]
    out = []
    class_type = node.get("class_type")
    if not isinstance(class_type, str) or not class_type:
        out.append(
            Diagnostic(
                ERROR, "malformed-node", node_id, None, "class_type",
                "node is missing a string 'class_type'",
            )
        )
    if "inputs" in node and not isinstance(node["inputs"], dict):
        out.append(
            Diagnostic(
                ERROR, "malformed-node", node_id, class_type, "inputs",
                "'inputs' must be a JSON object, got %s"
                % type(node["inputs"]).__name__,
            )
        )
    return out


def rule_unknown_class_type(node_id, node, ctx):
    """The node class must exist on the target ComfyUI install."""
    class_type = node.get("class_type")
    if not isinstance(class_type, str) or not class_type:
        return []
    if class_type in ctx.schema:
        return []
    message = "unknown node class_type %r - not installed on this ComfyUI" % class_type
    suggestions = ctx.schema.similar_names(class_type)
    if suggestions:
        message += " (did you mean: %s?)" % ", ".join(suggestions)
    return [
        Diagnostic(ERROR, "unknown-node", node_id, class_type, "class_type", message)
    ]


def rule_missing_required(node_id, node, ctx):
    """Every required input must be present."""
    class_type = node.get("class_type")
    spec = ctx.schema.get(class_type)
    if spec is None:
        return []
    given = set(_inputs_of(node).keys())
    out = []
    for name in sorted(spec.required_names):
        if name in given:
            continue
        slot = spec.inputs[name]
        out.append(
            Diagnostic(
                ERROR, "missing-required", node_id, class_type, name,
                "missing required input %r (expects %s)" % (name, slot.type_name),
            )
        )
    return out


def rule_extraneous_input(node_id, node, ctx):
    """Inputs the node class does not declare are silently ignored by ComfyUI."""
    class_type = node.get("class_type")
    spec = ctx.schema.get(class_type)
    if spec is None:
        return []
    out = []
    for name in _inputs_of(node):
        if name in spec.inputs:
            continue
        out.append(
            Diagnostic(
                WARNING, "extraneous-input", node_id, class_type, name,
                "unknown input %r for %s - it will be ignored" % (name, class_type),
            )
        )
    return out


def rule_invalid_enum(node_id, node, ctx):
    """Literal values fed to a COMBO widget must be one of its choices."""
    class_type = node.get("class_type")
    spec = ctx.schema.get(class_type)
    if spec is None:
        return []
    out = []
    for name, value in _inputs_of(node).items():
        slot = spec.inputs.get(name)
        if slot is None or not slot.is_enum:
            continue
        if is_link(value):
            continue  # value is produced upstream; nothing static to check
        if value in slot.choices:
            continue
        out.append(
            Diagnostic(
                ERROR, "invalid-enum", node_id, class_type, name,
                "%r is not a valid value for %r; allowed: %s"
                % (value, name, _summarize(slot.choices)),
            )
        )
    return out


def rule_dangling_link(node_id, node, ctx):
    """Links must point at a node that exists, on an output slot that exists."""
    class_type = node.get("class_type")
    out = []
    for name, value in _inputs_of(node).items():
        if not is_link(value):
            continue
        target_id, slot_index = str(value[0]), value[1]
        if target_id not in ctx.workflow:
            out.append(
                Diagnostic(
                    ERROR, "dangling-link", node_id, class_type, name,
                    "%r links to node %r, which does not exist in this workflow"
                    % (name, target_id),
                )
            )
            continue
        upstream = ctx.node_schema(target_id)
        if upstream is None:
            continue  # upstream class is unknown; already reported elsewhere
        if slot_index < 0 or slot_index >= len(upstream.outputs):
            out.append(
                Diagnostic(
                    ERROR, "dangling-link", node_id, class_type, name,
                    "%r links to node %s output #%d, but %s has %d output(s)"
                    % (name, target_id, slot_index, upstream.class_type,
                       len(upstream.outputs)),
                )
            )
    return out


def rule_type_mismatch(node_id, node, ctx):
    """Where both ends are known, a link's types must line up."""
    class_type = node.get("class_type")
    spec = ctx.schema.get(class_type)
    if spec is None:
        return []
    out = []
    for name, value in _inputs_of(node).items():
        if not is_link(value):
            continue
        slot = spec.inputs.get(name)
        if slot is None or slot.type_name == COMBO:
            # A combo slot has no link type of its own to compare against --
            # true whether ComfyUI spelled its choices out or not.
            continue
        target_id, slot_index = str(value[0]), value[1]
        upstream = ctx.node_schema(target_id)
        if upstream is None:
            continue
        produced = upstream.output_type(slot_index)
        if produced is None:
            continue  # dangling slot; reported by rule_dangling_link
        expected = slot.type_name
        if expected == WILDCARD or produced == WILDCARD or expected == produced:
            continue
        if (produced, expected) in _COMPATIBLE_TYPES:
            continue
        out.append(
            Diagnostic(
                ERROR, "type-mismatch", node_id, class_type, name,
                "%r expects %s but node %s output #%d (%s) produces %s"
                % (name, expected, target_id, slot_index,
                   upstream.class_type, produced),
            )
        )
    return out


#: Executed in this order for every node in the workflow.
ALL_RULES = (
    rule_node_shape,
    rule_unknown_class_type,
    rule_missing_required,
    rule_extraneous_input,
    rule_invalid_enum,
    rule_dangling_link,
    rule_type_mismatch,
)

#: rule id -> one-line description, used by ``--list-rules`` and the README.
RULE_DOCS = (
    ("malformed-node", ERROR, "Node entry is not a valid API-format object."),
    ("unknown-node", ERROR, "class_type is not installed on the target server."),
    ("missing-required", ERROR, "A required input is absent."),
    ("extraneous-input", WARNING, "An input the node does not declare."),
    ("invalid-enum", ERROR, "Literal value outside a COMBO widget's choices."),
    ("dangling-link", ERROR, "Link points at a missing node or output slot."),
    ("type-mismatch", ERROR, "Linked output type does not match the input slot."),
)
