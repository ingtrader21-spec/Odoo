"""The prepared Odoo → Middleware V3 command mapping stays dark and matches source."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "middleware-v3-command-mapping.v1.json"
VALIDATOR = ROOT / "scripts" / "validate_middleware_v3_command_mapping.py"
KERNEL = {
    "submit": ("POST", "/platform/v1/commands"),
    "status": ("GET", "/platform/v1/operations/{operation_id}"),
    "timeline": ("GET", "/platform/v1/operations/{operation_id}/timeline"),
    "cancel": ("POST", "/platform/v1/operations/{operation_id}/cancel"),
    "replay": ("POST", "/platform/v1/operations/{operation_id}/replay"),
    "describe": ("GET", "/platform/v1/kernel/describe"),
}


class MiddlewareV3CommandMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    def test_validator_passes_against_source(self) -> None:
        result = subprocess.run([sys.executable, "-I", str(VALIDATOR)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MIDDLEWARE_V3_COMMAND_MAPPING=PASS", result.stdout)
        self.assertIn("RUNTIME_APPLY_AUTHORIZED=NO", result.stdout)

    def test_mapping_is_pending_and_names_the_canonical_edge(self) -> None:
        contract = self.contract
        self.assertEqual(contract["status"], "V3_PENDING_FINAL_MIDDLEWARE_CONTRACT")
        self.assertFalse(contract["runtime_apply_authorized"])
        self.assertEqual(contract["flow"]["canonical_upstream"], "middleware-integration-api:8095")
        self.assertIn("appolon-middleware-integration-api", contract["flow"]["retired_upstream_aliases"])
        self.assertFalse(contract["flow"]["browser_privileged_direct_bridge"])
        self.assertEqual(contract["identity"]["caller_client_id"], "odoo-integration")
        self.assertEqual(contract["identity"]["audience"], "middleware-api")
        self.assertEqual(contract["identity"]["idempotency_header"], "Idempotency-Key")
        self.assertEqual(contract["identity"]["correlation_header"], "X-Correlation-ID")
        for key, (method, path) in KERNEL.items():
            with self.subTest(route=key):
                self.assertEqual(contract["v3_kernel_routes"][key]["method"], method)
                self.assertEqual(contract["v3_kernel_routes"][key]["path"], path)

    def test_every_effectful_call_maps_to_a_command_or_a_recorded_owner_decision(self) -> None:
        for row in self.contract["odoo_to_middleware"]:
            with self.subTest(path=row["current_path"]):
                if row["v3_state"] == "MAPS_TO_V3_COMMAND":
                    self.assertTrue(row["effectful"])
                    self.assertTrue(row["v3_command_type"])
                    self.assertTrue(row["v3_adapter"])
                    self.assertEqual(row["current_auth"], "keycloak-client-credentials")
                else:
                    self.assertIsNone(row["v3_command_type"])
                    self.assertTrue(row.get("owner_decision"), "non-mapped rows record the owner decision")
                for source in row["source"]:
                    self.assertTrue((ROOT / source).is_file(), source)

    def test_provider_selection_never_belongs_to_odoo(self) -> None:
        messages = next(r for r in self.contract["odoo_to_middleware"] if r["current_path"] == "/v1/communications/messages")
        self.assertEqual(messages["v3_command_type"], "message.send")
        self.assertIn("never by Odoo", messages["v3_adapter"])
        self.assertFalse(self.contract["middleware_to_odoo"]["n8n_direct_business_effect_bypass"])
        self.assertTrue(self.contract["middleware_to_odoo"]["readback_required"])
        self.assertEqual(self.contract["activation"]["blocked_on"], "BLOCKED_ON_MIDDLEWARE_V3_FINAL_SHA")


if __name__ == "__main__":
    unittest.main(verbosity=2)
