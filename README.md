# comfy-lint

**You queued a workflow, waited 12 minutes on the GPU, and it failed because a node type didn't exist.**

`comfy-lint` catches that in well under a second.

It reads your ComfyUI API-format workflow JSON, compares it against the `/object_info` schema of a real ComfyUI install, and tells you exactly which node and which field is wrong — before anything is queued, before a model is loaded, before a single step is sampled.

- Pure standard library. No dependencies. Python 3.9+.
- Works offline in CI with a cached schema — no ComfyUI process required.
- Human output for your terminal, `--json` for your pipeline.

---

## Install

PyPI release pending — install from git for now:

```bash
pip install git+https://github.com/tangyistudio/comfy-lint
```

Or from a checkout:

```bash
pip install .
```

## Usage

```bash
comfy-lint my_workflow.json
```

That's it. `comfy-lint` reads the schema from `http://127.0.0.1:8188` by default; point it elsewhere with `--server`.

```bash
comfy-lint my_workflow.json --server http://comfy.lan:8188
```

> The file must be **API format** — in ComfyUI, use *Workflow > Export (API)*. If you hand it a saved UI workflow, `comfy-lint` says so instead of producing nonsense.

## Sample output

```console
$ comfy-lint broken_workflow.json --server http://127.0.0.1:8188
broken_workflow.json:2:clip  error  type-mismatch  CLIPTextEncode: 'clip' expects CLIP but node 1 output #0 (CheckpointLoaderSimple) produces MODEL
broken_workflow.json:4:tile_size  warning  extraneous-input  EmptyLatentImage: unknown input 'tile_size' for EmptyLatentImage - it will be ignored
broken_workflow.json:5:denoise  error  missing-required  KSampler: missing required input 'denoise' (expects FLOAT)
broken_workflow.json:5:sampler_name  error  invalid-enum  KSampler: 'dpmpp_3m_sde' is not a valid value for 'sampler_name'; allowed: 'euler', 'euler_ancestral', 'heun', 'dpmpp_2m', 'dpmpp_2m_sde', 'ddim', ... (1 more)
broken_workflow.json:6:vae  error  dangling-link  VAEDecode: 'vae' links to node '12', which does not exist in this workflow
broken_workflow.json:7:class_type  error  unknown-node  SuperSaveImage: unknown node class_type 'SuperSaveImage' - not installed on this ComfyUI (did you mean: SaveImage?)

5 errors, 1 warning in 1 file
schema: http://127.0.0.1:8188/object_info
```

Anchors are `file:node_id:field`, so the node id maps straight onto the ids in your JSON. The file part is the path relative to your working directory — the same string in `--json` — so an editor or a CI annotation can jump straight to it.

A clean run is quiet:

```console
$ comfy-lint my_workflow.json
1 file clean (0 issues)
schema: http://127.0.0.1:8188/object_info
```

## Rules

| Rule | Severity | What it catches |
| --- | --- | --- |
| `unknown-node` | error | `class_type` isn't installed on the target server — the classic "missing custom node" failure. Suggests close matches. |
| `missing-required` | error | A required input is absent, so ComfyUI rejects the prompt. |
| `invalid-enum` | error | A literal value that isn't one of a COMBO widget's choices — a checkpoint, LoRA or sampler name that doesn't exist. |
| `dangling-link` | error | An input links to a node id that isn't in the workflow, or to an output slot the upstream node doesn't have. |
| `type-mismatch` | error | A link whose upstream output type doesn't match the input slot (e.g. `MODEL` into a `CLIP` input). |
| `extraneous-input` | warning | An input the node class doesn't declare; ComfyUI silently ignores it, which is usually a typo or a version drift. |
| `malformed-node` | error | The node entry isn't a valid API-format object at all. |

Print the table any time with `comfy-lint --list-rules`.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Clean (warnings only, unless `--strict`) |
| `1` | Errors found |
| `2` | Usage error, unreadable workflow, or the schema could not be fetched |

`--strict` promotes every warning to an error, so `extraneous-input` also fails the build.

When several files are given, an unreadable one is reported on stderr and the remaining files are still linted — you get the whole picture in one run. Exit code `2` wins over `1` in that case, since a file that was never checked is the more urgent problem.

## Offline mode / CI

CI runners don't have a GPU or a ComfyUI process. Cache the schema once, commit it, and lint forever without a server:

```bash
# On a machine that can reach ComfyUI: fetch and save the schema.
comfy-lint my_workflow.json --schema-cache .comfy/object_info.json

# Anywhere else (CI, pre-commit, an airplane): the cache is used, no network.
comfy-lint my_workflow.json --schema-cache .comfy/object_info.json
```

If `--schema-cache` points at a file that exists, it is used and **no server is contacted**. If the file doesn't exist, the schema is fetched from `--server` and written there. Use `--refresh-cache` to refetch after installing new custom nodes.

Both options also read from the environment, which keeps CI configs short:

```bash
export COMFY_LINT_SCHEMA_CACHE=.comfy/object_info.json
export COMFY_LINT_SERVER=http://127.0.0.1:8188
```

### GitHub Actions

```yaml
name: lint-workflows
on: [push, pull_request]

jobs:
  comfy-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      # PyPI release pending; install from git.
      - run: pip install git+https://github.com/tangyistudio/comfy-lint
      - run: comfy-lint workflows/*.json --schema-cache .comfy/object_info.json --strict
```

### pre-commit

```yaml
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: comfy-lint
        name: comfy-lint
        entry: comfy-lint --schema-cache .comfy/object_info.json --strict
        language: python
        # PyPI release pending; install from git.
        additional_dependencies: ["comfy-lint @ git+https://github.com/tangyistudio/comfy-lint"]
        files: ^workflows/.*\.json$
```

`comfy-lint` accepts multiple files in one invocation, which is exactly what pre-commit passes it.

### Machine-readable output

```bash
comfy-lint my_workflow.json --schema-cache .comfy/object_info.json --json
```

```json
{
  "tool": "comfy-lint",
  "version": "0.1.0",
  "schema_source": ".comfy/object_info.json",
  "strict": false,
  "summary": { "files": 1, "errors": 2, "warnings": 0, "unreadable": 0 },
  "files": [
    {
      "path": "my_workflow.json",
      "errors": 2,
      "warnings": 0,
      "diagnostics": [
        {
          "severity": "error",
          "rule": "invalid-enum",
          "node_id": "5",
          "class_type": "KSampler",
          "field": "sampler_name",
          "message": "'dpmpp_3m_sde' is not a valid value for 'sampler_name'; allowed: 'euler', ..."
        }
      ]
    }
  ],
  "unreadable": []
}
```

`unreadable` lists any file that could not be read or parsed (`{"path": ..., "error": ...}`) so a `--json` consumer can tell "clean" apart from "never checked".

## Options

```
comfy-lint [-h] [--server URL] [--schema-cache PATH] [--refresh-cache]
           [--timeout SECONDS] [--json] [--strict] [--color {auto,always,never}]
           [--list-rules] [--version]
           workflow.json [workflow.json ...]
```

## Use it as a library

```python
from comfy_lint import lint_workflow, load_schema
from comfy_lint.linter import load_workflow

schema = load_schema(cache_path=".comfy/object_info.json")
for diagnostic in lint_workflow(load_workflow("my_workflow.json"), schema):
    print(diagnostic.severity, diagnostic.anchor("my_workflow.json"), diagnostic.message)
```

Every rule in `comfy_lint.rules` is a plain function `rule(node_id, node, ctx) -> list[Diagnostic]`, so adding a project-specific check is a dozen lines and a tuple entry.

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/
```

The test suite is fully offline: it lints the fixtures in `tests/fixtures/` against a small hand-written `object_info` sample.

## License

MIT — see [LICENSE](LICENSE).

---

Built by [Tangyi Studio](https://github.com/tangyistudio)
