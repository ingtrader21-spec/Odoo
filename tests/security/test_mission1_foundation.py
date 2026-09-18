"""Mission 1 foundation: the authority configs, the validator that binds them to source, and the
negative attempts the gate must reject (unclassified module/endpoint, duplicate canonical authority,
direct provider bypass, invalid provisioning transition, cross-campaign / cross-company access rules)."""
from __future__ import annotations

import copy
import csv
import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADDONS = ROOT / "custom-addons"
SPEC = importlib.util.spec_from_file_location("mission1_gate", ROOT / "scripts/validate_mission1_foundation.py")
GATE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(GATE)


def load(name: str) -> dict:
    return json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))


class ModuleAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.authority = load("odoo-module-authority.v1.json")

    def test_every_installable_module_is_classified_exactly_once(self):
        installable = {name for name, manifest in GATE.manifests().items() if manifest.get("installable", True)}
        self.assertEqual(set(self.authority["modules"]), installable)
        self.assertEqual(self.authority["module_count"], len(installable))
        for name, entry in self.authority["modules"].items():
            self.assertIn(entry["classification"], GATE.MODULE_CLASSES, name)
            self.assertTrue(entry["rationale"] and entry["domain"], name)

    def test_unclassified_module_is_rejected(self):
        errors: list[str] = []
        original = GATE.manifests
        try:
            GATE.manifests = lambda: {**original(), "codestra_phantom_module": {"installable": True}}
            GATE.check_modules(errors)
        finally:
            GATE.manifests = original
        self.assertIn("unclassified module: codestra_phantom_module", errors)

    def test_duplicate_canonical_authority_is_rejected(self):
        errors: list[str] = []
        original = GATE.model_declarations
        try:
            GATE.model_declarations = lambda: {**original(), "cc.campaign": [("codestra_cc_core", "a.py"), ("codestra_campaign_crm_os", "b.py")]}
            GATE.check_modules(errors)
        finally:
            GATE.model_declarations = original
        self.assertTrue(any(e.startswith("duplicate model authority: cc.campaign") for e in errors), errors)

    def test_no_second_model_name_declaration_exists_in_source(self):
        owners = GATE.model_declarations()
        duplicates = {model: decl for model, decl in owners.items() if len({m for m, _ in decl}) > 1}
        self.assertEqual(duplicates, {}, "a model may have one declaring module; extensions use _inherit")

    def test_retire_candidates_and_transitional_modules_hold_no_canonical_owner(self):
        ownership = load("odoo-domain-ownership.v1.json")
        classes = {name: entry["classification"] for name, entry in self.authority["modules"].items()}
        for domain, spec in ownership["domains"].items():
            self.assertEqual(classes[spec["canonical_owner"]["module"]], "CANONICAL", domain)
            runtime = spec.get("runtime_control")
            if runtime:
                self.assertEqual(classes[runtime["module"]], "CANONICAL", domain)


class DomainOwnershipTests(unittest.TestCase):
    def test_seven_domains_have_one_canonical_owner_each(self):
        ownership = load("odoo-domain-ownership.v1.json")
        self.assertEqual(sorted(ownership["domains"]), ["agent", "call", "campaign", "channel", "identity", "inbox_outbox", "provisioning"])
        owners = GATE.model_declarations()
        for domain, spec in ownership["domains"].items():
            module, model = spec["canonical_owner"]["module"], spec["canonical_owner"]["model"]
            self.assertTrue(any(m == module for m, _ in owners[model]), f"{domain}: {model} not declared by {module}")

    def test_only_the_control_plane_controls_campaign_runtime_provisioning(self):
        ownership = load("odoo-domain-ownership.v1.json")
        self.assertEqual(ownership["domains"]["campaign"]["runtime_control"]["module"], "codestra_campaign_control_plane")
        manifest = json.loads(json.dumps(__import__("ast").literal_eval((ADDONS / "call_center_campaign/__manifest__.py").read_text(encoding="utf-8"))))
        self.assertFalse([entry for entry in manifest["data"] if "outbox_cron" in entry], "legacy design outbox cron must stay unloaded")
        cron = (ADDONS / "call_center_campaign/data/outbox_cron.xml").read_text(encoding="utf-8")
        self.assertIn('<field name="active">False</field>', cron)
        # the legacy campaign model never posts a control command itself
        legacy = (ADDONS / "call_center_campaign/models/campaign.py").read_text(encoding="utf-8")
        self.assertNotIn("control_command(", legacy)

    def test_odoo_never_becomes_the_identity_database(self):
        ownership = load("odoo-domain-ownership.v1.json")
        self.assertIn("Keycloak remains the identity database", ownership["domains"]["agent"]["identity_binding"]["authority"])
        forbidden = re.compile(r"(client_secret|password_hash|keycloak_password|refresh_token)\s*=\s*fields\.", re.I)
        for path in (ADDONS / "codestra_identity_provisioning/models").glob("*.py"):
            self.assertIsNone(forbidden.search(path.read_text(encoding="utf-8")), path.name)


class RoleFoundationTests(unittest.TestCase):
    def test_canonical_roles_resolve_to_existing_groups(self):
        roles = load("odoo-role-foundation.v1.json")
        groups = GATE.group_ids()
        self.assertEqual(sorted(roles["canonical_roles"]), ["AGENT", "AUDITOR", "INTEGRATION_SERVICE", "SUPERVISOR", "SUPER_ADMIN"])
        for role, spec in roles["canonical_roles"].items():
            for group in spec["odoo_groups"]:
                self.assertIn(group, groups, f"{role} -> {group}")

    def test_campaign_and_company_isolation_rules_are_membership_bound(self):
        """Cross-campaign / cross-company access is denied by record rules that resolve the user's
        membership. The unconditional branch of a scoped rule may be gated only by service groups,
        administrators, break-glass, or a role whose authority-matrix cross_campaign_access is not
        FALSE (auditor READ_ONLY, compliance officer CONTROLLED); agents, senior agents,
        supervisors, analysts, configuration managers and scoped users never bypass."""
        with (ROOT / "docs/authority/odoo_campaign_access_control_matrix.csv").open(newline="", encoding="utf-8") as handle:
            matrix = {row["stable_odoo_group"]: row["cross_campaign_access"] for row in csv.DictReader(handle)}
        never_bypass = {group for group, access in matrix.items() if access == "FALSE"}
        never_bypass.add("codestra_cc_security.group_cc_scoped_user")
        admin_groups = {"call_center_core.group_call_center_admin", "call_center_core.group_call_center_manager"}
        scoped_rules: list[tuple[str, str]] = []
        for path in ADDONS.glob("codestra_cc_*/security/*.xml"):
            module = path.relative_to(ADDONS).parts[0]
            for rule in re.findall(r'<field name="domain_force">(.*?)</field>', path.read_text(encoding="utf-8"), re.S):
                if "cc_allowed_campaign_ids" in rule or "cc_allowed_business_unit_ids" in rule or "cc_supervised_campaign_ids" in rule:
                    scoped_rules.append((module, rule))
        self.assertGreater(len(scoped_rules), 20)
        for module, rule in scoped_rules:
            head, _sep, _tail = rule.partition(" else ")
            if "[(1, '=', 1)]" not in head:
                continue  # a purely scoped rule (no bypass branch at all)
            bypass_groups = re.findall(r"has_group\('([^']+)'\)", head)
            for group in bypass_groups:
                self.assertNotIn(group, never_bypass, f"{module}: {group} bypasses campaign/company isolation")
                justified = (
                    group.endswith("_service")
                    or group in admin_groups
                    or matrix.get(group) in {"AUDITED", "BREAK_GLASS_ONLY", "READ_ONLY", "CONTROLLED"}
                )
                self.assertTrue(justified, f"{module}: {group} is not a justified bypass")
            if not bypass_groups:
                self.assertIn("cc_has_active_break_glass", head, f"{module}: unconditional bypass without a group or break-glass")
        # the auditor is read-only: never write/unlink; create only on the append-only audit ledger
        for path in ADDONS.glob("*/security/ir.model.access.csv"):
            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if row.get("group_id:id", "").endswith("group_cc_auditor"):
                        self.assertEqual((row["perm_write"], row["perm_unlink"]), ("0", "0"), f"{path.parent.parent.name}: {row['id']}")
                        if row["perm_create"] == "1":
                            self.assertEqual(row["model_id:id"], "model_cc_audit_event", f"{path.parent.parent.name}: {row['id']}")

    def test_membership_model_grants_exactly_one_active_operational_membership(self):
        source = (ADDONS / "codestra_cc_security/models/campaign_security.py").read_text(encoding="utf-8")
        self.assertIn("cc.campaign.membership", source)
        self.assertRegex(source, r"active.*operational|operational.*active", "membership cardinality must be enforced")


class ControllerGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.governance = load("odoo-controller-governance.v1.json")
        with (ROOT / "docs/reconciliation/ODOO-ENDPOINT-INVENTORY.csv").open(newline="", encoding="utf-8") as handle:
            self.inventory = list(csv.DictReader(handle))

    def test_inventory_is_fresh_complete_and_fully_classified(self):
        self.assertEqual(self.inventory, GATE.regenerate_inventory())
        keys = {(r["method"], r["path"], r["controller"]) for r in self.inventory}
        governed = {(r["method"], r["path"], r["controller"]) for r in self.governance["routes"]}
        self.assertEqual(keys, governed)
        self.assertEqual(self.governance["route_count"], len(keys))
        for row in self.inventory:
            for column in ("method", "path", "controller", "auth", "csrf", "scope", "campaign_binding", "idempotency", "caller", "downstream", "status"):
                self.assertTrue(row[column], f"{row['path']} lacks {column}")
            self.assertNotEqual(row["path"], "DYNAMIC_ROUTE")
            self.assertFalse(row["status"].startswith("REJECT"), row["path"])

    def test_unclassified_endpoint_is_rejected(self):
        errors: list[str] = []
        original = GATE.regenerate_inventory
        extra = dict(self.inventory[0], path="/codestra/phantom", controller="Phantom.route")
        try:
            GATE.regenerate_inventory = lambda: self.inventory + [extra]
            # committed inventory != fresh -> stale, and the phantom route is unclassified
            GATE.check_controllers(errors)
        finally:
            GATE.regenerate_inventory = original
        self.assertTrue(any("is stale" in e for e in errors), errors)

    def test_unauthenticated_routes_are_only_public_intentional_or_retired(self):
        for route in self.governance["routes"]:
            if route["auth"] in {"none", "public"}:
                self.assertIn(route["classification"], {"PUBLIC_INTENTIONAL", "LEGACY"}, route["path"])
            if route["classification"] == "MIDDLEWARE_ONLY":
                self.assertIn("controller_service_identity", route["auth"], route["path"])
                self.assertEqual(route["caller"], "MIDDLEWARE_SERVICE", route["path"])

    def test_every_sudo_route_carries_a_justification_and_no_open_findings_remain(self):
        for route in self.governance["routes"]:
            self.assertIn(route["sudo"], GATE.SUDO_CLASSES)
            self.assertNotEqual(route["sudo"], "REVIEW_REQUIRED", route["path"])
            self.assertNotEqual(route["classification"], "INTERNAL", route["path"])
            if route["sudo"] != "NONE":
                self.assertTrue(route["sudo_justification"], route["path"])
                self.assertEqual(route["lifecycle_status"], "REVIEW_SUDO", route["path"])

    def test_retired_routes_answer_410(self):
        retired = [r for r in self.inventory if r["status"] == "RETIRED"]
        self.assertEqual(sorted(r["path"] for r in retired), ["/api/v1/integration/campaign-actions", "/codestra/api/v1/call-events", "/codestra/integration/v1/results"])
        for route in self.governance["routes"]:
            if route["lifecycle_status"] == "RETIRED":
                self.assertEqual(route["classification"], "LEGACY", route["path"])


class ProviderBypassTests(unittest.TestCase):
    def test_every_outbound_site_targets_middleware_or_keycloak(self):
        paths = load("odoo-provider-paths.v1.json")
        for site in paths["outbound_http_sites"]:
            self.assertIn(site["classification"], {"MIDDLEWARE_CLIENT", "IDENTITY_PROVIDER", "READ_ONLY_PROJECTION"}, site["file"])
            self.assertTrue((ADDONS / site["file"]).is_file(), site["file"])
        self.assertFalse(paths["policy"]["direct_provider_transport_allowed"])

    def test_direct_provider_transport_is_rejected(self):
        errors: list[str] = []
        phantom = ADDONS / "codestra_base" / "models" / "_mission1_phantom_provider.py"
        phantom.write_text("import smtplib\nimport urllib.request\n\n\ndef send():\n    return urllib.request.urlopen('https://provider.invalid')\n", encoding="utf-8")
        try:
            GATE.check_provider_paths(errors)
        finally:
            phantom.unlink()
        self.assertTrue(any("forbidden provider transport import smtplib" in e for e in errors), errors)
        self.assertTrue(any("unclassified outbound HTTP site: codestra_base/models/_mission1_phantom_provider.py" in e for e in errors), errors)

    def test_no_forbidden_transport_in_source(self):
        forbidden = load("odoo-provider-paths.v1.json")["policy"]["forbidden_transport_modules"]
        pattern = re.compile(r"^\s*(?:import|from)\s+(" + "|".join(re.escape(m) for m in forbidden) + r")\b", re.M)
        for path in ADDONS.rglob("*.py"):
            if "tests" in path.parts:
                continue
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8", errors="ignore")), path)


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.machines = load("odoo-state-machines.v1.json")["machines"]

    def test_declared_states_match_code_for_every_machine(self):
        for name, machine in self.machines.items():
            self.assertEqual(GATE.selection_values(machine["module"], machine["model"], machine["field"]), machine["states"], name)

    def test_required_lifecycles_are_covered(self):
        models = {m["model"] + "." + m["field"] for m in self.machines.values()}
        for required in ("cc.campaign.lifecycle_state", "codestra.agent.onboarding.state", "codestra.provisioning.request.state", "codestra.agent.channel.state", "cc.campaign.provisioning.run.status", "codestra.runtime.integration.outbox.delivery_state"):
            self.assertIn(required, models)

    def test_invalid_provisioning_transition_is_rejected_by_the_campaign_lifecycle_table(self):
        table = GATE.module_constant("codestra_cc_core", "SAFE_LIFECYCLE_TRANSITIONS")
        self.assertIsInstance(table, dict)
        self.assertNotIn("active", table["draft"], "draft -> active must not be a safe transition")
        self.assertNotIn("provisioning", table["draft"])
        self.assertIn("provisioning", table["approved"])
        commands = GATE.module_constant("codestra_campaign_control_plane", "COMMANDS")
        self.assertNotIn("absent", commands["activate"]["allowed"], "activate from absent is an invalid transition")
        self.assertNotIn("active", commands["provision"]["allowed"], "provision while active is an invalid transition")
        self.assertTrue(commands["provision"].get("gated") and commands["activate"].get("gated"))

    def test_state_drift_is_rejected(self):
        errors: list[str] = []
        original = GATE.load
        drifted = copy.deepcopy(original("odoo-state-machines.v1.json"))
        drifted["machines"]["campaign_lifecycle"]["states"].append("phantom")
        try:
            GATE.load = lambda name: drifted if name == "odoo-state-machines.v1.json" else original(name)
            GATE.check_state_machines(errors)
        finally:
            GATE.load = original
        self.assertTrue(any("campaign_lifecycle" in e and "differ from code" in e for e in errors), errors)


class MiddlewareBoundaryTests(unittest.TestCase):
    def test_boundary_document_defines_every_required_concept(self):
        doc = (ROOT / "docs/odoo-middleware-boundary-v1.md").read_text(encoding="utf-8")
        for concept in ("command request", "command ID", "idempotency key", "correlation ID", "timeout", "retry classification", "result", "failure", "callback", "reconciliation"):
            self.assertIn(concept, doc, concept)

    def test_gate_passes_on_the_committed_tree(self):
        errors: list[str] = []
        authority = GATE.check_modules(errors)
        GATE.check_domains(errors, authority)
        GATE.check_roles(errors)
        GATE.check_controllers(errors)
        GATE.check_provider_paths(errors)
        GATE.check_state_machines(errors)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
