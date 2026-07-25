"""Fetching, caching and normalizing a ComfyUI ``/object_info`` document.

``/object_info`` is the authoritative description of every node class a given
ComfyUI instance can execute. It is a large, loosely-typed JSON blob; this
module turns it into a small lookup that the rules can query without having to
re-learn ComfyUI's quirks.
"""

import json
import os
import urllib.error
import urllib.request

DEFAULT_SERVER = "http://127.0.0.1:8188"
DEFAULT_TIMEOUT = 15.0

#: Input type name that accepts a link of any type.
WILDCARD = "*"


class SchemaError(Exception):
    """Raised when a schema cannot be fetched, read or parsed."""


class InputSpec(object):
    """One declared input slot of a node class.

    Attributes:
        name: the input name as used in a workflow's ``inputs`` mapping.
        type_name: ``"MODEL"``, ``"INT"``, ... or ``"COMBO"`` for enums.
        choices: the allowed values when the slot is an enum, else ``None``.
        required: whether ComfyUI declares the slot as required.
    """

    __slots__ = ("name", "type_name", "choices", "required")

    def __init__(self, name, type_name, choices=None, required=True):
        self.name = name
        self.type_name = type_name
        self.choices = choices
        self.required = required

    @property
    def is_enum(self):
        return self.choices is not None

    def __repr__(self):  # pragma: no cover - debugging aid
        return "InputSpec(%r, %r, required=%r)" % (
            self.name,
            self.type_name,
            self.required,
        )


class NodeSchema(object):
    """Normalized description of a single node class."""

    __slots__ = ("class_type", "inputs", "outputs")

    def __init__(self, class_type, inputs, outputs):
        self.class_type = class_type
        self.inputs = inputs  # dict: name -> InputSpec
        self.outputs = outputs  # list of output type names

    @property
    def required_names(self):
        return [name for name, spec in self.inputs.items() if spec.required]

    def output_type(self, index):
        """Return the type produced at ``index``, or ``None`` if out of range."""
        if isinstance(index, bool) or not isinstance(index, int):
            return None
        if 0 <= index < len(self.outputs):
            return self.outputs[index]
        return None

    def __repr__(self):  # pragma: no cover - debugging aid
        return "NodeSchema(%r, %d inputs)" % (self.class_type, len(self.inputs))


class Schema(object):
    """A whole ``/object_info`` document, normalized."""

    __slots__ = ("nodes", "source")

    def __init__(self, nodes, source=""):
        self.nodes = nodes  # dict: class_type -> NodeSchema
        self.source = source

    def __contains__(self, class_type):
        return class_type in self.nodes

    def __len__(self):
        return len(self.nodes)

    def get(self, class_type):
        return self.nodes.get(class_type)

    def similar_names(self, class_type, limit=3):
        """Cheap "did you mean ...?" suggestions, case/underscore insensitive."""
        key = _fold(class_type)
        exact = [name for name in self.nodes if _fold(name) == key]
        if exact:
            return exact[:limit]
        partial = [
            name
            for name in self.nodes
            if key and (key in _fold(name) or _fold(name) in key)
        ]
        return sorted(partial)[:limit]


def _fold(text):
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def _parse_input_spec(name, raw, required):
    """Normalize one entry of ``input.required`` / ``input.optional``.

    ComfyUI writes each entry as ``[type, options]`` where ``type`` is either a
    string (``"MODEL"``) or a list of literal choices (an enum widget). Some
    custom nodes omit the options element entirely.
    """
    if isinstance(raw, list) and raw:
        declared = raw[0]
    else:
        declared = raw
    if isinstance(declared, list):
        choices = [c for c in declared]
        return InputSpec(name, "COMBO", choices, required)
    if isinstance(declared, str):
        return InputSpec(name, declared, None, required)
    # Unrecognized shape: accept anything rather than emit false positives.
    return InputSpec(name, WILDCARD, None, required)


def normalize(raw_object_info, source=""):
    """Turn a raw ``/object_info`` mapping into a :class:`Schema`."""
    if not isinstance(raw_object_info, dict):
        raise SchemaError("object_info must be a JSON object at the top level")
    nodes = {}
    for class_type, entry in raw_object_info.items():
        if not isinstance(entry, dict):
            continue
        raw_input = entry.get("input") or {}
        if not isinstance(raw_input, dict):
            raw_input = {}
        inputs = {}
        for section, required in (("required", True), ("optional", False)):
            block = raw_input.get(section) or {}
            if not isinstance(block, dict):
                continue
            for name, raw in block.items():
                inputs[name] = _parse_input_spec(name, raw, required)
        outputs = entry.get("output") or []
        if isinstance(outputs, str):
            outputs = [outputs]
        flat_outputs = []
        for item in outputs:
            # A list here means a COMBO output; its concrete type is unknown.
            flat_outputs.append(item if isinstance(item, str) else WILDCARD)
        nodes[class_type] = NodeSchema(class_type, inputs, flat_outputs)
    if not nodes:
        raise SchemaError("object_info contained no node definitions")
    return Schema(nodes, source)


def load_from_file(path):
    """Load a cached ``/object_info`` document from disk."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (IOError, OSError) as exc:
        raise SchemaError("cannot read schema cache %s: %s" % (path, exc))
    except ValueError as exc:
        raise SchemaError("schema cache %s is not valid JSON: %s" % (path, exc))
    return normalize(raw, source=path)


def fetch_from_server(server=DEFAULT_SERVER, timeout=DEFAULT_TIMEOUT):
    """Fetch ``/object_info`` from a running ComfyUI server."""
    url = server.rstrip("/") + "/object_info"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise SchemaError("cannot reach ComfyUI at %s: %s" % (url, exc.reason))
    except (IOError, OSError) as exc:
        raise SchemaError("cannot reach ComfyUI at %s: %s" % (url, exc))
    try:
        raw = json.loads(payload)
    except ValueError as exc:
        raise SchemaError("%s did not return valid JSON: %s" % (url, exc))
    return normalize(raw, source=url), raw


def save_cache(raw_object_info, path):
    """Write a raw ``/object_info`` document to ``path`` for offline reuse."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        try:
            os.makedirs(directory)
        except OSError as exc:
            raise SchemaError("cannot create %s: %s" % (directory, exc))
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(raw_object_info, handle, ensure_ascii=False)
    except (IOError, OSError) as exc:
        raise SchemaError("cannot write schema cache %s: %s" % (path, exc))


def load_schema(server=DEFAULT_SERVER, cache_path=None, refresh=False,
                timeout=DEFAULT_TIMEOUT):
    """Resolve a schema, preferring an existing cache over the network.

    * ``cache_path`` present on disk and ``refresh`` false -> read it (offline).
    * otherwise -> fetch from ``server`` and, if ``cache_path`` was given,
      write the raw document there so later runs need no server.
    """
    if cache_path and not refresh and os.path.isfile(cache_path):
        return load_from_file(cache_path)
    schema, raw = fetch_from_server(server, timeout)
    if cache_path:
        save_cache(raw, cache_path)
        schema.source = "%s (cached to %s)" % (schema.source, cache_path)
    return schema
