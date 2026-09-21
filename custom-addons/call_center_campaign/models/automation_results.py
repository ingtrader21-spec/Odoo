"""Apply n8n automation results delivered by Middleware to CRM leads.

Contract: ``contracts/odoo/schemas/automation-results.v1.json``
(``automation_results.apply``). Every action targets one lead resolved by its
public identity inside the request's business unit and campaign, and maps onto
reviewed primitives only: an internal note, a to-do activity, or a canonical
journey stage change that respects ``call.center.status.transition``.
"""

import uuid
from datetime import date

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import MissingError, ValidationError

AUTOMATION_ACTION_TYPES = ("CREATE_INTERNAL_SUMMARY", "SET_NEXT_ACTION", "CHANGE_STATUS")
AUTOMATION_ENTITY_TYPES = {"lead", "opportunity", "crm.lead"}
MAX_TEXT = 4000


def public_record_id(model_name, record_id):
    """Opaque, stable public identifier for a record; never the database id."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"odoo:{model_name}:{record_id}"))


def _text_value(values, name, *, required=True, limit=MAX_TEXT):
    value = values.get(name)
    if value is None or value == "":
        if required:
            raise ValidationError(f"ACTION_VALUE_REQUIRED:{name}")
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValidationError(f"ACTION_VALUE_INVALID:{name}")
    return value.strip()


class CrmLeadAutomationResults(models.Model):
    _inherit = "crm.lead"

    @api.model
    def _automation_validate_action(self, action):
        if not isinstance(action, dict):
            raise ValidationError("ACTION_INVALID")
        for name in ("action_type", "entity_type", "entity_id", "values"):
            if name not in action:
                raise ValidationError(f"ACTION_FIELD_REQUIRED:{name}")
        if action["action_type"] not in AUTOMATION_ACTION_TYPES:
            raise ValidationError("UNSUPPORTED_ACTION_TYPE")
        if action["entity_type"] not in AUTOMATION_ENTITY_TYPES:
            raise ValidationError("UNSUPPORTED_ENTITY_TYPE")
        if not isinstance(action["entity_id"], str) or not 1 <= len(action["entity_id"]) <= 128:
            raise ValidationError("ACTION_FIELD_INVALID:entity_id")
        if not isinstance(action["values"], dict):
            raise ValidationError("ACTION_FIELD_INVALID:values")
        return action

    @api.model
    def _automation_resolve_lead(self, entity_id, campaign):
        """One lead by public identity inside the request campaign, or 404."""
        leads = self.search(
            [
                ("call_center_campaign_id", "=", campaign.id),
                "|",
                ("integration_uuid", "=", entity_id),
                ("external_source_id", "=", entity_id),
            ],
            limit=2,
        )
        if len(leads) != 1:
            raise MissingError(_("ENTITY_NOT_FOUND"))
        return leads

    def _automation_stage_for_status(self, status):
        self.ensure_one()
        # crm.stage has no team_id on Odoo 17+ (stages are shared across
        # teams); the canonical journey status is the only selector.
        Stage = self.env["crm.stage"]
        domain = [("canonical_journey_status_id.code", "=", status)]
        stage = Stage.search(domain, order="sequence, id", limit=1)
        if not stage:
            raise ValidationError("STATUS_UNKNOWN")
        current = self.stage_id.canonical_journey_status_id
        target = stage.canonical_journey_status_id
        if current and current != target:
            allowed = self.env["call.center.status.transition"].search_count(
                [("from_status_id", "=", current.id), ("to_status_id", "=", target.id)]
            )
            if not allowed:
                raise ValidationError("STATUS_TRANSITION_NOT_ALLOWED")
        return stage

    def _automation_apply_action(self, action, actor):
        """Apply one validated action; returns its public receipt."""
        self.ensure_one()
        values = action["values"]
        action_type = action["action_type"]
        author = f"{actor['actor_type']}:{actor['actor_id']}"
        if action_type == "CREATE_INTERNAL_SUMMARY":
            summary = _text_value(values, "summary")
            message = self.message_post(
                body=Markup("<p><strong>%s</strong></p><p>%s</p>")
                % (escape(_("Automation summary (%s)") % author), escape(summary)),
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )
            return {"action_type": action_type, "message_id": public_record_id("mail.message", message.id)}
        if action_type == "SET_NEXT_ACTION":
            summary = _text_value(values, "summary", limit=512)
            due_text = _text_value(values, "due_date", required=False, limit=32)
            try:
                deadline = date.fromisoformat(due_text) if due_text else fields.Date.context_today(self)
            except ValueError as exc:
                raise ValidationError("ACTION_VALUE_INVALID:due_date") from exc
            note = _text_value(values, "note", required=False)
            activity = self.activity_schedule(
                "mail.mail_activity_data_todo",
                date_deadline=deadline,
                summary=summary,
                note=escape(note) if note else False,
                user_id=(self.user_id or self.env.user).id,
            )
            self.write({"lifecycle_next_action": summary})
            return {"action_type": action_type, "activity_id": public_record_id("mail.activity", activity.id)}
        status = _text_value(values, "status", limit=64).upper()
        reason = _text_value(values, "reason", required=False, limit=512)
        stage = self._automation_stage_for_status(status)
        if stage != self.stage_id:
            self.write(
                {
                    "stage_id": stage.id,
                    "lifecycle_reason": reason or _("Automation status change (%s)") % author,
                }
            )
        return {
            "action_type": action_type,
            "status": stage.canonical_journey_status_id.code,
            "stage_id": public_record_id("crm.stage", stage.id),
        }

    @api.model
    def apply_automation_result(self, campaign, actions, actor):
        """Apply every action atomically; any failure rolls the whole result back."""
        applied = []
        for raw in actions:
            action = self._automation_validate_action(raw)
            lead = self._automation_resolve_lead(action["entity_id"], campaign)
            applied.append(
                {
                    "entity_type": action["entity_type"],
                    "entity_id": action["entity_id"],
                    **lead._automation_apply_action(action, actor),
                }
            )
        return applied
