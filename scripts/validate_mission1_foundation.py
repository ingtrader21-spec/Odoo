#!/usr/bin/env python3
"""Mission 1 foundation gate: module authority, domain ownership, roles, controller governance,
Middleware boundary, provider-path classification, provisioning/state models and inventory parity.

Everything is verified against the source tree, not against the configs alone:

- every installable module is classified once (`config/odoo-module-authority.v1.json`) and no
  module is missing or invented; the recorded duplicate-model exception is the only duplicate;
- every domain has exactly one canonical owner whose model exists in the named module, and no
  compatibility/transitional/retire-candidate module declares a canonical owner model;
- every canonical role maps to groups that exist in the security XML, the membership model
  exists, and the cc groups the authority matrix relies on are present;
- the committed controller inventory equals a fresh offline regeneration, every inventory route
  is classified with a sudo justification, no route is REJECT_*, every MIDDLEWARE_ONLY route
  carries controller service identity, and every auth="none" route is either MIDDLEWARE_ONLY,
  PUBLIC_INTENTIONAL or LEGACY;
- every outbound HTTP site in the addons is classified and no forbidden provider transport is
  imported; the legacy design outbox cron stays inactive and unloaded;
- every declared state machine matches the model's selection values and, where declared, the
  code transition/command tables.

Exit 0 prints ``ODOO_MISSION1_FOUNDATION=PASS``; any finding prints one ``ERROR=`` line per
finding and exits 1.
"""
from __future__ import annotations

import ast
import csv
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADDONS = ROOT / "custom-addons"
CONFIG = ROOT / "config"
INVENTORY = ROOT / "docs/reconciliation/ODOO-ENDPOINT-INVENTORY.csv"
GENERATOR = ROOT / "scripts/generate_reconciliation_ledgers.py"
MODULE_CLASSES = {"CANONICAL", "SUPPORTING", "COMPATIBILITY", "LEGACY", "TRANSITIONAL", "DEPRECATED", "RETIRE_CANDIDATE"}
NON_AUTHORITY_CLASSES = {"COMPATIBILITY", "LEGACY", "TRANSITIONAL", "DEPRECATED", "RETIRE_CANDIDATE"}
ROUTE_CLASSES = {"PUBLIC_INTENTIONAL", "USER_AUTHENTICATED", "MIDDLEWARE_ONLY", "SERVICE_AUTHENTICATED", "INTERNAL", "LEGACY"}
SUDO_CLASSES = {"NONE", "SERVICE_IDENTITY_EXECUTION", "GOVERNED_PARAMETER_READ", "USER_SCOPE_THEN_SYSTEM", "REVIEW_REQUIRED"}
OUTBOUND = re.compile(r"urllib\.request\.(urlopen|Request|build_opener)|requests\.(post|get|put|patch|delete)\(|http\.client\.HTTPS?Connection")


def load(name: str) -> dict:
    return json.loads((CONFIG / name).read_text(encoding="utf-8"))


def manifests() -> dict[str, dict]:
    out = {}
    for manifest in sorted(ADDONS.glob("*/__manifest__.py")):
        out[manifest.parent.name] = ast.literal_eval(manifest.read_text(encoding="utf-8"))
    return out


def model_declarations() -> dict[str, list[tuple[str, str]]]:
    """model name -> [(module, file)] for every class that declares ``_name`` without inheriting itself."""
    owners: dict[str, list[tuple[str, str]]] = {}
    for path in ADDONS.rglob("*.py"):
        if "tests" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            fields: dict[str, object] = {}
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id in {"_name", "_inherit"}:
                    try:
                        fields[stmt.targets[0].id] = ast.literal_eval(stmt.value)
                    except (ValueError, TypeError):
                        fields[stmt.targets[0].id] = None
            name = fields.get("_name")
            inherit = fields.get("_inherit")
            inherits_self = name is not None and (inherit == name or (isinstance(inherit, list) and name in inherit))
            if isinstance(name, str) and not inherits_self:
                owners.setdefault(name, []).append((path.relative_to(ADDONS).parts[0], path.relative_to(ADDONS).as_posix()))
    return owners


def selection_values(module: str, model: str, field: str) -> list[str] | None:
    """Selection keys of ``field`` on ``model`` as declared in ``module`` (resolving module constants)."""
    for path in (ADDONS / module).rglob("*.py"):
        if "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if f'"{model}"' not in source and f"'{model}'" not in source:
            continue
        tree = ast.parse(source)
        constants: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                try:
                    constants[node.targets[0].id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass
        for cls in [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]:
            names = [ast.literal_eval(s.value) for s in cls.body if isinstance(s, ast.Assign) and isinstance(s.targets[0], ast.Name) and s.targets[0].id in {"_name", "_inherit"} and isinstance(s.value, ast.Constant)]
            if model not in names:
                continue
            for stmt in cls.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == field:
                    call = stmt.value
                    if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "Selection" and call.args:
                        arg = call.args[0]
                        try:
                            options = ast.literal_eval(arg)
                        except (ValueError, TypeError):
                            options = constants.get(getattr(arg, "id", "")) if isinstance(arg, ast.Name) else None
                        if isinstance(options, list):
                            return [option[0] for option in options]
    return None


SAFE_CONSTRUCTORS = {"set", "frozenset", "dict", "list", "tuple", "sorted"}


def _pure(node: ast.AST) -> bool:
    """Only literals, names, comprehensions, containers and empty-container constructors such as
    ``set()``: no other calls, attributes or imports."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if not (isinstance(child.func, ast.Name) and child.func.id in SAFE_CONSTRUCTORS and not child.keywords):
                return False
        elif isinstance(child, (ast.Attribute, ast.Import, ast.ImportFrom, ast.Lambda, ast.Await, ast.Yield)):
            return False
    return True


def module_constant(module: str, constant: str) -> object:
    """Evaluate a module-level constant, following pure assignments it depends on (e.g. a dict
    built from a set comprehension over another constant)."""
    for path in (ADDONS / module).rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {node.targets[0].id for node in tree.body if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)}
        if constant not in names:
            continue
        namespace: dict[str, object] = {"__builtins__": {name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name) for name in SAFE_CONSTRUCTORS}}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and _pure(node.value):
                try:
                    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)  # noqa: S102 - pure literal assignments only
                except Exception:  # noqa: BLE001 - a dependency that is not pure leaves the name undefined
                    continue
        return namespace.get(constant, "<dynamic>")
    return None


def inherit_declarations() -> dict[str, set[str]]:
    """model name -> modules that extend it with ``_inherit``."""
    extenders: dict[str, set[str]] = {}
    for path in ADDONS.rglob("*.py"):
        if "tests" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == "_inherit":
                        try:
                            value = ast.literal_eval(stmt.value)
                        except (ValueError, TypeError):
                            continue
                        for name in ([value] if isinstance(value, str) else value):
                            extenders.setdefault(name, set()).add(path.relative_to(ADDONS).parts[0])
    return extenders


def group_ids() -> set[str]:
    ids: set[str] = set()
    for path in list(ADDONS.glob("*/security/*.xml")) + list(ADDONS.glob("*/data/*.xml")):
        module = path.relative_to(ADDONS).parts[0]
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r'<record[^>]*model="res\.groups"[^>]*id="([^"]+)"|<record[^>]*id="([^"]+)"[^>]*model="res\.groups"', text):
            ids.add(f"{module}.{match.group(1) or match.group(2)}")
    return ids


def check_modules(errors: list[str]) -> dict:
    authority = load("odoo-module-authority.v1.json")
    installed = {name for name, manifest in manifests().items() if manifest.get("installable", True)}
    declared = set(authority["modules"])
    for name in sorted(installed - declared):
        errors.append(f"unclassified module: {name}")
    for name in sorted(declared - installed):
        errors.append(f"module authority names a module that does not exist or is not installable: {name}")
    for name, entry in authority["modules"].items():
        if entry.get("classification") not in MODULE_CLASSES:
            errors.append(f"module {name} has an unknown classification {entry.get('classification')!r}")
        if not entry.get("rationale") or not entry.get("domain"):
            errors.append(f"module {name} lacks a domain or rationale")
    if authority.get("module_count") != len(authority["modules"]):
        errors.append("module_count does not match the module table")
    owners = model_declarations()
    duplicates = {model: decl for model, decl in owners.items() if len({module for module, _ in decl}) > 1}
    exceptions = authority["policy"].get("duplicate_model_name_exceptions", {})
    for model, decl in sorted(duplicates.items()):
        modules = sorted({module for module, _ in decl})
        if model not in exceptions:
            errors.append(f"duplicate model authority: {model} declared by {modules}")
        else:
            expected = {exceptions[model]["owner"], exceptions[model]["duplicate"]}
            if set(modules) != expected:
                errors.append(f"duplicate model exception for {model} names {sorted(expected)} but source declares {modules}")
    for model in exceptions:
        if model not in duplicates:
            errors.append(f"duplicate model exception for {model} is stale: no duplicate declaration remains")
    return authority


def check_domains(errors: list[str], authority: dict) -> dict:
    ownership = load("odoo-domain-ownership.v1.json")
    owners = model_declarations()
    classes = {name: entry["classification"] for name, entry in authority["modules"].items()}
    for domain, spec in ownership["domains"].items():
        canonical = spec.get("canonical_owner")
        if not canonical:
            errors.append(f"domain {domain} has no canonical owner")
            continue
        module, model = canonical["module"], canonical["model"].split(" ")[0]
        if classes.get(module) != "CANONICAL":
            errors.append(f"domain {domain}: canonical owner module {module} is classified {classes.get(module)!r}, not CANONICAL")
        if not any(mod == module for mod, _ in owners.get(model, [])):
            errors.append(f"domain {domain}: canonical model {model} is not declared by {module}")
        extenders = inherit_declarations()
        for key in ("storage_record", "runtime_control", "identity_binding", "propagation", "projection", "campaign_side"):
            extra = spec.get(key)
            if extra:
                mod, mdl = extra["module"], extra["model"].split(" ")[0]
                declared = any(m == mod for m, _ in owners.get(mdl, []))
                extended = extra.get("extension") and mod in extenders.get(mdl, set())
                if not declared and not extended:
                    errors.append(f"domain {domain}: {key} model {mdl} is not declared or extended by {mod}")
        for model_name in spec.get("compatibility_models", []):
            if model_name not in owners:
                errors.append(f"domain {domain}: compatibility model {model_name} does not exist")
            elif any(mod == module for mod, _ in owners[model_name]):
                errors.append(f"domain {domain}: compatibility model {model_name} is declared by the canonical owner module {module}")
        for mod in spec.get("compatibility_modules", []):
            if mod not in classes:
                errors.append(f"domain {domain}: compatibility module {mod} is not a known module")
            elif classes[mod] == "CANONICAL" and mod != module:
                errors.append(f"domain {domain}: compatibility module {mod} is classified CANONICAL")
    # a non-authority module may not own a canonical/runtime-control model of any domain
    for domain, spec in ownership["domains"].items():
        for key in ("canonical_owner", "runtime_control"):
            entry = spec.get(key)
            if entry and classes.get(entry["module"]) in NON_AUTHORITY_CLASSES:
                errors.append(f"domain {domain}: {key} lives in non-authority module {entry['module']}")
    return ownership


def check_roles(errors: list[str]) -> dict:
    roles = load("odoo-role-foundation.v1.json")
    groups = group_ids()
    owners = model_declarations()
    for role, spec in roles["canonical_roles"].items():
        if not spec.get("odoo_groups"):
            errors.append(f"canonical role {role} maps to no Odoo group")
        for group in spec.get("odoo_groups", []):
            if group not in groups:
                errors.append(f"canonical role {role} maps to unknown group {group}")
    for role, group_list in roles["additional_justified_roles"].items():
        for group in group_list:
            if group not in groups:
                errors.append(f"additional role {role} maps to unknown group {group}")
    if roles["membership_model"] not in owners:
        errors.append(f"membership model {roles['membership_model']} does not exist")
    if not (ROOT / roles["authority_matrix"]).is_file():
        errors.append(f"authority matrix {roles['authority_matrix']} is missing")
    return roles


def regenerate_inventory() -> list[dict[str, str]]:
    spec = importlib.util.spec_from_file_location("ledgers", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory() as tmp:
        module.OUT = Path(tmp)
        module.generate_endpoint_inventory()
        with (Path(tmp) / "ODOO-ENDPOINT-INVENTORY.csv").open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))


def check_controllers(errors: list[str]) -> dict:
    governance = load("odoo-controller-governance.v1.json")
    with INVENTORY.open(newline="", encoding="utf-8") as handle:
        committed = list(csv.DictReader(handle))
    fresh = regenerate_inventory()
    if committed != fresh:
        errors.append("docs/reconciliation/ODOO-ENDPOINT-INVENTORY.csv is stale; regenerate with scripts/generate_reconciliation_ledgers.py --endpoints-only")
    required = {"method", "path", "controller", "auth", "csrf", "scope", "campaign_binding", "idempotency", "caller", "downstream", "status"}
    for row in committed:
        missing = [column for column in required if not row.get(column)]
        if missing:
            errors.append(f"inventory row {row.get('method')} {row.get('path')} lacks {missing}")
        if row["path"] == "DYNAMIC_ROUTE":
            errors.append(f"inventory route {row['controller']} has an unresolved path")
        if row["status"].startswith("REJECT"):
            errors.append(f"inventory route {row['method']} {row['path']} is {row['status']}")
    inventory_keys = {(row["method"], row["path"], row["controller"]) for row in committed}
    governed = {(route["method"], route["path"], route["controller"]): route for route in governance["routes"]}
    for key in sorted(inventory_keys - set(governed)):
        errors.append(f"unclassified endpoint: {key[0]} {key[1]} ({key[2]})")
    for key in sorted(set(governed) - inventory_keys):
        errors.append(f"controller governance names a route that no longer exists: {key[0]} {key[1]} ({key[2]})")
    for key, route in governed.items():
        if route["classification"] not in ROUTE_CLASSES:
            errors.append(f"route {key[0]} {key[1]} has unknown classification {route['classification']!r}")
        if route["sudo"] not in SUDO_CLASSES:
            errors.append(f"route {key[0]} {key[1]} has unknown sudo class {route['sudo']!r}")
        if route["sudo"] == "REVIEW_REQUIRED" or route["classification"] == "INTERNAL":
            errors.append(f"route {key[0]} {key[1]} is an open finding ({route['classification']}/{route['sudo']})")
        if route["sudo"] != "NONE" and not route.get("sudo_justification"):
            errors.append(f"route {key[0]} {key[1]} uses sudo without a justification")
        if route["classification"] == "MIDDLEWARE_ONLY" and "controller_service_identity" not in route["auth"]:
            errors.append(f"MIDDLEWARE_ONLY route {key[0]} {key[1]} does not verify a service identity")
        if route["auth"] in {"none", "public"} and route["classification"] not in {"PUBLIC_INTENTIONAL", "LEGACY"}:
            errors.append(f"unauthenticated route {key[0]} {key[1]} is classified {route['classification']}")
    by_key = {(row["method"], row["path"], row["controller"]): row for row in committed}
    for key, route in governed.items():
        row = by_key.get(key)
        if row and row["status"] == "REVIEW_SUDO" and route["sudo"] == "NONE":
            errors.append(f"route {key[0]} {key[1]} reaches sudo but is governed as NONE")
        if row and row["status"] != "REVIEW_SUDO" and route["sudo"] != "NONE":
            errors.append(f"route {key[0]} {key[1]} is governed with sudo class {route['sudo']} but reaches no sudo")
    if governance.get("route_count") != len(governance["routes"]):
        errors.append("controller governance route_count mismatch")
    return governance


def check_provider_paths(errors: list[str]) -> dict:
    paths = load("odoo-provider-paths.v1.json")
    forbidden = set(paths["policy"]["forbidden_transport_modules"])
    classified = {site["file"] for site in paths["outbound_http_sites"]}
    found: set[str] = set()
    for path in ADDONS.rglob("*.py"):
        if "tests" in path.parts:
            continue
        rel = path.relative_to(ADDONS).as_posix()
        source = path.read_text(encoding="utf-8", errors="ignore")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden or alias.name.split(".")[0] in forbidden:
                        errors.append(f"forbidden provider transport import {alias.name} in {rel}")
            elif isinstance(node, ast.ImportFrom) and node.module and (node.module in forbidden or node.module.split(".")[0] in forbidden):
                errors.append(f"forbidden provider transport import {node.module} in {rel}")
        if OUTBOUND.search(source):
            found.add(rel)
    for rel in sorted(found - classified):
        errors.append(f"unclassified outbound HTTP site: {rel}")
    for rel in sorted(classified - found):
        errors.append(f"provider path config names a file without outbound HTTP: {rel}")
    for site in paths["outbound_http_sites"]:
        if site["classification"] in {"MIGRATE", "REMOVE"}:
            errors.append(f"outbound site {site['file']} is classified {site['classification']} and must not remain")
    manifest = ast.literal_eval((ADDONS / "call_center_campaign/__manifest__.py").read_text(encoding="utf-8"))
    if any("outbox_cron" in entry for entry in manifest.get("data", [])):
        errors.append("call_center_campaign loads the legacy design outbox cron")
    cron = (ADDONS / "call_center_campaign/data/outbox_cron.xml").read_text(encoding="utf-8")
    if '<field name="active">False</field>' not in cron:
        errors.append("legacy design outbox cron is not shipped inactive")
    return paths


def check_state_machines(errors: list[str]) -> dict:
    machines = load("odoo-state-machines.v1.json")
    for name, machine in machines["machines"].items():
        actual = selection_values(machine["module"], machine["model"], machine["field"])
        if actual is None:
            errors.append(f"state machine {name}: {machine['model']}.{machine['field']} not found in {machine['module']}")
        elif actual != machine["states"]:
            errors.append(f"state machine {name}: declared states {machine['states']} differ from code {actual}")
        constant = machine.get("transition_constant")
        if constant:
            table = module_constant(machine["module"], constant)
            if not isinstance(table, dict):
                errors.append(f"state machine {name}: transition table {constant} not found")
            else:
                for source, targets in table.items():
                    unknown = sorted(set(targets) - set(machine["states"]))
                    if source not in machine["states"] or unknown:
                        errors.append(f"state machine {name}: transition {source} -> {unknown or targets} leaves the declared state set")
        commands = machine.get("commands")
        if commands:
            table = module_constant(machine["module"], machine["command_constant"])
            if not isinstance(table, dict):
                errors.append(f"state machine {name}: command table {machine['command_constant']} not found")
            else:
                for command, spec in commands.items():
                    code = table.get(command)
                    if not code:
                        errors.append(f"state machine {name}: command {command} missing in code")
                        continue
                    if sorted(code.get("allowed", [])) != sorted(spec["allowed"]) or code.get("target") != spec["target"] or bool(code.get("gated")) != bool(spec.get("gated")):
                        errors.append(f"state machine {name}: command {command} differs from code ({code})")
    mapping = machines["machines"]["provisioning_request"].get("lifecycle_mapping", {})
    mapped = [state for states in mapping.values() for state in states]
    if sorted(mapped) != sorted(machines["machines"]["provisioning_request"]["states"]) or list(mapping) != ["CREATE", "VALIDATE", "DISPATCH", "STATUS", "RECONCILE", "DISABLE"]:
        errors.append("provisioning lifecycle mapping does not cover every request state exactly once")
    return machines


def main() -> int:
    errors: list[str] = []
    authority = check_modules(errors)
    ownership = check_domains(errors, authority)
    roles = check_roles(errors)
    governance = check_controllers(errors)
    provider = check_provider_paths(errors)
    machines = check_state_machines(errors)
    for validator in ("validate_integration_boundary.py", "validate_campaign_authority_matrix.py", "validate_migration_contracts.py"):
        result = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / validator)], capture_output=True, text=True)
        if result.returncode != 0:
            errors.append(f"{validator} failed: {(result.stderr or result.stdout).strip().splitlines()[-1:]}")
    if errors:
        for error in errors:
            print(f"ERROR={error}", file=sys.stderr)
        print("ODOO_MISSION1_FOUNDATION=FAIL", file=sys.stderr)
        return 1
    classes = {}
    for entry in authority["modules"].values():
        classes[entry["classification"]] = classes.get(entry["classification"], 0) + 1
    print(f"MODULE_AUTHORITY=PASS modules={authority['module_count']} " + " ".join(f"{k.lower()}={v}" for k, v in sorted(classes.items())))
    print(f"DOMAIN_OWNERSHIP=PASS domains={len(ownership['domains'])}")
    print(f"ROLE_FOUNDATION=PASS canonical_roles={len(roles['canonical_roles'])}")
    print(f"CONTROLLER_GOVERNANCE=PASS routes={governance['route_count']} middleware_only={sum(1 for r in governance['routes'] if r['classification'] == 'MIDDLEWARE_ONLY')}")
    print(f"PROVIDER_PATHS=PASS outbound_sites={len(provider['outbound_http_sites'])} direct_provider_transport=0")
    print(f"STATE_MODELS=PASS machines={len(machines['machines'])}")
    print("ODOO_MISSION1_FOUNDATION=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
