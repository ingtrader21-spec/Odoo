import csv
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class MissionContractTests(unittest.TestCase):
    def test_all_live_capabilities_are_closed(self):
        policy = json.loads((ROOT / "config/mission-safety-policy.json").read_text())
        self.assertTrue(policy["live_capabilities"])
        self.assertFalse(any(policy["live_capabilities"].values()))
        self.assertTrue(policy["test_data"]["synthetic_only"])
        self.assertFalse(policy["test_data"]["provider_delivery"])
        self.assertFalse(policy["test_data"]["pstn_calls"])

    def test_negative_authorization_matrix_is_complete(self):
        payload = json.loads(
            (ROOT / "tests/security/negative-authorization-matrix.json").read_text()
        )
        scenario_ids = {item["id"] for item in payload["scenarios"]}
        self.assertEqual(len(scenario_ids), 10)
        self.assertTrue(all(item["expected"] == "deny" for item in payload["scenarios"]))

    def test_api_inventory_remains_uncertified_without_runtime_evidence(self):
        payload = json.loads(
            (ROOT / "tests/contracts/canonical-endpoints.json").read_text()
        )
        self.assertEqual(payload["certification_status"], "blocked-runtime-evidence")
        self.assertTrue(
            all(
                item["certification_status"] == "blocked-runtime-evidence"
                for item in payload["endpoints"]
            )
        )

    def test_controller_inventory_rejects_no_authenticated_service_route(self):
        with (ROOT / "docs/reconciliation/ODOO-ENDPOINT-INVENTORY.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            rows = list(csv.DictReader(handle))

        # Mission 1 (2026-09-18): the inventory is regenerated offline from every controller
        # (135 routes) and the three 410 Gone routes are recognised as RETIRED.
        self.assertEqual(len(rows), 135)
        self.assertFalse(
            [row for row in rows if row["status"].startswith("REJECT")]
        )
        self.assertEqual(
            sorted(row["path"] for row in rows if row["status"] == "RETIRED"),
            [
                "/api/v1/integration/campaign-actions",
                "/codestra/api/v1/call-events",
                "/codestra/integration/v1/results",
            ],
        )
        self.assertFalse([row for row in rows if row["path"] == "DYNAMIC_ROUTE"])
        unauthenticated_mutations = [
            row
            for row in rows
            if row["auth"] in {"none", "public"}
            and set(row["method"].split(";"))
            & {"POST", "PUT", "PATCH", "DELETE", "ANY"}
            and row["status"] != "RETIRED"
        ]
        self.assertFalse(unauthenticated_mutations)


if __name__ == "__main__":
    unittest.main()
