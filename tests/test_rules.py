"""Tests for the rules, the orchestrator and the CLI (offline throughout)."""

import io
import json
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
)

from comfy_lint import cli, linter, rules, schema  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
OBJECT_INFO = os.path.join(FIXTURES, "object_info_sample.json")


def fixture(name):
    return os.path.join(FIXTURES, name)


@pytest.fixture(scope="module")
def sample_schema():
    return schema.load_from_file(OBJECT_INFO)


def lint_file(name, sample_schema):
    workflow = linter.load_workflow(fixture(name))
    return linter.lint_workflow(workflow, sample_schema)


def rule_ids(diagnostics):
    return sorted(d.rule for d in diagnostics)


# --------------------------------------------------------------------------
# schema normalization
# --------------------------------------------------------------------------


def test_schema_normalizes_types_and_enums(sample_schema):
    ksampler = sample_schema.get("KSampler")
    assert ksampler.inputs["model"].type_name == "MODEL"
    assert ksampler.inputs["sampler_name"].is_enum
    assert "dpmpp_2m" in ksampler.inputs["sampler_name"].choices
    assert ksampler.outputs == ["LATENT"]


def test_schema_marks_optional_inputs(sample_schema):
    lora = sample_schema.get("LoraLoader")
    assert lora.inputs["note"].required is False
    assert "note" not in lora.required_names
    assert lora.inputs["model"].required is True


def test_schema_reads_inline_combo_choices():
    """The classic spelling: the choices are the declared type."""
    spec = schema.normalize({
        "Node": {
            "input": {"required": {"sampler_name": [["euler", "heun"], {"default": "euler"}]}},
            "output": [],
        }
    })
    slot = spec.get("Node").inputs["sampler_name"]
    assert slot.type_name == "COMBO"
    assert slot.is_enum and slot.is_combo
    assert slot.choices == ["euler", "heun"]


def test_schema_reads_combo_options_dict():
    """The newer spelling: ["COMBO", {"options": [...]}]."""
    spec = schema.normalize({
        "Node": {
            "input": {
                "required": {
                    "sampler_name": ["COMBO", {"options": ["euler", "heun"], "default": "euler"}]
                }
            },
            "output": [],
        }
    })
    slot = spec.get("Node").inputs["sampler_name"]
    assert slot.type_name == "COMBO"
    assert slot.is_enum and slot.is_combo
    assert slot.choices == ["euler", "heun"]


def test_combo_options_dict_still_catches_bad_enum_values():
    """The headline rule must keep working under the new COMBO spelling."""
    spec = schema.normalize({
        "KSampler": {
            "input": {
                "required": {"sampler_name": ["COMBO", {"options": ["euler", "heun"]}]}
            },
            "output": ["LATENT"],
        }
    })
    nodes = {"1": {"class_type": "KSampler", "inputs": {"sampler_name": "dpmpp_3m_sde"}}}
    diagnostics = linter.lint_workflow(nodes, spec)
    assert rule_ids(diagnostics) == ["invalid-enum"]
    assert "'euler'" in diagnostics[0].message
    good = {"1": {"class_type": "KSampler", "inputs": {"sampler_name": "euler"}}}
    assert linter.lint_workflow(good, spec) == []


def test_combo_without_options_is_never_type_checked():
    """A COMBO whose choices ComfyUI computes at runtime must not false-positive."""
    spec = schema.normalize({
        "Source": {"input": {"required": {}}, "output": ["STRING"]},
        "Node": {
            "input": {"required": {"pick": ["COMBO", {"tooltip": "filled in at runtime"}]}},
            "output": [],
        },
    })
    slot = spec.get("Node").inputs["pick"]
    assert slot.is_combo and not slot.is_enum
    # A literal value cannot be judged...
    assert linter.lint_workflow(
        {"1": {"class_type": "Node", "inputs": {"pick": "anything at all"}}}, spec
    ) == []
    # ...and neither can a link into the slot (no type-mismatch false positive).
    linked = {
        "1": {"class_type": "Source", "inputs": {}},
        "2": {"class_type": "Node", "inputs": {"pick": ["1", 0]}},
    }
    assert linter.lint_workflow(linked, spec) == []


def test_bare_combo_string_without_options_element():
    """Some custom nodes write just ["COMBO"] with no options at all."""
    spec = schema.normalize({
        "Node": {"input": {"required": {"pick": ["COMBO"]}}, "output": []}
    })
    slot = spec.get("Node").inputs["pick"]
    assert slot.is_combo and not slot.is_enum


def test_schema_rejects_empty_document():
    with pytest.raises(schema.SchemaError):
        schema.normalize({})


def test_similar_names_suggests_close_match(sample_schema):
    assert "SaveImage" in sample_schema.similar_names("SuperSaveImage")
    assert "KSampler" in sample_schema.similar_names("ksampler")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def test_valid_workflow_is_clean(sample_schema):
    assert lint_file("valid_workflow.json", sample_schema) == []


def test_unknown_node_is_reported(sample_schema):
    diagnostics = lint_file("broken_unknown_node.json", sample_schema)
    assert rule_ids(diagnostics) == ["unknown-node"]
    diag = diagnostics[0]
    assert diag.severity == rules.ERROR
    assert diag.node_id == "7"
    assert diag.class_type == "SuperSaveImage"
    assert "did you mean" in diag.message


def test_missing_required_is_reported(sample_schema):
    diagnostics = lint_file("broken_missing_required.json", sample_schema)
    missing = [(d.node_id, d.field) for d in diagnostics if d.rule == "missing-required"]
    assert missing == [("5", "denoise"), ("5", "scheduler"), ("6", "vae")]
    assert all(d.severity == rules.ERROR for d in diagnostics)


def test_bad_enum_is_reported(sample_schema):
    diagnostics = lint_file("broken_bad_enum.json", sample_schema)
    assert rule_ids(diagnostics) == ["invalid-enum", "invalid-enum"]
    fields = sorted(d.field for d in diagnostics)
    assert fields == ["ckpt_name", "sampler_name"]
    assert "allowed:" in diagnostics[0].message


# --------------------------------------------------------------------------
# individual rules on hand-built workflows
# --------------------------------------------------------------------------


def lint_nodes(nodes, sample_schema):
    return linter.lint_workflow(nodes, sample_schema)


def test_extraneous_input_is_a_warning(sample_schema):
    nodes = {
        "1": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 512, "height": 512, "batch_size": 1, "tile_size": 64},
        }
    }
    diagnostics = lint_nodes(nodes, sample_schema)
    assert rule_ids(diagnostics) == ["extraneous-input"]
    assert diagnostics[0].severity == rules.WARNING
    assert diagnostics[0].field == "tile_size"


def test_dangling_link_to_missing_node(sample_schema):
    nodes = {
        "1": {"class_type": "VAEDecode", "inputs": {"samples": ["99", 0], "vae": ["98", 0]}}
    }
    diagnostics = lint_nodes(nodes, sample_schema)
    assert rule_ids(diagnostics) == ["dangling-link", "dangling-link"]
    assert "does not exist" in diagnostics[0].message


def test_dangling_link_to_missing_output_slot(sample_schema):
    nodes = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd15.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "a photo of a cat", "clip": ["1", 7]}},
    }
    diagnostics = lint_nodes(nodes, sample_schema)
    assert rule_ids(diagnostics) == ["dangling-link"]
    assert "has 3 output(s)" in diagnostics[0].message


def test_type_mismatch_on_link(sample_schema):
    nodes = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd15.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "a photo of a cat", "clip": ["1", 0]}},
    }
    diagnostics = lint_nodes(nodes, sample_schema)
    assert rule_ids(diagnostics) == ["type-mismatch"]
    assert "expects CLIP" in diagnostics[0].message
    assert "produces MODEL" in diagnostics[0].message


def test_link_into_enum_slot_is_not_flagged(sample_schema):
    nodes = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ["2", 0]}},
        "2": {"class_type": "PreviewImage", "inputs": {"images": ["1", 0]}},
    }
    diagnostics = lint_nodes(nodes, sample_schema)
    # PreviewImage has no outputs, so the link out of it is dangling, but the
    # enum slot itself must not be reported as an invalid value.
    assert "invalid-enum" not in rule_ids(diagnostics)


def test_unknown_node_suppresses_downstream_noise(sample_schema):
    nodes = {
        "1": {"class_type": "TotallyMadeUpNode", "inputs": {"whatever": 1}},
    }
    diagnostics = lint_nodes(nodes, sample_schema)
    assert rule_ids(diagnostics) == ["unknown-node"]


def test_malformed_node_entries(sample_schema):
    nodes = {
        "1": "not a node",
        "2": {"inputs": {}},
        "3": {"class_type": "SaveImage", "inputs": []},
    }
    diagnostics = lint_nodes(nodes, sample_schema)
    assert rule_ids(diagnostics).count("malformed-node") == 3


def test_optional_inputs_are_not_required(sample_schema):
    nodes = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd15.safetensors"}},
        "2": {
            "class_type": "LoraLoader",
            "inputs": {
                "model": ["1", 0],
                "clip": ["1", 1],
                "lora_name": "line_art.safetensors",
                "strength_model": 0.8,
                "strength_clip": 0.8,
            },
        },
    }
    assert lint_nodes(nodes, sample_schema) == []


def test_int_output_is_accepted_by_float_input(sample_schema):
    spec = schema.normalize({
        "IntSource": {"input": {"required": {}}, "output": ["INT"]},
        "FloatSink": {"input": {"required": {"value": ["FLOAT"]}}, "output": []},
    })
    nodes = {
        "1": {"class_type": "IntSource", "inputs": {}},
        "2": {"class_type": "FloatSink", "inputs": {"value": ["1", 0]}},
    }
    assert linter.lint_workflow(nodes, spec) == []


def test_wildcard_input_accepts_anything(sample_schema):
    spec = schema.normalize({
        "Source": {"input": {"required": {}}, "output": ["IMAGE"]},
        "AnySink": {"input": {"required": {"value": ["*"]}}, "output": []},
    })
    nodes = {
        "1": {"class_type": "Source", "inputs": {}},
        "2": {"class_type": "AnySink", "inputs": {"value": ["1", 0]}},
    }
    assert linter.lint_workflow(nodes, spec) == []


def test_integer_node_ids_sort_numerically(sample_schema):
    nodes = {
        "10": {"class_type": "Nope", "inputs": {}},
        "2": {"class_type": "Nope", "inputs": {}},
    }
    diagnostics = lint_nodes(nodes, sample_schema)
    assert [d.node_id for d in diagnostics] == ["2", "10"]


# --------------------------------------------------------------------------
# orchestration helpers
# --------------------------------------------------------------------------


def test_strict_promotes_warnings(sample_schema):
    nodes = {
        "1": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 512, "height": 512, "batch_size": 1, "tile_size": 64},
        }
    }
    diagnostics = lint_nodes(nodes, sample_schema)
    assert linter.counts(diagnostics) == (0, 1)
    assert linter.counts(linter.apply_strict(diagnostics)) == (1, 0)


def test_anchor_formatting():
    diag = rules.Diagnostic(
        rules.ERROR, "invalid-enum", "5", "KSampler", "sampler_name", "boom"
    )
    assert diag.anchor("workflow.json") == "workflow.json:5:sampler_name"
    assert diag.anchor() == "5:sampler_name"


def test_diagnostics_are_hashable_and_deduplicate():
    args = (rules.ERROR, "invalid-enum", "5", "KSampler", "sampler_name", "boom")
    first, second = rules.Diagnostic(*args), rules.Diagnostic(*args)
    other = rules.Diagnostic(
        rules.ERROR, "invalid-enum", "6", "KSampler", "sampler_name", "boom"
    )
    assert first == second
    assert hash(first) == hash(second)
    assert len({first, second, other}) == 2


def test_ui_format_workflow_is_rejected(tmp_path):
    path = tmp_path / "ui.json"
    path.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
    with pytest.raises(linter.WorkflowError) as excinfo:
        linter.load_workflow(str(path))
    assert "Export (API)" in str(excinfo.value)


def test_prompt_wrapper_is_unwrapped(sample_schema, tmp_path):
    with open(fixture("valid_workflow.json"), encoding="utf-8") as handle:
        inner = json.load(handle)
    path = tmp_path / "wrapped.json"
    path.write_text(json.dumps({"prompt": inner}), encoding="utf-8")
    workflow = linter.load_workflow(str(path))
    assert linter.lint_workflow(workflow, sample_schema) == []


# --------------------------------------------------------------------------
# CLI, offline
# --------------------------------------------------------------------------


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_clean_workflow_exits_zero():
    code, out, _ = run_cli(
        ["--schema-cache", OBJECT_INFO, fixture("valid_workflow.json")]
    )
    assert code == cli.EXIT_OK
    assert "clean" in out


def test_cli_errors_exit_one():
    code, out, _ = run_cli(
        ["--schema-cache", OBJECT_INFO, fixture("broken_bad_enum.json")]
    )
    assert code == cli.EXIT_ERRORS
    assert "broken_bad_enum.json:1:ckpt_name" in out
    assert "invalid-enum" in out


def test_cli_json_output_is_machine_readable():
    code, out, _ = run_cli(
        ["--schema-cache", OBJECT_INFO, "--json", fixture("broken_missing_required.json")]
    )
    assert code == cli.EXIT_ERRORS
    payload = json.loads(out)
    assert payload["summary"]["errors"] == 3
    assert payload["summary"]["warnings"] == 0
    assert payload["files"][0]["diagnostics"][0]["rule"] == "missing-required"


def test_cli_strict_turns_warnings_into_failure(tmp_path):
    path = tmp_path / "extra.json"
    path.write_text(
        json.dumps({
            "1": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 512, "height": 512, "batch_size": 1, "seed": 7},
            }
        }),
        encoding="utf-8",
    )
    code, _, _ = run_cli(["--schema-cache", OBJECT_INFO, str(path)])
    assert code == cli.EXIT_OK
    code, _, _ = run_cli(["--schema-cache", OBJECT_INFO, "--strict", str(path)])
    assert code == cli.EXIT_ERRORS


def test_cli_multiple_files():
    code, out, _ = run_cli([
        "--schema-cache", OBJECT_INFO,
        fixture("valid_workflow.json"),
        fixture("broken_unknown_node.json"),
    ])
    assert code == cli.EXIT_ERRORS
    assert "in 2 files" in out


def test_cli_reports_the_same_path_in_text_and_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nested = tmp_path / "workflows"
    nested.mkdir()
    path = nested / "bad.json"
    path.write_text(
        json.dumps({"7": {"class_type": "SuperSaveImage", "inputs": {}}}),
        encoding="utf-8",
    )
    expected = os.path.join("workflows", "bad.json")
    code, text_out, _ = run_cli(["--schema-cache", OBJECT_INFO, str(path)])
    assert code == cli.EXIT_ERRORS
    assert text_out.startswith(expected + ":7:class_type")
    code, json_out, _ = run_cli(["--schema-cache", OBJECT_INFO, "--json", str(path)])
    assert json.loads(json_out)["files"][0]["path"] == expected


def test_cli_keeps_linting_after_an_unreadable_file(tmp_path):
    broken = tmp_path / "not_json.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    code, out, err = run_cli([
        "--schema-cache", OBJECT_INFO,
        str(broken),
        fixture("broken_unknown_node.json"),
        fixture("valid_workflow.json"),
    ])
    # The bad file is reported once...
    assert "not valid JSON" in err
    # ...and the two readable files were still linted.
    assert "unknown-node" in out
    assert "in 2 files" in out
    assert "1 file skipped (unreadable)" in out
    # An unchecked file outranks lint errors: usage exit code.
    assert code == cli.EXIT_USAGE


def test_cli_json_lists_unreadable_files(tmp_path):
    broken = tmp_path / "not_json.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    code, out, _ = run_cli([
        "--schema-cache", OBJECT_INFO, "--json",
        str(broken),
        fixture("valid_workflow.json"),
    ])
    assert code == cli.EXIT_USAGE
    payload = json.loads(out)
    assert payload["summary"]["files"] == 1
    assert payload["summary"]["unreadable"] == 1
    assert len(payload["unreadable"]) == 1
    assert "not valid JSON" in payload["unreadable"][0]["error"]


def test_cli_missing_workflow_argument_exits_two():
    code, _, err = run_cli(["--schema-cache", OBJECT_INFO])
    assert code == cli.EXIT_USAGE
    assert "at least one workflow file" in err


def test_cli_unreadable_workflow_exits_two(tmp_path):
    code, _, err = run_cli(
        ["--schema-cache", OBJECT_INFO, str(tmp_path / "nope.json")]
    )
    assert code == cli.EXIT_USAGE
    assert "cannot read workflow" in err


def test_cli_unreachable_server_exits_two(tmp_path):
    code, _, err = run_cli([
        "--server", "http://127.0.0.1:9",
        "--timeout", "1",
        fixture("valid_workflow.json"),
    ])
    assert code == cli.EXIT_USAGE
    assert "--schema-cache" in err


def test_cli_list_rules():
    code, out, _ = run_cli(["--list-rules"])
    assert code == cli.EXIT_OK
    for rule_id, _, _ in rules.RULE_DOCS:
        assert rule_id in out
