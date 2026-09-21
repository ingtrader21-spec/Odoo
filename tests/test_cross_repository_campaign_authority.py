from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts/middleware-public-api-route-contract.v2.json"
PINNED = ROOT / "contracts/middleware-public-api-route-contract.sha256"
ODOO_CONTRACT = ROOT / "contracts/odoo/campaign-control.v1.json"
FIXTURE = ROOT / "contracts/odoo/fixtures/TEST_SYN.campaign-desired-state.v1.json"
CAMPAIGN_MODEL = ROOT / "custom-addons/codestra_campaign_control_plane/models/campaign_lifecycle.py"
TELEPHONY_CLIENT = ROOT / "custom-addons/codestra_vicidial_crm/models/middleware_client.py"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def digest(document: dict) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_vendored_contract_hash_is_exact_and_private_odoo_operations_match():
    contract = load(CONTRACT)
    assert PINNED.read_text(encoding="utf-8").strip() == digest(contract)
    expected = {
        (row["method"], row["path"], row["scope"])
        for row in contract["routes"]
        if row["classification"] == "private_only"
    }
    operations = load(ODOO_CONTRACT)["operations"]
    actual = {
        (row["method"], row["path"], row["scope"])
        for row in operations.values()
        if row["path"] in {
            "/api/v1/integration/automation-results",
            "/api/v1/integration/campaigns/actual-state",
        }
    }
    assert actual == expected


def test_cc_campaign_has_one_owner_and_control_plane_only_extends_it():
    declarations: list[tuple[Path, str]] = []
    for path in (ROOT / "custom-addons").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "_name":
                        try:
                            value = ast.literal_eval(node.value)
                        except (ValueError, TypeError):
                            continue
                        if value == "cc.campaign":
                            declarations.append((path, value))
    assert len(declarations) == 1
    source = CAMPAIGN_MODEL.read_text(encoding="utf-8")
    assert '_inherit = "cc.campaign"' in source
    assert '_name = "cc.campaign"' not in source


def test_test_syn_fixture_is_safe_and_contains_campaign_and_desired_state():
    fixture = load(FIXTURE)
    assert fixture["campaign"]["campaign_public_id"] == "TEST_SYN"
    assert fixture["campaign"]["business_unit_public_id"] == "TEST_SYN"
    assert fixture["campaign"]["organization_public_id"] == "TEST_SYN_TENANT"
    assert fixture["desired_state"]["campaign_public_id"] == "TEST_SYN"
    assert fixture["desired_state"]["desired_state"] == "provisioned_disabled"
    assert fixture["desired_state"]["configuration_version"] == 1
    assert fixture["safety"] == {
        "production_writes_enabled": False,
        "provider_effects_enabled": False,
        "design_automation_enabled": False,
    }


def test_telephony_command_scope_uses_the_canonical_name_only():
    source = TELEPHONY_CLIENT.read_text(encoding="utf-8")
    assert "telephony:command" not in source
    assert source.count("telephony.commands.write") >= 2
