"""Command line entry point for comfy-lint."""

import argparse
import json
import os
import sys

from . import __version__
from .linter import (
    WorkflowError,
    apply_strict,
    counts,
    lint_workflow,
    load_workflow,
)
from .rules import ERROR, RULE_DOCS
from .schema import DEFAULT_SERVER, DEFAULT_TIMEOUT, SchemaError, load_schema

EXIT_OK = 0
EXIT_ERRORS = 1
EXIT_USAGE = 2

_COLORS = {"error": "\033[31m", "warning": "\033[33m", "dim": "\033[2m"}
_RESET = "\033[0m"


def _use_color(flag, stream):
    if flag == "never":
        return False
    if flag == "always":
        return True
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _paint(text, key, enabled):
    if not enabled or key not in _COLORS:
        return text
    return _COLORS[key] + text + _RESET


def build_parser():
    parser = argparse.ArgumentParser(
        prog="comfy-lint",
        description=(
            "Statically validate ComfyUI API-format workflow JSON against a "
            "server's /object_info, before you spend GPU time on it."
        ),
    )
    parser.add_argument(
        "workflow", nargs="*", metavar="workflow.json",
        help="one or more API-format workflow files to lint",
    )
    parser.add_argument(
        "--server", default=os.environ.get("COMFY_LINT_SERVER", DEFAULT_SERVER),
        metavar="URL",
        help="ComfyUI base URL to read /object_info from (default: %(default)s)",
    )
    parser.add_argument(
        "--schema-cache", metavar="PATH",
        default=os.environ.get("COMFY_LINT_SCHEMA_CACHE"),
        help=(
            "path to a cached /object_info document. If it exists it is used "
            "and no server is contacted; if it does not, the schema is fetched "
            "and written there for offline runs."
        ),
    )
    parser.add_argument(
        "--refresh-cache", action="store_true",
        help="ignore an existing --schema-cache file and refetch it",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS",
        help="HTTP timeout when contacting the server (default: %(default)s)",
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit machine-readable JSON instead of human-readable text",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="treat warnings as errors",
    )
    parser.add_argument(
        "--color", choices=("auto", "always", "never"), default="auto",
        help="colorize human-readable output (default: %(default)s)",
    )
    parser.add_argument(
        "--list-rules", action="store_true",
        help="print the rule table and exit",
    )
    parser.add_argument(
        "--version", action="version", version="comfy-lint " + __version__,
    )
    return parser


def _print_rules(out):
    width = max(len(rule) for rule, _, _ in RULE_DOCS)
    out.write("comfy-lint rules:\n")
    for rule, severity, description in RULE_DOCS:
        out.write("  %-*s  %-7s  %s\n" % (width, rule, severity, description))


def _report_text(results, schema_source, strict, out, color):
    total_errors = 0
    total_warnings = 0
    for path, diagnostics in results:
        errors, warnings = counts(diagnostics)
        total_errors += errors
        total_warnings += warnings
        display = os.path.basename(path)
        for diag in diagnostics:
            out.write(
                "%s  %s  %s  %s\n"
                % (
                    diag.anchor(display),
                    _paint(diag.severity, diag.severity, color),
                    diag.rule,
                    ("%s: %s" % (diag.class_type, diag.message))
                    if diag.class_type
                    else diag.message,
                )
            )
    plural = lambda n, word: "%d %s%s" % (n, word, "" if n == 1 else "s")
    if total_errors or total_warnings:
        out.write(
            "\n%s, %s in %s\n"
            % (
                plural(total_errors, "error"),
                plural(total_warnings, "warning"),
                plural(len(results), "file"),
            )
        )
    else:
        out.write(
            "%s clean (%s)\n"
            % (plural(len(results), "file"), plural(0, "issue"))
        )
    out.write(_paint("schema: %s%s\n" % (schema_source, " [strict]" if strict else ""),
                     "dim", color))


def _report_json(results, schema_source, strict, out):
    files = []
    total_errors = 0
    total_warnings = 0
    for path, diagnostics in results:
        errors, warnings = counts(diagnostics)
        total_errors += errors
        total_warnings += warnings
        files.append({
            "path": path,
            "errors": errors,
            "warnings": warnings,
            "diagnostics": [d.to_dict() for d in diagnostics],
        })
    payload = {
        "tool": "comfy-lint",
        "version": __version__,
        "schema_source": schema_source,
        "strict": strict,
        "summary": {
            "files": len(results),
            "errors": total_errors,
            "warnings": total_warnings,
        },
        "files": files,
    }
    json.dump(payload, out, indent=2, ensure_ascii=False, sort_keys=False)
    out.write("\n")


def main(argv=None, stdout=None, stderr=None):
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    if args.list_rules:
        _print_rules(out)
        return EXIT_OK

    if not args.workflow:
        parser.print_usage(err)
        err.write("comfy-lint: error: at least one workflow file is required\n")
        return EXIT_USAGE

    try:
        schema = load_schema(
            server=args.server,
            cache_path=args.schema_cache,
            refresh=args.refresh_cache,
            timeout=args.timeout,
        )
    except SchemaError as exc:
        err.write("comfy-lint: %s\n" % exc)
        if not args.schema_cache:
            err.write(
                "hint: pass --schema-cache PATH to lint offline without a "
                "running ComfyUI.\n"
            )
        return EXIT_USAGE

    results = []
    for path in args.workflow:
        try:
            workflow = load_workflow(path)
        except WorkflowError as exc:
            err.write("comfy-lint: %s\n" % exc)
            return EXIT_USAGE
        diagnostics = lint_workflow(workflow, schema)
        if args.strict:
            diagnostics = apply_strict(diagnostics)
        results.append((path, diagnostics))

    if args.as_json:
        _report_json(results, schema.source, args.strict, out)
    else:
        _report_text(results, schema.source, args.strict, out,
                     _use_color(args.color, out))

    has_errors = any(
        d.severity == ERROR for _, diagnostics in results for d in diagnostics
    )
    return EXIT_ERRORS if has_errors else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
