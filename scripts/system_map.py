from __future__ import annotations

import argparse
import ast
import fnmatch
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "docs" / "architecture" / "system-map.json"
MARKDOWN_PATH = ROOT / "docs" / "architecture" / "system-map.md"
MERMAID_PATH = ROOT / "docs" / "architecture" / "system-map.mmd"


class SystemMapError(ValueError):
    pass


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _load() -> dict[str, Any]:
    try:
        value = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemMapError(f"system map is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemMapError("system map root must be an object")
    return value


def _python_symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    symbols: set[str] = set()

    def walk(body: Iterable[ast.stmt], prefix: str = "") -> None:
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{prefix}.{node.name}" if prefix else node.name
                symbols.add(node.name)
                symbols.add(qualified)
                walk(node.body, qualified)

    walk(tree.body)
    return symbols


def _require_text(value: Any, field: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} must be non-empty text")


def _require_text_list(
    value: Any, field: str, errors: list[str], *, allow_empty: bool = False
) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        errors.append(f"{field} must be a list of non-empty strings")
        return []
    if not allow_empty and not value:
        errors.append(f"{field} must not be empty")
    if len(value) != len(set(value)):
        errors.append(f"{field} contains duplicates")
    return list(value)


def _matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def validate(value: dict[str, Any]) -> None:
    errors: list[str] = []
    if value.get("schema_version") != "substar.system-map.v1":
        errors.append("schema_version must be substar.system-map.v1")
    _require_text(value.get("title"), "title", errors)
    _require_text(value.get("authority"), "authority", errors)

    contracts = value.get("contracts")
    modules = value.get("modules")
    flows = value.get("flows")
    coverage = value.get("coverage")
    if not isinstance(contracts, dict) or not contracts:
        errors.append("contracts must be a non-empty object")
        contracts = {}
    if not isinstance(modules, dict) or not modules:
        errors.append("modules must be a non-empty object")
        modules = {}
    if not isinstance(flows, dict) or not flows:
        errors.append("flows must be a non-empty object")
        flows = {}
    if not isinstance(coverage, dict):
        errors.append("coverage must be an object")
        coverage = {}

    for contract_id, contract in contracts.items():
        prefix = f"contracts.{contract_id}"
        if not isinstance(contract, dict):
            errors.append(f"{prefix} must be an object")
            continue
        _require_text(contract.get("title"), f"{prefix}.title", errors)
        _require_text(contract.get("authority"), f"{prefix}.authority", errors)
        _require_text(contract.get("description"), f"{prefix}.description", errors)
        schema = contract.get("schema")
        if schema is not None:
            _require_text(schema, f"{prefix}.schema", errors)
            if isinstance(schema, str) and not (ROOT / schema).is_file():
                errors.append(f"{prefix}.schema does not exist: {schema}")

    owned_paths: set[str] = set()
    python_cache: dict[str, set[str]] = {}
    contract_producers: dict[str, set[str]] = defaultdict(set)
    contract_consumers: dict[str, set[str]] = defaultdict(set)
    required_module_fields = {
        "title",
        "layer",
        "role",
        "must_not",
        "code",
        "inputs",
        "outputs",
        "calls",
        "invariants",
        "failure_modes",
        "recovery",
        "tests",
        "change_impact",
    }
    for module_id, module in modules.items():
        prefix = f"modules.{module_id}"
        if not isinstance(module, dict):
            errors.append(f"{prefix} must be an object")
            continue
        missing = required_module_fields - set(module)
        extra = set(module) - required_module_fields
        if missing:
            errors.append(f"{prefix} missing fields: {sorted(missing)}")
        if extra:
            errors.append(f"{prefix} has unknown fields: {sorted(extra)}")
        for field in ("title", "layer", "role"):
            _require_text(module.get(field), f"{prefix}.{field}", errors)
        _require_text_list(module.get("must_not"), f"{prefix}.must_not", errors)
        _require_text_list(module.get("invariants"), f"{prefix}.invariants", errors)
        _require_text_list(
            module.get("failure_modes"), f"{prefix}.failure_modes", errors
        )
        inputs = _require_text_list(
            module.get("inputs"), f"{prefix}.inputs", errors, allow_empty=True
        )
        outputs = _require_text_list(
            module.get("outputs"), f"{prefix}.outputs", errors, allow_empty=True
        )
        calls = _require_text_list(
            module.get("calls"), f"{prefix}.calls", errors, allow_empty=True
        )
        for contract_id in inputs + outputs:
            if contract_id not in contracts:
                errors.append(f"{prefix} references unknown contract: {contract_id}")
        for contract_id in inputs:
            contract_consumers[contract_id].add(module_id)
        for contract_id in outputs:
            contract_producers[contract_id].add(module_id)
        for target in calls:
            if target not in modules:
                errors.append(f"{prefix}.calls references unknown module: {target}")

        code = module.get("code")
        if not isinstance(code, list) or not code:
            errors.append(f"{prefix}.code must be a non-empty list")
        else:
            for index, reference in enumerate(code):
                code_prefix = f"{prefix}.code[{index}]"
                if not isinstance(reference, dict) or set(reference) != {
                    "path",
                    "symbols",
                }:
                    errors.append(
                        f"{code_prefix} must contain exactly path and symbols"
                    )
                    continue
                path_text = reference.get("path")
                _require_text(path_text, f"{code_prefix}.path", errors)
                symbols = _require_text_list(
                    reference.get("symbols"),
                    f"{code_prefix}.symbols",
                    errors,
                    allow_empty=True,
                )
                if not isinstance(path_text, str):
                    continue
                path = ROOT / path_text
                if not path.is_file():
                    errors.append(f"{code_prefix}.path does not exist: {path_text}")
                    continue
                owned_paths.add(Path(path_text).as_posix())
                if path.suffix == ".py" and symbols:
                    available = python_cache.setdefault(
                        path_text, _python_symbols(path)
                    )
                    for symbol in symbols:
                        if symbol not in available:
                            errors.append(
                                f"{code_prefix} symbol does not exist: {symbol}"
                            )

        tests = _require_text_list(module.get("tests"), f"{prefix}.tests", errors)
        for test in tests:
            if not (ROOT / test).is_file():
                errors.append(f"{prefix}.tests does not exist: {test}")

        recovery = module.get("recovery")
        if not isinstance(recovery, dict) or set(recovery) != {
            "strategy",
            "reuses",
            "restarts",
            "terminal",
        }:
            errors.append(
                f"{prefix}.recovery must contain strategy, reuses, restarts and terminal"
            )
        else:
            _require_text(recovery.get("strategy"), f"{prefix}.recovery.strategy", errors)
            _require_text(recovery.get("terminal"), f"{prefix}.recovery.terminal", errors)
            _require_text_list(
                recovery.get("reuses"),
                f"{prefix}.recovery.reuses",
                errors,
                allow_empty=True,
            )
            _require_text_list(
                recovery.get("restarts"),
                f"{prefix}.recovery.restarts",
                errors,
                allow_empty=True,
            )

        impact = module.get("change_impact")
        if not isinstance(impact, dict) or set(impact) != {"modules", "contracts"}:
            errors.append(
                f"{prefix}.change_impact must contain modules and contracts"
            )
        else:
            impact_modules = _require_text_list(
                impact.get("modules"),
                f"{prefix}.change_impact.modules",
                errors,
                allow_empty=True,
            )
            impact_contracts = _require_text_list(
                impact.get("contracts"),
                f"{prefix}.change_impact.contracts",
                errors,
                allow_empty=True,
            )
            for target in impact_modules:
                if target not in modules:
                    errors.append(
                        f"{prefix}.change_impact references unknown module: {target}"
                    )
            for contract_id in impact_contracts:
                if contract_id not in contracts:
                    errors.append(
                        f"{prefix}.change_impact references unknown contract: {contract_id}"
                    )

    for contract_id in contracts:
        if not contract_producers[contract_id]:
            errors.append(f"contract has no registered producer: {contract_id}")
        if not contract_consumers[contract_id]:
            errors.append(f"contract has no registered consumer: {contract_id}")

    for module_id, module in modules.items():
        if not isinstance(module, dict) or module.get("layer") != "worker":
            continue
        result_contracts = {
            contract_id
            for contract_id in module.get("outputs", [])
            if contract_id.endswith("_result")
            and contract_id not in {"semantic_grouping_result"}
        }
        for contract_id in result_contracts:
            finalizers = {
                consumer
                for consumer in contract_consumers[contract_id]
                if modules.get(consumer, {}).get("layer") == "finalizer"
            }
            if not finalizers:
                errors.append(
                    f"worker {module_id} result {contract_id} has no finalizer consumer"
                )

    for flow_id, flow in flows.items():
        prefix = f"flows.{flow_id}"
        if not isinstance(flow, dict) or set(flow) != {
            "title",
            "modules",
            "success",
            "tests",
        }:
            errors.append(
                f"{prefix} must contain title, modules, success and tests"
            )
            continue
        _require_text(flow.get("title"), f"{prefix}.title", errors)
        _require_text(flow.get("success"), f"{prefix}.success", errors)
        flow_modules = _require_text_list(
            flow.get("modules"), f"{prefix}.modules", errors
        )
        for module_id in flow_modules:
            if module_id not in modules:
                errors.append(f"{prefix} references unknown module: {module_id}")
        flow_tests = _require_text_list(flow.get("tests"), f"{prefix}.tests", errors)
        for test in flow_tests:
            if not (ROOT / test).is_file():
                errors.append(f"{prefix}.tests does not exist: {test}")

    production_globs = _require_text_list(
        coverage.get("production_globs"), "coverage.production_globs", errors
    )
    ignored_globs = _require_text_list(
        coverage.get("ignored_globs"),
        "coverage.ignored_globs",
        errors,
        allow_empty=True,
    )
    production_paths: set[str] = set()
    for pattern in production_globs:
        for path in ROOT.glob(pattern):
            if path.is_file():
                production_paths.add(_relative(path))
    ignored_paths = {
        path for path in production_paths if _matches_any(path, ignored_globs)
    }
    uncovered = sorted(production_paths - ignored_paths - owned_paths)
    if uncovered:
        errors.append("production files lack system-map ownership: " + ", ".join(uncovered))

    generated = value.get("generated_files")
    expected_generated = {
        _relative(MARKDOWN_PATH),
        _relative(MERMAID_PATH),
    }
    if not isinstance(generated, list) or set(generated) != expected_generated:
        errors.append(f"generated_files must be {sorted(expected_generated)}")

    if errors:
        raise SystemMapError("\n".join(f"- {error}" for error in errors))


def _cell(values: Iterable[str]) -> str:
    rows = list(values)
    return "<br>".join(f"`{row}`" for row in rows) if rows else "—"


def _bullets(values: Iterable[str]) -> str:
    return "\n".join(f"- {value}" for value in values)


def render_mermaid(value: dict[str, Any]) -> str:
    modules = value["modules"]
    layers: dict[str, list[str]] = defaultdict(list)
    for module_id, module in modules.items():
        layers[module["layer"]].append(module_id)
    lines = ["flowchart LR"]
    for layer in sorted(layers):
        lines.append(f'  subgraph layer_{layer}["{layer}"]')
        for module_id in sorted(layers[layer]):
            title = modules[module_id]["title"].replace('"', "'")
            lines.append(f'    {module_id}["{title}"]')
        lines.append("  end")
    for source_id, module in modules.items():
        for target_id in module["calls"]:
            lines.append(f"  {source_id} --> {target_id}")
    return "\n".join(lines) + "\n"


def render_markdown(value: dict[str, Any], mermaid: str) -> str:
    modules = value["modules"]
    contracts = value["contracts"]
    producers: dict[str, list[str]] = defaultdict(list)
    consumers: dict[str, list[str]] = defaultdict(list)
    for module_id, module in modules.items():
        for contract_id in module["outputs"]:
            producers[contract_id].append(module_id)
        for contract_id in module["inputs"]:
            consumers[contract_id].append(module_id)

    lines = [
        "# Substar executable system map",
        "",
        "> Generated from `docs/architecture/system-map.json` by `scripts/system_map.py`. Do not edit this file by hand.",
        "",
        "This is the current human- and machine-consumable architecture authority. It covers process ownership, browser API callers, backend APIs, task runtime, provider connectors, Workers, Finalizers, editor services, settings, glossary, export and packaging.",
        "",
        "## Mandatory change rule",
        "",
        _bullets(value["change_policy"]["rules"]),
        "",
        "A canonical contract change is incomplete until all of these are checked: "
        + ", ".join(
            f"`{item}`" for item in value["change_policy"]["contract_change_checks"]
        )
        + ".",
        "",
        "## Whole-system call graph",
        "",
        "```mermaid",
        mermaid.rstrip(),
        "```",
        "",
        "## Module index",
        "",
        "| Module | Layer | Responsibility | Code | Inputs | Outputs | Calls |",
        "|---|---|---|---|---|---|---|",
    ]
    for module_id, module in modules.items():
        code = [reference["path"] for reference in module["code"]]
        lines.append(
            f"| `{module_id}` | `{module['layer']}` | {module['role']} | "
            f"{_cell(code)} | {_cell(module['inputs'])} | {_cell(module['outputs'])} | {_cell(module['calls'])} |"
        )

    lines.extend(
        [
            "",
            "## Contract producer/consumer index",
            "",
            "| Contract | Authority | Schema | Producers | Consumers |",
            "|---|---|---|---|---|",
        ]
    )
    for contract_id, contract in contracts.items():
        schema = f"`{contract['schema']}`" if contract["schema"] else "domain validation"
        lines.append(
            f"| `{contract_id}` | `{contract['authority']}` | {schema} | "
            f"{_cell(producers[contract_id])} | {_cell(consumers[contract_id])} |"
        )

    lines.extend(["", "## End-to-end flows", ""])
    for flow_id, flow in value["flows"].items():
        lines.extend(
            [
                f"### {flow['title']} (`{flow_id}`)",
                "",
                " → ".join(f"`{module_id}`" for module_id in flow["modules"]),
                "",
                f"Success condition: {flow['success']}",
                "",
                "Tests: " + ", ".join(f"`{test}`" for test in flow["tests"]),
                "",
            ]
        )

    lines.extend(["## Module details", ""])
    for module_id, module in modules.items():
        lines.extend(
            [
                f"### {module['title']} (`{module_id}`)",
                "",
                f"Layer: `{module['layer']}`",
                "",
                module["role"],
                "",
                "Code:",
                "",
            ]
        )
        for reference in module["code"]:
            suffix = (
                " — " + ", ".join(f"`{symbol}`" for symbol in reference["symbols"])
                if reference["symbols"]
                else ""
            )
            lines.append(f"- `{reference['path']}`{suffix}")
        lines.extend(
            [
                "",
                "Must not:",
                "",
                _bullets(module["must_not"]),
                "",
                "Invariants:",
                "",
                _bullets(module["invariants"]),
                "",
                "Failure modes:",
                "",
                _bullets(module["failure_modes"]),
                "",
                f"Recovery: {module['recovery']['strategy']}",
                "",
                f"Reuses: {_cell(module['recovery']['reuses'])}; restarts: {_cell(module['recovery']['restarts'])}; terminal behavior: {module['recovery']['terminal']}",
                "",
                f"Tests: {', '.join(f'`{test}`' for test in module['tests'])}",
                "",
                f"Change impact modules: {_cell(module['change_impact']['modules'])}",
                "",
                f"Change impact contracts: {_cell(module['change_impact']['contracts'])}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def generate(*, check: bool) -> None:
    value = _load()
    validate(value)
    mermaid = render_mermaid(value)
    markdown = render_markdown(value, mermaid)
    expected = {MERMAID_PATH: mermaid, MARKDOWN_PATH: markdown}
    if check:
        stale = [
            _relative(path)
            for path, content in expected.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != content
        ]
        if stale:
            raise SystemMapError(
                "generated system map is stale: "
                + ", ".join(stale)
                + "; run python scripts/system_map.py"
            )
        return
    for path, content in expected.items():
        path.write_text(content, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and generate the Substar system map")
    parser.add_argument("--check", action="store_true", help="fail when generated files are stale")
    arguments = parser.parse_args()
    try:
        generate(check=arguments.check)
    except SystemMapError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
