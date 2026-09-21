"""Lifecycle certification scenarios for the shared campaign-control contract.

Test names map onto the runtime certification list (see README): version
zero, pull/ack semantics, provision -> synthetic test -> activate -> disable,
duplicate and stale read-backs, repeated disable/activate and reconcile without
collision, rollback through reconcile, creation allowlist, and tenant scope.
"""

import json
import os
import uuid

from odoo.exceptions import AccessError, ValidationError
from odoo.modules.module import get_module_path
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..models.campaign_lifecycle import (
    EFFECTIVE_STATE_KEYS,
    ORGANIZATION_PARAMETER,
    OUTBOX_EVENT_ALLOWLIST,
    READBACK_REQUIRED_FIELDS,
    CampaignControlConflict,
)

ORGANIZATION = "TEST_ORG"


def _contract():
    path = os.path.join(get_module_path("codestra_campaign_control_plane"), "..", "..", "contracts", "odoo", "campaign-control.v1.json")
    with open(os.path.normpath(path), encoding="utf-8") as handle:
        return json.load(handle)


@tagged("post_install", "-at_install")
class TestCampaignLifecycle(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param(ORGANIZATION_PARAMETER, ORGANIZATION)
        cls.Unit = cls.env["cc.business.unit"].with_context(active_test=False)
        cls.Unit._adopt_legacy_records()
        cls.unit = cls.Unit.search([("code", "=", "COD")], limit=1)
        cls.Campaign = cls.env["cc.campaign"].with_context(active_test=False)
        cls.Run = cls.env["cc.campaign.provisioning.run"].sudo()
        cls.Readback = cls.env["cc.campaign.readback"].sudo()
        cls.Outbox = cls.env["codestra.runtime.integration.outbox"].sudo()
        cls.contract = _contract()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _campaign(self):
        suffix = uuid.uuid4().hex[:6].upper()
        return self.Campaign.create(
            {
                "name": f"Lifecycle {suffix}",
                "code": f"COD-CTRL{suffix}-OUT",
                "cc_business_unit_id": self.unit.id,
                "environment": "staging",
            }
        )

    def _command(self, campaign, command, key=None, expected=None, **extra):
        key = key or f"key-{uuid.uuid4().hex}"
        return campaign.control_command(
            command,
            idempotency_key=key,
            audit_reason="lifecycle test",
            expected_configuration_version=(
                campaign.configuration_version if expected is None else expected
            ),
            correlation_id=f"corr-{key}",
            causation_id=f"cause-{key}",
            external_effects_enabled=True,
            **extra,
        )

    def _readback_body(self, campaign, run, state, **overrides):
        body = {
            "event_uuid": run.event_uuid,
            "command_id": run.event_uuid,
            "idempotency_key": f"actual-state:{run.event_uuid}:{uuid.uuid4().hex}",
            "attempt": 1,
            "organization_public_id": ORGANIZATION,
            "business_unit_public_id": self.unit.code,
            "campaign_public_id": campaign.code,
            "configuration_version": campaign.configuration_version,
            "operation": run.operation,
            "effective_state": state,
            "manifest_ref": campaign._control_manifest_ref(),
            "manifest_hash": campaign._control_manifest_hash(),
            "evidence": {"adapter": "synthetic", "operation": run.operation},
            "observed_at": "2026-09-16T10:00:00+00:00",
            "correlation_id": run.correlation_id,
            "causation_id": run.event_uuid,
        }
        body.update(overrides)
        return body

    def _readback(self, campaign, run, state, **overrides):
        body = self._readback_body(campaign, run, state, **overrides)
        return body, campaign.control_readback(body)

    def _events(self, campaign):
        return self.Outbox.search(
            [("aggregate_type", "=", "cc.campaign"), ("aggregate_record_id", "=", campaign.id)],
            order="created_at, id",
        )

    def _provisioned(self, campaign):
        self._command(campaign, "validate", expected=0)
        run, _ = self._command(campaign, "provision")
        self._readback(campaign, run, "provisioned_disabled")
        return campaign

    def _activated(self, campaign):
        self._provisioned(campaign)
        run, _ = self._command(campaign, "synthetic_test")
        self._readback(campaign, run, "synthetic_tested")
        run, _ = self._command(campaign, "activate")
        self._readback(campaign, run, "active")
        return campaign

    # ------------------------------------------------------------------
    # vocabulary
    # ------------------------------------------------------------------
    def test_effective_state_vocabulary_matches_the_contract(self):
        self.assertEqual(EFFECTIVE_STATE_KEYS, set(self.contract["effective_states"]))
        campaign = self._campaign()
        self.assertEqual(campaign.control_status, "draft")
        self.assertEqual(campaign.desired_state, "absent")
        self.assertEqual(campaign.effective_state, "unknown")
        for name in ("desired_state", "effective_state"):
            keys = {key for key, _label in campaign._fields[name].selection}
            self.assertEqual(keys, set(self.contract["effective_states"]), name)
        self.assertNotIn("draft", keys)
        self.assertNotIn("blocked", keys)

    def test_outbox_event_allowlist_matches_the_contract(self):
        self.assertEqual(OUTBOX_EVENT_ALLOWLIST, self.contract["outbox_events"]["allowlist"])
        self.assertNotIn("campaign.rollback.requested.v1", OUTBOX_EVENT_ALLOWLIST)

    # ------------------------------------------------------------------
    # version zero and concurrency
    # ------------------------------------------------------------------
    def test_version_zero_validation_creates_version_one(self):
        campaign = self._campaign()
        self.assertEqual(campaign.configuration_version, 0)
        run, replayed = self._command(campaign, "validate", expected=0)
        self.assertFalse(replayed)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.delivery_state, "none")
        self.assertFalse(run.event_uuid)
        self.assertEqual(campaign.configuration_version, 1)
        self.assertEqual(campaign.configuration_version_id.state, "published")
        self.assertEqual(campaign.control_status, "validated")
        self.assertEqual(campaign.desired_state, "absent")
        self.assertEqual(campaign.effective_state, "unknown")
        self.assertFalse(self._events(campaign))
        document = campaign.contract_desired_state_document()
        self.assertEqual(document["configuration_version"], 1)
        self.assertTrue(document["manifest_ref"].endswith("/configuration-versions/1"))
        self.assertTrue(document["manifest_hash"].startswith("sha256:"))

    def test_unchanged_revalidation_keeps_the_same_version(self):
        campaign = self._campaign()
        self._command(campaign, "validate", expected=0)
        self._command(campaign, "validate", expected=1)
        self.assertEqual(campaign.configuration_version, 1)

    def test_stale_expected_version_is_a_conflict(self):
        campaign = self._campaign()
        self._command(campaign, "validate", expected=0)
        with self.assertRaises(CampaignControlConflict) as raised:
            self._command(campaign, "provision", expected=0)
        self.assertEqual(raised.exception.code, "CONFIGURATION_VERSION_CONFLICT")
        self.assertEqual(campaign.desired_state, "absent")
        self.assertFalse(self._events(campaign))
        run, _ = self._command(campaign, "provision", expected=1)
        self.assertEqual(run.status, "pending")

    def test_commands_require_validated_configuration_and_valid_transition(self):
        campaign = self._campaign()
        with self.assertRaises(CampaignControlConflict) as raised:
            self._command(campaign, "provision", expected=0)
        self.assertEqual(raised.exception.code, "CONFIGURATION_NOT_VALIDATED")
        self._command(campaign, "validate", expected=0)
        with self.assertRaises(CampaignControlConflict) as raised:
            self._command(campaign, "activate")
        self.assertEqual(raised.exception.code, "INVALID_STATE_TRANSITION")

    def test_external_effects_gate_blocks_provision(self):
        campaign = self._campaign()
        self._command(campaign, "validate", expected=0)
        with self.assertRaises(CampaignControlConflict) as raised:
            campaign.control_command(
                "provision",
                idempotency_key="gated",
                audit_reason="gate",
                expected_configuration_version=1,
                correlation_id="corr",
                external_effects_enabled=False,
            )
        self.assertEqual(raised.exception.code, "EXTERNAL_EFFECTS_DISABLED")

    # ------------------------------------------------------------------
    # queue -> claim/ack -> read-back
    # ------------------------------------------------------------------
    def test_full_lifecycle_advances_only_through_readback(self):
        campaign = self._campaign()
        self._command(campaign, "validate", expected=0)
        run, _ = self._command(campaign, "provision")
        self.assertEqual(run.status, "pending")
        self.assertEqual(run.delivery_state, "queued")
        self.assertEqual(run.operation, "provision")
        self.assertEqual(campaign.desired_state, "provisioned_disabled")
        self.assertEqual(campaign.effective_state, "unknown")
        self.assertEqual(campaign.control_status, "validated")

        # exactly one outbox event per command, carrying the contract payload
        events = self._events(campaign)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.event_uuid, run.event_uuid)
        self.assertEqual(event.event_type, "campaign.provision.requested.v1")
        self.assertEqual(event.delivery_state, "pending")
        self.assertEqual(event.design_request_revision, 0)
        self.assertEqual(event.organization_public_id, ORGANIZATION)
        for name in self.contract["outbox_events"]["payload_required_fields"]:
            self.assertIn(name, event.payload_json, name)
        self.assertEqual(event.payload_json["command_id"], event.event_uuid)
        self.assertEqual(event.payload_json["campaign_public_id"], campaign.code)
        self.assertEqual(event.payload_json["business_unit_public_id"], self.unit.code)
        self.assertEqual(event.payload_json["configuration_version"], 1)
        self.assertEqual(event.payload_json["manifest_hash"], campaign._control_manifest_hash())
        self.assertEqual(event.campaign_id, campaign.legacy_campaign_id)

        # acknowledgement proves durable intake only
        event._on_acknowledged()
        self.assertEqual(run.delivery_state, "accepted")
        self.assertTrue(run.delivery_acknowledged_at)
        self.assertEqual(run.status, "pending")
        self.assertEqual(campaign.effective_state, "unknown")
        self.assertEqual(campaign.desired_state, "provisioned_disabled")

        body, receipt = self._readback(campaign, run, "provisioned_disabled")
        self.assertEqual(receipt["status"], "APPLIED")
        self.assertEqual(receipt["outcome"], "COMPLETED")
        for name in self.contract["operations"]["campaign.actual_state.write"]["response_binding"]:
            self.assertEqual(receipt[name], "APPLIED" if name == "status" else body[name], name)
        self.assertTrue(receipt[self.contract["operations"]["campaign.actual_state.write"]["receipt_field"]])
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.readback_id.readback_uuid, receipt["readback_id"])
        self.assertEqual(campaign.effective_state, "provisioned_disabled")

        run, _ = self._command(campaign, "synthetic_test")
        self.assertEqual(self._events(campaign)[-1].event_type, "campaign.synthetic_test.requested.v1")
        self.assertEqual(campaign.effective_state, "provisioned_disabled")
        self._readback(campaign, run, "synthetic_tested")
        self.assertEqual(campaign.effective_state, "synthetic_tested")

        run, _ = self._command(campaign, "activate")
        self.assertEqual(self._events(campaign)[-1].event_type, "campaign.activate.requested.v1")
        self._readback(campaign, run, "active")
        self.assertEqual(campaign.desired_state, "active")
        self.assertEqual(campaign.effective_state, "active")

        run, _ = self._command(campaign, "disable")
        self.assertEqual(self._events(campaign)[-1].event_type, "campaign.disable.requested.v1")
        self._readback(campaign, run, "disabled")
        self.assertEqual(campaign.effective_state, "disabled")
        self.assertEqual(len(self._events(campaign)), 4)
        self.assertEqual(campaign.control_status, "validated")

    def test_pending_lag_is_not_drift(self):
        campaign = self._campaign()
        self._command(campaign, "validate", expected=0)
        run, _ = self._command(campaign, "provision")
        _, receipt = self._readback(campaign, run, "unknown")
        self.assertEqual(receipt["outcome"], "IN_PROGRESS")
        self.assertEqual(receipt["effective_state"], "unknown")
        self.assertEqual(run.status, "pending")
        self.assertEqual(campaign.desired_state, "provisioned_disabled")
        self.assertEqual(campaign.effective_state, "unknown")
        self.assertEqual(campaign.control_status, "validated")

    def test_stale_and_future_versions_are_rejected_without_mutation(self):
        campaign = self._campaign()
        self._command(campaign, "validate", expected=0)
        run, _ = self._command(campaign, "provision")
        for version in (0, 7):
            with self.assertRaises(CampaignControlConflict) as raised:
                self._readback(campaign, run, "provisioned_disabled", configuration_version=version)
            self.assertEqual(raised.exception.code, "STALE_CONFIGURATION_VERSION")
        with self.assertRaises(CampaignControlConflict) as raised:
            self._readback(campaign, run, "provisioned_disabled", manifest_hash="sha256:" + "0" * 64)
        self.assertEqual(raised.exception.code, "STALE_CONFIGURATION_VERSION")
        self.assertEqual(run.status, "pending")
        self.assertEqual(campaign.effective_state, "unknown")
        self.assertFalse(self.Readback.search([("campaign_id", "=", campaign.id)]))

    def test_unexpected_terminal_readback_fails_run_and_blocks_control(self):
        campaign = self._campaign()
        self._command(campaign, "validate", expected=0)
        run, _ = self._command(campaign, "provision")
        _, receipt = self._readback(campaign, run, "active")
        self.assertEqual(receipt["outcome"], "BLOCKED")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "UNEXPECTED_PROVIDER_STATE")
        self.assertEqual(campaign.control_status, "blocked")
        self.assertEqual(campaign.desired_state, "provisioned_disabled")
        self.assertEqual(campaign.effective_state, "active")
        self.assertIn("PROVIDER_STATE_DRIFT", campaign.control_blocked_reason_json)

    def test_duplicate_readback_returns_the_original_receipt(self):
        campaign = self._campaign()
        self._command(campaign, "validate", expected=0)
        run, _ = self._command(campaign, "provision")
        body, first = self._readback(campaign, run, "provisioned_disabled")
        second = campaign.control_readback(dict(body))
        self.assertEqual(first, second)
        self.assertEqual(self.Readback.search_count([("campaign_id", "=", campaign.id)]), 1)
        with self.assertRaises(CampaignControlConflict) as raised:
            campaign.control_readback({**body, "attempt": 2})
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(campaign.effective_state, "provisioned_disabled")

    def test_readback_request_is_validated_against_the_contract(self):
        campaign = self._campaign()
        self._command(campaign, "validate", expected=0)
        run, _ = self._command(campaign, "provision")
        schema_required = self.contract["actual_state_readback"]["required_fields"]
        self.assertEqual(list(READBACK_REQUIRED_FIELDS), schema_required)
        body = self._readback_body(campaign, run, "provisioned_disabled")
        for name in schema_required:
            with self.assertRaises(ValidationError, msg=name) as raised:
                campaign.control_readback({key: value for key, value in body.items() if key != name})
            self.assertIn("FIELD_REQUIRED", str(raised.exception))
        with self.assertRaises(ValidationError) as raised:
            campaign.control_readback({**body, "execution_status": "SUCCEEDED"})
        self.assertIn("UNKNOWN_FIELDS", str(raised.exception))
        for name, value in (
            ("operation", "rollback"),
            ("effective_state", "validated"),
            ("attempt", 0),
            ("evidence", "not-an-object"),
            ("observed_at", "yesterday"),
        ):
            with self.assertRaises(ValidationError, msg=name):
                campaign.control_readback({**body, name: value})
        with self.assertRaises(ValidationError) as raised:
            campaign.control_readback({**body, "command_id": str(uuid.uuid4()), "event_uuid": str(uuid.uuid4())})
        self.assertIn("COMMAND_ID_UNKNOWN", str(raised.exception))
        with self.assertRaises(ValidationError) as raised:
            campaign.control_readback({**body, "operation": "reconcile"})
        self.assertIn("COMMAND_BINDING_MISMATCH", str(raised.exception))
        self.assertEqual(run.status, "pending")

    def test_dead_letter_before_intake_fails_run_and_blocks_control(self):
        campaign = self._campaign()
        self._command(campaign, "validate", expected=0)
        run, _ = self._command(campaign, "provision")
        self._events(campaign)[0]._on_dead_letter()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.delivery_state, "dead_letter")
        self.assertEqual(run.error_code, "DELIVERY_DEAD_LETTER")
        self.assertEqual(campaign.control_status, "blocked")
        self.assertEqual(campaign.desired_state, "provisioned_disabled")
        self.assertEqual(campaign.effective_state, "unknown")

    # ------------------------------------------------------------------
    # idempotency and repeated commands
    # ------------------------------------------------------------------
    def test_retry_with_same_key_returns_original_run_and_event(self):
        campaign = self._campaign()
        self._command(campaign, "validate", expected=0)
        first, replayed_first = self._command(campaign, "provision", key="retry-1")
        second, replayed_second = self._command(campaign, "provision", key="retry-1", expected=99)
        self.assertFalse(replayed_first)
        self.assertTrue(replayed_second)
        self.assertEqual(first, second)
        self.assertEqual(len(self._events(campaign)), 1)
        self.assertEqual(self._events(campaign).event_uuid, first.event_uuid)

    def test_idempotency_key_reuse_across_commands_conflicts(self):
        campaign = self._campaign()
        self._command(campaign, "validate", key="shared", expected=0)
        with self.assertRaises(CampaignControlConflict) as raised:
            self._command(campaign, "provision", key="shared")
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_KEY_REUSED")

    def test_pending_run_blocks_state_commands_but_not_reconcile(self):
        campaign = self._campaign()
        self._command(campaign, "validate", expected=0)
        self._command(campaign, "provision")
        with self.assertRaises(CampaignControlConflict) as raised:
            self._command(campaign, "validate")
        self.assertEqual(raised.exception.code, "PROVISIONING_RUN_PENDING")
        run, _ = self._command(campaign, "reconcile")
        self.assertEqual(run.operation, "reconcile")

    def test_disable_activate_disable_on_one_configuration(self):
        campaign = self._activated(self._campaign())
        version = campaign.configuration_version
        run, _ = self._command(campaign, "disable")
        self._readback(campaign, run, "disabled")
        run, _ = self._command(campaign, "activate")
        self._readback(campaign, run, "active")
        run, _ = self._command(campaign, "disable")
        self._readback(campaign, run, "disabled")
        self.assertEqual(campaign.configuration_version, version)
        self.assertEqual(campaign.effective_state, "disabled")
        disables = self._events(campaign).filtered(
            lambda event: event.event_type == "campaign.disable.requested.v1"
        )
        self.assertEqual(len(disables), 2)
        self.assertEqual(len(set(disables.mapped("event_uuid"))), 2)

    def test_reconcile_twice_without_collision(self):
        campaign = self._provisioned(self._campaign())
        first, _ = self._command(campaign, "reconcile")
        second, _ = self._command(campaign, "reconcile")
        self.assertNotEqual(first.event_uuid, second.event_uuid)
        self.assertEqual(first.requested_state, "provisioned_disabled")
        reconciles = self._events(campaign).filtered(
            lambda event: event.event_type == "campaign.reconcile.requested.v1"
        )
        self.assertEqual(len(reconciles), 2)
        _, receipt = self._readback(campaign, first, "provisioned_disabled")
        self.assertEqual(receipt["outcome"], "COMPLETED")
        _, receipt = self._readback(campaign, second, "provisioned_disabled")
        self.assertEqual(receipt["outcome"], "COMPLETED")
        self.assertEqual({first.status, second.status}, {"succeeded"})
        self.assertEqual(campaign.control_status, "validated")

    def test_reconcile_observing_divergence_is_drift(self):
        campaign = self._provisioned(self._campaign())
        run, _ = self._command(campaign, "reconcile")
        _, receipt = self._readback(campaign, run, "disabled")
        self.assertEqual(receipt["outcome"], "DRIFT")
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(campaign.effective_state, "disabled")
        self.assertEqual(campaign.desired_state, "provisioned_disabled")
        self.assertEqual(campaign.control_status, "blocked")

    # ------------------------------------------------------------------
    # rollback through reconcile
    # ------------------------------------------------------------------
    def test_rollback_creates_a_new_version_and_enqueues_reconcile(self):
        campaign = self._provisioned(self._campaign())
        original = campaign.configuration_version_id
        campaign.write({"name": "Renamed for v2"})
        self._command(campaign, "validate")
        self.assertEqual(campaign.configuration_version, 2)
        superseded = campaign.configuration_version_id
        self.assertEqual(original.state, "superseded")
        self.assertEqual(campaign.desired_state, "provisioned_disabled")

        with self.assertRaises(CampaignControlConflict) as raised:
            self._command(campaign, "rollback", target_configuration_version=2)
        self.assertEqual(raised.exception.code, "ROLLBACK_TARGET_IS_CURRENT")

        run, _ = self._command(campaign, "rollback", target_configuration_version=1)
        self.assertEqual(campaign.configuration_version, 3)
        rollback = campaign.configuration_version_id
        self.assertEqual(rollback.configuration_json, original.configuration_json)
        self.assertEqual(rollback.source_version_id, original)
        self.assertEqual(rollback.predecessor_id, superseded)
        self.assertEqual(superseded.state, "rolled_back")
        self.assertEqual(run.command, "rollback")
        self.assertEqual(run.operation, "reconcile")
        self.assertEqual(run.status, "pending")
        event = self._events(campaign)[-1]
        self.assertEqual(event.event_type, "campaign.reconcile.requested.v1")
        self.assertEqual(event.payload_json["configuration_version"], 3)
        self.assertEqual(campaign.desired_state, "provisioned_disabled")
        self.assertEqual(campaign.control_status, "validated")
        _, receipt = self._readback(campaign, run, "provisioned_disabled")
        self.assertEqual(receipt["outcome"], "COMPLETED")
        self.assertEqual(receipt["configuration_version"], 3)

    # ------------------------------------------------------------------
    # projections and scope
    # ------------------------------------------------------------------
    def test_contract_read_projections(self):
        campaign = self._provisioned(self._campaign())
        read = campaign.contract_read_document()
        for name in self.contract["operations"]["campaigns.read"].get("response_fields", []) or (
            "campaign_public_id", "organization_public_id", "business_unit_public_id",
            "configuration_version", "lifecycle_status",
        ):
            self.assertIn(name, read)
        self.assertEqual(read["lifecycle_status"], "validated")
        self.assertIn(read["effective_state"], EFFECTIVE_STATE_KEYS)
        self.assertIn(read["desired_state"], EFFECTIVE_STATE_KEYS)
        desired = campaign.contract_desired_state_document()
        self.assertEqual(
            set(desired), set(self.contract["operations"]["desired_state.read"]["response_fields"])
        )
        self.assertEqual(desired["desired_state"], "provisioned_disabled")

    def test_scoped_lookup_enforces_the_scope_triple(self):
        campaign = self._campaign()
        Campaign = self.env["cc.campaign"]
        self.assertEqual(Campaign.control_lookup_scoped(ORGANIZATION, self.unit.code, campaign.code), campaign)
        self.assertEqual(Campaign.control_lookup_scoped(ORGANIZATION, self.unit.code, campaign.workspace_uuid), campaign)
        self.assertFalse(Campaign.control_lookup_scoped("OTHER_ORG", self.unit.code, campaign.code))
        self.assertFalse(Campaign.control_lookup_scoped(ORGANIZATION, "NOPE", campaign.code))
        self.assertFalse(Campaign.control_lookup_scoped(ORGANIZATION, self.unit.code, "COD-MISSING-OUT"))

    def test_commands_require_the_organization_identity(self):
        self.env["ir.config_parameter"].sudo().set_param(ORGANIZATION_PARAMETER, "")
        try:
            campaign = self._campaign()
            run, _ = self._command(campaign, "validate", expected=0)
            self.assertEqual(run.status, "failed")
            self.assertEqual(campaign.control_status, "blocked")
            self.assertIn("ORGANIZATION_IDENTITY_NOT_CONFIGURED", campaign.control_blocked_reason_json)
        finally:
            self.env["ir.config_parameter"].sudo().set_param(ORGANIZATION_PARAMETER, ORGANIZATION)

    # ------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------
    def test_control_state_is_system_controlled(self):
        campaign = self._provisioned(self._campaign())
        for values in (
            {"desired_state": "active"},
            {"effective_state": "active"},
            {"control_status": "validated"},
            {"configuration_version_id": False},
        ):
            with self.assertRaises(AccessError):
                campaign.write(values)
        run = campaign.provisioning_run_ids[:1]
        with self.assertRaises(AccessError):
            run.write({"status": "succeeded"})
        with self.assertRaises(AccessError):
            run.unlink()
        receipt = campaign.readback_ids[:1]
        with self.assertRaises(AccessError):
            receipt.write({"outcome": "completed"})
        with self.assertRaises(AccessError):
            receipt.unlink()
        version = campaign.configuration_version_id
        with self.assertRaises(AccessError):
            version.write({"configuration_json": {}})
        with self.assertRaises(AccessError):
            version.unlink()
        with self.assertRaises(AccessError):
            self.Campaign.create(
                {
                    "name": "Caller-controlled state",
                    "code": "COD-CALLERSTATE-OUT",
                    "cc_business_unit_id": self.unit.id,
                    "desired_state": "active",
                }
            )

    # ------------------------------------------------------------------
    # creation contract
    # ------------------------------------------------------------------
    def test_create_values_reject_unknown_and_protected_fields(self):
        Campaign = self.env["cc.campaign"]
        base = {"name": "API Campaign", "code": "COD-APICREATE-OUT", "business_unit_code": "COD"}
        for forbidden in (
            "desired_state", "effective_state", "control_status", "state", "active",
            "agent_ids", "company_id", "cc_business_unit_id", "business_unit_id", "id",
            "legacy_campaign_id", "workspace_uuid", "lifecycle_state",
        ):
            with self.assertRaises(ValidationError, msg=forbidden) as raised:
                Campaign.control_create_values({**base, forbidden: 1}, idempotency_key="k")
            self.assertIn("UNKNOWN_FIELDS", str(raised.exception))
        with self.assertRaises(ValidationError) as raised:
            Campaign.control_create_values({**base, "business_unit_code": "NOPE"}, idempotency_key="k")
        self.assertIn("BUSINESS_UNIT_UNKNOWN", str(raised.exception))
        with self.assertRaises(ValidationError) as raised:
            Campaign.control_create_values({**base, "max_retries": "3"}, idempotency_key="k")
        self.assertIn("FIELD_TYPE_INVALID", str(raised.exception))
        with self.assertRaises(ValidationError):
            Campaign.control_create_values({"name": "x", "code": "COD-X-OUT"}, idempotency_key="k")

    def test_create_is_idempotent_on_the_caller_key(self):
        Campaign = self.env["cc.campaign"]
        document = {
            "name": "API Created",
            "code": f"COD-API{uuid.uuid4().hex[:5].upper()}-OUT",
            "business_unit_code": "COD",
            "environment": "staging",
            "direction": "outbound",
            "idempotency_key": "create-1",
        }
        unit, values = Campaign.control_create_values(document, idempotency_key="create-1")
        self.assertEqual(unit, self.unit)
        self.assertFalse(values["design_automation_enabled"])
        campaign, created = Campaign.control_create(values)
        self.assertTrue(created)
        self.assertEqual(campaign.cc_business_unit_id, self.unit)
        self.assertEqual(campaign.control_status, "draft")
        self.assertEqual(campaign.desired_state, "absent")
        self.assertEqual(campaign.effective_state, "unknown")
        _, values_again = Campaign.control_create_values(document, idempotency_key="create-1")
        replay, created_again = Campaign.control_create(values_again)
        self.assertFalse(created_again)
        self.assertEqual(replay, campaign)
        self.assertEqual(Campaign.control_lookup(campaign.workspace_uuid), campaign)
        self.assertEqual(Campaign.control_lookup(campaign.code.lower()), campaign)
