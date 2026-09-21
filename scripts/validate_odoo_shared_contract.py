#!/usr/bin/env python3
"""Source-level parity gate for the shared Middleware->Odoo contract.

Runs without an Odoo runtime. Proves that:

* contracts/odoo/campaign-control.v1.json hashes to the pinned canonical
  SHA-256 (the value Middleware migration 0066 embeds);
* the YAML endpoint catalog lists every contract operation exactly once, as
  served (method/path/scope identical) or explicitly unserved;
* the campaign-control vocabulary in the catalog equals the contract
  (effective states, outbox allowlist, read-back required fields);
* the vendored request schemas agree with the contract (required read-back
  fields, effective-state and operation enums, scope triple for reads);
* the retired campaign-actions route is catalogued as 410 and never served.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "odoo" / "campaign-control.v1.json"
PIN = ROOT / "contracts" / "odoo" / "campaign-control.v1.sha256.json"
SCHEMAS = ROOT / "contracts" / "odoo" / "schemas"
CATALOG = ROOT / "custom-addons" / "call_center_campaign" / "docs" / "ODOO-ENDPOINT-CATALOG.yaml"
CONTROLLER = ROOT / "custom-addons" / "codestra_campaign_control_plane" / "controllers" / "campaign_platform_api.py"
LIFECYCLE = ROOT / "custom-addons" / "codestra_campaign_control_plane" / "models" / "campaign_lifecycle.py"
BASE_CONTROLLER = ROOT / "custom-addons" / "call_center_campaign" / "controllers" / "integration_api.py"
MIDDLEWARE_CATALOG_SHA256 = "874711f3a8c38bfb2c69169d5f7c1717897d48419ce60bf5649a899f023e1e03"
RETIRED_PATH = "/api/v1/integration/campaign-actions"


def canonical_sha256(document: dict) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_catalog(path: Path) -> dict:
    sections: dict = {}
    section = current = block = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            section = line.split(":", 1)[0]
            sections.setdefault(section, {"entries": [], "scalars": {}, "lists": {}})
            current = block = None
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        store = sections[section]
        if stripped.startswith("- key:") and indent <= 4:
            current = {"key": stripped.split(":", 1)[1].strip()}
            store["entries"].append(current)
            block = None
        elif stripped.startswith("- ") and block is not None:
            store["lists"].setdefault(block, []).append(stripped[2:].strip())
        elif stripped.startswith("- ") and current is not None and block is None:
            current.setdefault("_items", []).append(stripped[2:].strip())
        elif stripped.endswith(":"):
            block = stripped[:-1]
            if current is not None and indent > 2:
                block = None
        elif ":" in stripped:
            name, value = stripped.split(":", 1)
            value = value.strip().strip('"')
            if current is not None and indent > 2:
                current[name.strip()] = value
            else:
                store["scalars"][name.strip()] = value
    return sections


def python_list_literal(source: str, name: str) -> list[str]:
    match = re.search(rf"^{name}\s*=\s*\((.*?)\)\n", source, re.S | re.M)
    if match is None:
        match = re.search(rf"^{name}\s*=\s*\[(.*?)\]\n", source, re.S | re.M)
    if match is None:
        return []
    return re.findall(r'"([^"]+)"', match.group(1))


def main() -> int:
    errors: list[str] = []
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    pin = json.loads(PIN.read_text(encoding="utf-8"))
    catalog = load_catalog(CATALOG)

    digest = canonical_sha256(contract)
    if digest != MIDDLEWARE_CATALOG_SHA256:
        errors.append(f"contract canonical sha256 {digest} != middleware pin")
    if pin.get("canonical_sha256") != MIDDLEWARE_CATALOG_SHA256:
        errors.append("contracts/odoo/campaign-control.v1.sha256.json pin drifted")
    if pin.get("file_sha256") != hashlib.sha256(CONTRACT.read_bytes()).hexdigest():
        errors.append("contract file bytes drifted from the recorded file sha256")
    if catalog.get("shared_contract", {}).get("scalars", {}).get("canonical_sha256") != MIDDLEWARE_CATALOG_SHA256:
        errors.append("catalog shared_contract.canonical_sha256 drifted")

    served = {entry.get("operation"): entry for entry in catalog.get("endpoints", {}).get("entries", [])}
    unserved = {entry["key"] for entry in catalog.get("shared_contract", {}).get("entries", [])}
    operations = contract["operations"]
    if set(served) | unserved != set(operations):
        errors.append(
            "catalog coverage mismatch: missing="
            + ",".join(sorted(set(operations) - set(served) - unserved))
            + " extra="
            + ",".join(sorted((set(served) | unserved) - set(operations)))
        )
    if set(served) & unserved:
        errors.append("operations listed as both served and unserved: " + ",".join(sorted(set(served) & unserved)))
    for key, entry in served.items():
        operation = operations.get(key, {})
        for field, expected in (
            ("key", operation.get("endpoint_key")),
            ("method", operation.get("method")),
            ("path", operation.get("path")),
            ("scope", operation.get("scope")),
            ("mutation", "true" if operation.get("kind") == "write" else "false"),
        ):
            if entry.get(field) != expected:
                errors.append(f"{key}: catalog {field}={entry.get(field)!r} != contract {expected!r}")
    if any(entry.get("path") == RETIRED_PATH for entry in served.values()):
        errors.append("retired campaign-actions path is still catalogued as served")
    retired = contract["retired_operations"]["campaign_actions.apply"]["path"]
    webhook = catalog.get("webhook_boundary", {}).get("lists", {}).get("retired_routes", [])
    if f"path: {retired}" not in webhook:
        errors.append("retired campaign-actions route is not catalogued under webhook_boundary.retired_routes")
    if "odoo.campaign.control.write" in {op.get("scope") for op in operations.values()}:
        errors.append("contract must never grant odoo.campaign.control.write to Middleware")

    control = catalog.get("campaign_control", {})
    lists = control.get("lists", {})
    if lists.get("outbox_event_allowlist") != contract["outbox_events"]["allowlist"]:
        errors.append("catalog outbox_event_allowlist != contract allowlist")
    if set(lists.get("effective_states", [])) != set(contract["effective_states"]):
        errors.append("catalog effective_states != contract effective_states")
    if lists.get("readback_required_fields") != contract["actual_state_readback"]["required_fields"]:
        errors.append("catalog readback_required_fields != contract required_fields")
    if set(lists.get("middleware_scopes", [])) != {"odoo.campaign.control.read", "odoo.campaign.actual_state.write"}:
        errors.append("catalog middleware_scopes drifted")
    if control.get("scalars", {}).get("delivery") != "middleware_pull":
        errors.append("catalog delivery must be middleware_pull")

    readback_schema = json.loads((SCHEMAS / "campaigns-actual-state.v1.json").read_text(encoding="utf-8"))
    request = readback_schema["$defs"]["request"]
    if set(request["required"]) != set(contract["actual_state_readback"]["required_fields"]):
        errors.append("readback schema required != contract required_fields")
    if set(request["properties"]) != set(request["required"]) or request.get("additionalProperties", True):
        errors.append("readback schema must forbid additional properties")
    if set(request["properties"]["effective_state"]["enum"]) != set(contract["effective_states"]):
        errors.append("readback schema effective_state enum != contract")
    if set(request["properties"]["operation"]["enum"]) != set(contract["synthetic_adapter_result_state"]):
        errors.append("readback schema operation enum != contract operations")
    response_required = set(readback_schema["$defs"]["response"]["required"])
    binding = operations["campaign.actual_state.write"]
    if response_required != set(binding["response_binding"]) | {binding["receipt_field"]}:
        errors.append("readback response schema != response_binding + receipt_field")
    for name in ("campaigns-read.v1.json", "desired-state-read.v1.json"):
        schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
        if set(schema["$defs"]["request"]["required"]) != {
            "organization_public_id", "business_unit_public_id", "campaign_public_id"
        }:
            errors.append(f"{name}: request must require the scope triple")

    lifecycle = LIFECYCLE.read_text(encoding="utf-8")
    readback_fields = python_list_literal(lifecycle, "READBACK_REQUIRED_FIELDS")
    if readback_fields != contract["actual_state_readback"]["required_fields"]:
        errors.append("campaign_lifecycle.READBACK_REQUIRED_FIELDS != contract required_fields")
    operations_literal = python_list_literal(lifecycle, "OPERATIONS")
    if set(operations_literal) != set(contract["synthetic_adapter_result_state"]):
        errors.append("campaign_lifecycle.OPERATIONS != contract operations")
    for retired_event in ("campaign.synthetic.test.requested.v1", "campaign.activation.requested.v1", "campaign.rollback.requested.v1"):
        if retired_event in lifecycle or retired_event in CATALOG.read_text(encoding="utf-8"):
            errors.append(f"retired event vocabulary present: {retired_event}")
    controller = CONTROLLER.read_text(encoding="utf-8")
    for key, operation in operations.items():
        if operation.get("domain") == "campaign-control" and operation["path"] not in controller:
            errors.append(f"controller does not declare {operation['path']}")
    base_controller = BASE_CONTROLLER.read_text(encoding="utf-8")
    if "psycopg2" in base_controller or "DATABASE_URL" in base_controller:
        errors.append("base controller must not import a database driver")
    if (ROOT / "scripts" / "outbox_worker.py").exists() or (ROOT / "deploy" / "systemd").exists():
        errors.append("database outbox worker artifacts must not exist")

    if errors:
        print("Odoo shared-contract validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"ODOO_SHARED_CONTRACT_SHA256={digest}")
    print(f"ODOO_SHARED_CONTRACT_OPERATIONS={len(operations)}")
    print(f"ODOO_SHARED_CONTRACT_SERVED={len(served)}")
    print(f"ODOO_SHARED_CONTRACT_UNSERVED={len(unserved)}")
    print("ODOO_SHARED_CONTRACT_PARITY=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
