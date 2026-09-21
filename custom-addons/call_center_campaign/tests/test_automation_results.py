"""``automation_results.apply``: allowlisted n8n actions applied to leads."""

import uuid

from odoo.exceptions import MissingError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..models.automation_results import AUTOMATION_ACTION_TYPES, public_record_id


@tagged("post_install", "-at_install")
class TestAutomationResults(TransactionCase):
    def setUp(self):
        super().setUp()
        self.unit = self.env.ref("call_center_core.business_unit_digital")
        self.campaign = self.env["call.center.campaign"].create(
            {
                "name": "Automation Campaign",
                "code": f"COD-AUTO{uuid.uuid4().hex[:6].upper()}-OUT",
                "business_unit_id": self.unit.id,
                "direction": "outbound",
                "purpose_code": "TEST",
                "design_automation_enabled": True,
            }
        )
        self.validating = self.env.ref("call_center_campaign.stage_validating")
        self.lead = self.env["crm.lead"].create(
            {
                "name": "Automation lead",
                "business_unit_id": self.unit.id,
                "call_center_campaign_id": self.campaign.id,
                "external_source_id": f"ext-{uuid.uuid4().hex[:8]}",
                "stage_id": self.validating.id,
            }
        )
        self.actor = {"actor_type": "n8n", "actor_id": "workflow-runner"}
        self.Lead = self.env["crm.lead"]

    def _action(self, action_type, values, entity_id=None):
        return {
            "action_type": action_type,
            "entity_type": "lead",
            "entity_id": entity_id or self.lead.external_source_id,
            "values": values,
        }

    def test_action_types_match_the_shared_schema(self):
        self.assertEqual(
            set(AUTOMATION_ACTION_TYPES), {"CREATE_INTERNAL_SUMMARY", "SET_NEXT_ACTION", "CHANGE_STATUS"}
        )

    def test_internal_summary_posts_a_note_with_a_public_receipt(self):
        before = self.lead.message_ids
        applied = self.Lead.apply_automation_result(
            self.campaign, [self._action("CREATE_INTERNAL_SUMMARY", {"summary": "Caller asked for a quote"})], self.actor
        )
        message = self.lead.message_ids - before
        self.assertEqual(len(message), 1)
        self.assertIn("Caller asked for a quote", message.body)
        self.assertEqual(applied[0]["message_id"], public_record_id("mail.message", message.id))
        self.assertNotEqual(applied[0]["message_id"], str(message.id))
        self.assertNotIn("partner_id", applied[0])

    def test_next_action_schedules_a_todo_and_records_the_lifecycle_hint(self):
        applied = self.Lead.apply_automation_result(
            self.campaign,
            [self._action("SET_NEXT_ACTION", {"summary": "Call back Monday", "due_date": "2026-09-21"})],
            self.actor,
        )
        activity = self.lead.activity_ids.filtered(lambda act: act.summary == "Call back Monday")
        self.assertEqual(len(activity), 1)
        self.assertEqual(str(activity.date_deadline), "2026-09-21")
        self.assertEqual(self.lead.lifecycle_next_action, "Call back Monday")
        self.assertEqual(applied[0]["activity_id"], public_record_id("mail.activity", activity.id))

    def test_change_status_follows_the_canonical_journey_transitions(self):
        applied = self.Lead.apply_automation_result(
            self.campaign, [self._action("CHANGE_STATUS", {"status": "qualification"})], self.actor
        )
        self.assertEqual(self.lead.stage_id.canonical_journey_status_id.code, "QUALIFICATION")
        self.assertEqual(applied[0]["status"], "QUALIFICATION")
        with self.assertRaises(ValidationError) as raised:
            self.Lead.apply_automation_result(
                self.campaign, [self._action("CHANGE_STATUS", {"status": "RETENTION"})], self.actor
            )
        self.assertIn("STATUS_TRANSITION_NOT_ALLOWED", str(raised.exception))
        with self.assertRaises(ValidationError) as raised:
            self.Lead.apply_automation_result(
                self.campaign, [self._action("CHANGE_STATUS", {"status": "NOT_A_STATUS"})], self.actor
            )
        self.assertIn("STATUS_UNKNOWN", str(raised.exception))

    def test_actions_are_validated_and_scoped(self):
        with self.assertRaises(ValidationError):
            self.Lead.apply_automation_result(
                self.campaign, [{**self._action("CREATE_INTERNAL_SUMMARY", {}), "action_type": "SEND_EMAIL"}], self.actor
            )
        with self.assertRaises(ValidationError):
            self.Lead.apply_automation_result(
                self.campaign, [{**self._action("CREATE_INTERNAL_SUMMARY", {"summary": "x"}), "entity_type": "res.partner"}], self.actor
            )
        with self.assertRaises(ValidationError):
            self.Lead.apply_automation_result(
                self.campaign, [self._action("CREATE_INTERNAL_SUMMARY", {})], self.actor
            )
        with self.assertRaises(MissingError):
            self.Lead.apply_automation_result(
                self.campaign, [self._action("CREATE_INTERNAL_SUMMARY", {"summary": "x"}, entity_id="unknown-lead")], self.actor
            )
        other = self.env["call.center.campaign"].create(
            {
                "name": "Other campaign",
                "code": f"COD-OTHER{uuid.uuid4().hex[:5].upper()}-OUT",
                "business_unit_id": self.unit.id,
                "direction": "outbound",
                "purpose_code": "TEST",
                "design_automation_enabled": True,
            }
        )
        with self.assertRaises(MissingError):
            self.Lead.apply_automation_result(
                other, [self._action("CREATE_INTERNAL_SUMMARY", {"summary": "x"})], self.actor
            )

    def test_a_failing_action_rolls_back_the_whole_result(self):
        before = len(self.lead.message_ids)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.Lead.apply_automation_result(
                self.campaign,
                [
                    self._action("CREATE_INTERNAL_SUMMARY", {"summary": "first"}),
                    self._action("CHANGE_STATUS", {"status": "NOT_A_STATUS"}),
                ],
                self.actor,
            )
        self.lead.invalidate_recordset(["message_ids"])
        self.assertEqual(len(self.lead.message_ids), before)
