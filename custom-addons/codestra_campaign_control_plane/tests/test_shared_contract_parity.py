"""Parity between the shared Middleware contract, the Odoo routes, the
capabilities document, the YAML catalog, and the error classes.

The contract is ``contracts/odoo/campaign-control.v1.json`` (vendored
byte-identical from the Middleware repository; canonical SHA-256 pinned by
Middleware migration 0066).
"""

import hashlib
import inspect
import json
import os
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from odoo import http
from odoo.exceptions import AccessError, MissingError, ValidationError
from odoo.modules.module import get_module_path
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from odoo.addons.call_center_campaign.controllers import integration_api
from odoo.addons.call_center_campaign.controllers.integration_api import (
    CONFLICT_CONSTRAINTS,
    RETIRED_ROUTES,
    IntegrationConflict,
    IntegrationGone,
    _handle_errors,
)

from ..controllers.campaign_platform_api import (
    CONTRACT_CAPABILITIES,
    INTEGRATION_ROUTES,
    SCOPE_ACTUAL_STATE_WRITE,
    SCOPE_CONTROL_READ,
    SCOPE_CONTROL_WRITE,
    SCOPE_TRIPLE,
    CampaignIntegrationController,
    CampaignPlatformCapabilities,
    _domain,
)
from ..models.campaign_lifecycle import (
    COMMANDS,
    EFFECTIVE_STATE_KEYS,
    EVENT_TYPE_BY_OPERATION,
    OPERATIONS,
    OUTBOX_EVENT_ALLOWLIST,
    READBACK_REQUIRED_FIELDS,
    RUN_DELIVERY_STATES,
    RUN_STATUSES,
    CampaignControlConflict,
)

MIDDLEWARE_CATALOG_SHA256 = "874711f3a8c38bfb2c69169d5f7c1717897d48419ce60bf5649a899f023e1e03"
PLACEHOLDER = re.compile(r"\{[^}]+\}|<[^>]+>")
RETIRED_PATH = "/api/v1/integration/campaign-actions"


def _repository_root():
    return os.path.normpath(os.path.join(get_module_path("codestra_campaign_control_plane"), "..", ".."))


def _load_json(*parts):
    with open(os.path.join(_repository_root(), *parts), encoding="utf-8") as handle:
        return json.load(handle)


def _normalize(path):
    return PLACEHOLDER.sub("*", path.split("?", 1)[0])


def _load_catalog():
    """Minimal reader for the flat endpoint catalog (no PyYAML dependency)."""
    path = os.path.join(get_module_path("call_center_campaign"), "docs", "ODOO-ENDPOINT-CATALOG.yaml")
    sections, section, current, block = {}, None, None, None
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
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


def _all_routes():
    """(method, normalized path) -> module for every registered controller."""
    registry = getattr(http.Controller, "children_classes", None)
    if registry is None:
        registry = getattr(http, "controllers_per_module", {})
    routes = {}
    seen = set()

    def visit(cls, module):
        if cls in seen:
            return
        seen.add(cls)
        for _name, member in inspect.getmembers(cls):
            routing = getattr(member, "original_routing", None)
            if not routing:
                continue
            # A route-less @route() override inherits its paths from the parent
            # controller (Odoo 17+); it declares no routes of its own.
            for route in routing.get("routes") or []:
                for verb in routing.get("methods") or ["GET", "POST"]:
                    routes.setdefault((verb, _normalize(route)), module)
        for child in cls.__subclasses__():
            visit(child, child.__module__.split(".")[2] if child.__module__.startswith("odoo.addons.") else module)

    for module, classes in registry.items():
        for entry in classes:
            cls = entry[1] if isinstance(entry, tuple) else entry
            visit(cls, module)
    return routes


@tagged("post_install", "-at_install")
class TestSharedContractParity(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.contract = _load_json("contracts", "odoo", "campaign-control.v1.json")
        cls.pin = _load_json("contracts", "odoo", "campaign-control.v1.sha256.json")
        cls.catalog = _load_catalog()
        cls.routes = _all_routes()

    # ------------------------------------------------------------------
    # 1. the file itself
    # ------------------------------------------------------------------
    def test_contract_canonical_sha256_equals_the_middleware_pin(self):
        canonical = hashlib.sha256(
            json.dumps(self.contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(canonical, MIDDLEWARE_CATALOG_SHA256)
        self.assertEqual(self.pin["canonical_sha256"], MIDDLEWARE_CATALOG_SHA256)
        self.assertEqual(self.catalog["shared_contract"]["scalars"]["canonical_sha256"], MIDDLEWARE_CATALOG_SHA256)
        self.assertEqual(self.contract["direction"], "middleware_to_odoo")
        self.assertEqual(self.contract["service_key"], "odoo")

    # ------------------------------------------------------------------
    # 2. operations: method, path, scope
    # ------------------------------------------------------------------
    def _catalog_endpoints(self):
        return {entry["operation"]: entry for entry in self.catalog["endpoints"]["entries"]}

    def _unserved(self):
        return {entry["key"] for entry in self.catalog["shared_contract"]["entries"]}

    def test_catalog_covers_every_contract_operation_exactly_once(self):
        served = self._catalog_endpoints()
        unserved = self._unserved()
        self.assertEqual(set(served) | unserved, set(self.contract["operations"]))
        self.assertFalse(set(served) & unserved)
        for key, entry in served.items():
            operation = self.contract["operations"][key]
            self.assertEqual(entry["key"], operation["endpoint_key"], key)
            self.assertEqual(entry["method"], operation["method"], key)
            self.assertEqual(entry["path"], operation["path"], key)
            self.assertEqual(entry["scope"], operation["scope"], key)
            self.assertEqual(entry["mutation"], "true" if operation["kind"] == "write" else "false", key)

    def test_every_served_operation_has_a_registered_route(self):
        served = self._catalog_endpoints()
        loaded = {module for _key, module in self.routes.items()}
        for key, entry in served.items():
            module = entry["served_by"]
            if module not in loaded and module not in ("call_center_campaign", "codestra_campaign_control_plane"):
                continue  # optional addon not installed in this database
            self.assertIn((entry["method"], _normalize(entry["path"])), self.routes, key)
            self.assertEqual(self.routes[(entry["method"], _normalize(entry["path"]))], module, key)

    def test_unserved_operations_really_have_no_route(self):
        for key in self._unserved():
            operation = self.contract["operations"][key]
            self.assertNotIn((operation["method"], _normalize(operation["path"])), self.routes, key)

    def test_middleware_facing_campaign_routes_and_scopes(self):
        expected = {
            "campaign.actual_state.write": SCOPE_ACTUAL_STATE_WRITE,
            "campaigns.read": SCOPE_CONTROL_READ,
            "desired_state.read": SCOPE_CONTROL_READ,
        }
        for key, scope in expected.items():
            operation = self.contract["operations"][key]
            self.assertEqual(INTEGRATION_ROUTES[key], operation["path"])
            self.assertEqual(operation["scope"], scope)
            self.assertEqual(operation["method"], "POST")
        self.assertNotEqual(SCOPE_CONTROL_WRITE, SCOPE_ACTUAL_STATE_WRITE)
        contract_scopes = {op["scope"] for op in self.contract["operations"].values()}
        self.assertNotIn(SCOPE_CONTROL_WRITE, contract_scopes, "Middleware must never hold control.write")
        self.assertEqual(
            set(self.catalog["campaign_control"]["lists"]["middleware_scopes"]),
            {SCOPE_CONTROL_READ, SCOPE_ACTUAL_STATE_WRITE},
        )

    def test_retired_campaign_actions_route_is_gone_and_not_advertised(self):
        retired = self.contract["retired_operations"]["campaign_actions.apply"]
        self.assertEqual(retired["path"], RETIRED_PATH)
        self.assertIn(RETIRED_PATH, RETIRED_ROUTES)
        self.assertEqual(self.routes.get(("POST", RETIRED_PATH)), "call_center_campaign")
        keys = CampaignPlatformCapabilities()._capability_keys()
        self.assertNotIn("campaign_actions.apply", keys)
        self.assertNotIn(RETIRED_PATH, {entry["path"] for entry in self.catalog["endpoints"]["entries"]})
        fake_request = MagicMock()
        fake_request.httprequest.path = RETIRED_PATH
        fake_request.httprequest.headers = {}
        with patch.object(integration_api, "request", fake_request):
            def gone():
                raise IntegrationGone("ROUTE_RETIRED:automation_results.apply")

            response = _handle_errors(gone)
        self.assertEqual(response.status_code, 410)
        self.assertIn("GONE", response.get_data(as_text=True))

    # ------------------------------------------------------------------
    # 3. capabilities
    # ------------------------------------------------------------------
    def test_capabilities_advertise_served_operations_of_installed_modules(self):
        keys = CampaignPlatformCapabilities()._capability_keys()
        self.assertEqual(len(keys), len(set(keys)))
        served = self._catalog_endpoints()
        for key, entry in served.items():
            if entry["served_by"] in ("call_center_campaign", "codestra_campaign_control_plane"):
                self.assertIn(key, keys, key)
        for key in self._unserved():
            self.assertNotIn(key, keys)
        self.assertEqual(set(CONTRACT_CAPABILITIES), set(INTEGRATION_ROUTES))
        self.assertTrue({"outbox.claim", "outbox.acknowledge"}.issubset(keys))

    # ------------------------------------------------------------------
    # 4. vocabularies and request schemas
    # ------------------------------------------------------------------
    def test_effective_states_and_events_match_the_contract(self):
        self.assertEqual(EFFECTIVE_STATE_KEYS, set(self.contract["effective_states"]))
        self.assertEqual(OUTBOX_EVENT_ALLOWLIST, self.contract["outbox_events"]["allowlist"])
        self.assertEqual(
            {event: op for op, event in EVENT_TYPE_BY_OPERATION.items()},
            self.contract["outbox_events"]["operation_by_event_type"],
        )
        self.assertEqual(set(OPERATIONS), set(self.contract["synthetic_adapter_result_state"]))
        for operation, state in self.contract["synthetic_adapter_result_state"].items():
            self.assertEqual(COMMANDS[operation]["target"], state, operation)
        self.assertEqual(COMMANDS["rollback"]["operation"], "reconcile")
        for spec in COMMANDS.values():
            self.assertIn(spec["operation"], set(OPERATIONS) | {None})
        self.assertEqual(
            self.catalog["campaign_control"]["lists"]["outbox_event_allowlist"], OUTBOX_EVENT_ALLOWLIST
        )
        self.assertEqual(set(self.catalog["campaign_control"]["lists"]["effective_states"]), EFFECTIVE_STATE_KEYS)
        self.assertEqual(
            self.catalog["campaign_control"]["lists"]["run_statuses"], [key for key, _label in RUN_STATUSES]
        )
        self.assertEqual(
            self.catalog["campaign_control"]["lists"]["run_delivery_states"],
            [key for key, _label in RUN_DELIVERY_STATES],
        )

    def test_readback_schema_matches_the_controller_and_model(self):
        schema = _load_json("contracts", "odoo", "schemas", "campaigns-actual-state.v1.json")
        request_schema = schema["$defs"]["request"]
        self.assertEqual(list(READBACK_REQUIRED_FIELDS), self.contract["actual_state_readback"]["required_fields"])
        self.assertEqual(set(READBACK_REQUIRED_FIELDS), set(request_schema["required"]))
        self.assertEqual(set(request_schema["properties"]), set(READBACK_REQUIRED_FIELDS))
        self.assertFalse(request_schema.get("additionalProperties", True))
        self.assertNotIn("execution_status", READBACK_REQUIRED_FIELDS)
        self.assertEqual(set(request_schema["properties"]["effective_state"]["enum"]), EFFECTIVE_STATE_KEYS)
        self.assertEqual(set(request_schema["properties"]["operation"]["enum"]), set(OPERATIONS))
        response_required = set(schema["$defs"]["response"]["required"])
        binding = self.contract["operations"]["campaign.actual_state.write"]
        self.assertEqual(response_required, set(binding["response_binding"]) | {binding["receipt_field"]})
        self.assertEqual(binding["conflict_error_class"], "STALE_CONFIGURATION_VERSION")
        self.assertIn("STALE_CONFIGURATION_VERSION", self.catalog["campaign_control"]["lists"]["conflict_codes"])
        self.assertIn("IDEMPOTENCY_CONFLICT", schema["errors"]["409"])
        self.assertEqual(
            self.catalog["campaign_control"]["lists"]["readback_required_fields"], list(READBACK_REQUIRED_FIELDS)
        )

    def test_read_schemas_use_the_scope_triple(self):
        for name in ("campaigns-read.v1.json", "desired-state-read.v1.json"):
            schema = _load_json("contracts", "odoo", "schemas", name)
            self.assertEqual(set(schema["$defs"]["request"]["required"]), set(SCOPE_TRIPLE), name)
            self.assertFalse(schema["$defs"]["request"].get("additionalProperties", True), name)
        desired = _load_json("contracts", "odoo", "schemas", "desired-state-read.v1.json")
        self.assertEqual(
            set(desired["$defs"]["response"]["required"]),
            set(self.contract["operations"]["desired_state.read"]["response_fields"]),
        )
        self.assertEqual(set(desired["$defs"]["response"]["properties"]["desired_state"]["enum"]), EFFECTIVE_STATE_KEYS)

    def test_automation_and_provider_activity_schemas_match_the_controller(self):
        automation = _load_json("contracts", "odoo", "schemas", "automation-results.v1.json")
        self.assertEqual(
            set(automation["$defs"]["request"]["required"]), integration_api.AUTOMATION_RESULT_REQUIRED_FIELDS
        )
        self.assertEqual(
            set(automation["$defs"]["request"]["properties"]["actions"]["items"]["properties"]["action_type"]["enum"]),
            set(integration_api.AUTOMATION_ACTION_TYPES),
        )
        provider = _load_json("contracts", "odoo", "schemas", "provider-activities.v1.json")
        self.assertEqual(
            set(provider["$defs"]["request"]["required"]), integration_api.PROVIDER_ACTIVITY_REQUIRED_FIELDS
        )

    # ------------------------------------------------------------------
    # 5. error classes
    # ------------------------------------------------------------------
    def test_error_classes_map_to_http_statuses(self):
        fake_request = MagicMock()
        fake_request.httprequest.headers = {"X-Codestra-Correlation-ID": "corr-contract"}
        allowlisted = next(iter(CONFLICT_CONSTRAINTS))

        def db_error(pgcode, constraint):
            exc = RuntimeError("duplicate key value violates unique constraint (secret detail)")
            exc.pgcode = pgcode
            exc.diag = SimpleNamespace(constraint_name=constraint)
            return exc

        with patch.object(integration_api, "request", fake_request), mute_logger(
            "odoo.addons.call_center_campaign.controllers.integration_api"
        ):
            cases = (
                (AccessError("denied"), 403, "FORBIDDEN"),
                (MissingError("ENTITY_NOT_FOUND"), 404, "NOT_FOUND"),
                (IntegrationConflict("STALE_CONFIGURATION_VERSION"), 409, "CONFLICT"),
                (ValidationError("FIELD_REQUIRED:name"), 422, "VALIDATION"),
                (ValueError("UNKNOWN_COMMAND"), 422, "VALIDATION"),
                (IntegrationGone("ROUTE_RETIRED"), 410, "GONE"),
                (db_error("23505", allowlisted), 409, "CONFLICT"),
                (db_error("23505", "some_other_table_pkey"), 500, "INTERNAL"),
                (db_error("23503", "cc_campaign_fk"), 500, "INTERNAL"),
                (RuntimeError("database detail must not leak"), 500, "INTERNAL"),
            )
            for exc, status, classification in cases:
                def operation(exc=exc):
                    raise exc

                response = _handle_errors(operation)
                body = response.get_data(as_text=True)
                self.assertEqual(response.status_code, status, str(exc))
                self.assertIn(classification, body)
                self.assertEqual(response.headers["X-Correlation-ID"], "corr-contract")
                if status == 500:
                    self.assertNotIn("secret detail", body)
                    self.assertNotIn("database detail", body)
                    self.assertIn("corr-contract", body)

            def serialization_failure():
                raise db_error("40001", None)

            with self.assertRaises(RuntimeError):
                _handle_errors(serialization_failure)

            def conflict():
                raise CampaignControlConflict("STALE_CONFIGURATION_VERSION")

            response = _handle_errors(lambda: _domain(conflict))
            self.assertEqual(response.status_code, 409)
            self.assertIn("STALE_CONFIGURATION_VERSION", response.get_data(as_text=True))

    # ------------------------------------------------------------------
    # 6. no competing delivery path, no second state machine
    # ------------------------------------------------------------------
    def test_no_competing_delivery_path_or_database_worker_remains(self):
        Outbox = self.env["codestra.runtime.integration.outbox"]
        for name in (
            "_send_control_to_middleware",
            "_control_middleware_configuration",
            "_cron_deliver_campaign_control_events",
        ):
            self.assertFalse(hasattr(Outbox, name), name)
        crons = self.env["ir.cron"].sudo().search([("code", "ilike", "_cron_deliver_campaign")])
        self.assertFalse(crons, "no Odoo-originated delivery cron may be loaded")
        self.assertTrue(self.env["ir.cron"].sudo().search([("code", "ilike", "_cron_purge_expired")]))
        repository = _repository_root()
        self.assertFalse(os.path.exists(os.path.join(repository, "scripts", "outbox_worker.py")))
        self.assertFalse(os.path.exists(os.path.join(repository, "deploy", "systemd")))
        self.assertEqual(self.catalog["campaign_control"]["scalars"]["delivery"], "middleware_pull")

    def test_legacy_campaign_model_has_no_second_state_machine(self):
        Legacy = self.env["call.center.campaign"]
        for name in (
            "control_state", "actual_state", "manifest_version", "current_manifest_id",
            "manifest_ids", "last_control_event_id", "desired_state", "effective_state",
        ):
            self.assertNotIn(name, Legacy._fields, name)
        self.assertNotIn("call.center.campaign.configuration.version", self.env.registry)
        self.assertNotIn("call.center.campaign.manifest", self.env.registry)
        self.assertIn("cc.campaign.readback", self.env.registry)

    def test_integration_controller_classes_are_registered(self):
        self.assertTrue(issubclass(CampaignIntegrationController, http.Controller))
