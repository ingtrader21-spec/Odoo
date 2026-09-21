"""Canonical campaign control lifecycle on ``cc.campaign``.

Vocabulary and route semantics come from the shared Middleware contract
``contracts/odoo/campaign-control.v1.json``:

* ``desired_state`` / ``effective_state`` use only the contract's
  effective-state enum (``unknown``, ``absent``, ``provisioned_disabled``,
  ``synthetic_tested``, ``active``, ``disabled``).
* ``control_status`` carries Odoo-side configuration progress (``draft``,
  ``validated``, ``blocked``); it is never an effective state.
* Provisioning-run ``status`` (pending/succeeded/failed) is separate from the
  run's outbox ``delivery_state`` (queued/accepted/dead_letter).

Odoo owns the desired configuration and desired state. Middleware claims the
outbox command, acknowledges durable intake (delivery only), executes the
saga, and posts an authenticated actual-state read-back. Only that read-back
moves ``effective_state`` or completes a run.
"""

import hashlib
import uuid
from datetime import datetime, timezone

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.call_center_campaign.models.outbox import canonical_json

EFFECTIVE_STATES = [
    ("unknown", "Unknown"),
    ("absent", "Absent"),
    ("provisioned_disabled", "Provisioned Disabled"),
    ("synthetic_tested", "Synthetic Tested"),
    ("active", "Active"),
    ("disabled", "Disabled"),
]
EFFECTIVE_STATE_KEYS = {key for key, _label in EFFECTIVE_STATES}
CONTROL_STATUSES = [
    ("draft", "Draft"),
    ("validated", "Validated"),
    ("blocked", "Blocked"),
]
OPERATIONS = ["provision", "synthetic_test", "activate", "disable", "reconcile"]
EVENT_TYPE_BY_OPERATION = {
    operation: f"campaign.{operation}.requested.v1" for operation in OPERATIONS
}
OUTBOX_EVENT_ALLOWLIST = [EVENT_TYPE_BY_OPERATION[operation] for operation in OPERATIONS]
RUN_STATUSES = [
    ("pending", "Pending"),
    ("succeeded", "Succeeded"),
    ("failed", "Failed"),
]
RUN_DELIVERY_STATES = [
    ("none", "No Delivery"),
    ("queued", "Queued"),
    ("accepted", "Accepted"),
    ("dead_letter", "Dead Letter"),
]
READBACK_REQUIRED_FIELDS = (
    "event_uuid",
    "command_id",
    "idempotency_key",
    "attempt",
    "organization_public_id",
    "business_unit_public_id",
    "campaign_public_id",
    "configuration_version",
    "operation",
    "effective_state",
    "manifest_ref",
    "manifest_hash",
    "evidence",
    "observed_at",
    "correlation_id",
    "causation_id",
)
CONTROL_CAPABILITY = object()
CONFIGURATION_SCHEMA = "campaign-configuration.v1"
EVENT_SCHEMA_VERSION = "1.0"
RUN_NAMESPACE = "cc-campaign-run"
EVENT_NAMESPACE = "cc-campaign-control"
CREATE_NAMESPACE = "cc-campaign-create"
READBACK_NAMESPACE = "cc-campaign-readback"
ORGANIZATION_PARAMETER = "codestra.integration.organization_public_id"
ENVIRONMENT_CODES = {
    "development": "TEST",
    "staging": "STAGING",
    "production": "PRODUCTION",
}
CONTROL_FIELDS = {
    "desired_state",
    "effective_state",
    "control_status",
    "configuration_version_id",
    "control_blocked_reason_json",
}

# command -> contract operation, permitted current desired states, target
# desired state. ``validate`` is Odoo-local (no provider execution, no outbox
# event). ``rollback`` creates a new immutable configuration version and
# enqueues the contract's ``reconcile`` operation; there is no rollback event.
# ``reconcile`` may run alongside a pending state-changing run.
COMMANDS = {
    "validate": {"operation": None, "allowed": EFFECTIVE_STATE_KEYS, "target": None},
    "provision": {
        "operation": "provision",
        "allowed": {"absent", "unknown", "disabled"},
        "target": "provisioned_disabled",
        "gated": True,
    },
    "synthetic_test": {
        "operation": "synthetic_test",
        "allowed": {"provisioned_disabled"},
        "target": "synthetic_tested",
    },
    "activate": {
        "operation": "activate",
        "allowed": {"synthetic_tested", "disabled"},
        "target": "active",
        "gated": True,
    },
    "disable": {
        "operation": "disable",
        "allowed": {"active", "synthetic_tested"},
        "target": "disabled",
    },
    "reconcile": {
        "operation": "reconcile",
        "allowed": EFFECTIVE_STATE_KEYS,
        "target": None,
        "concurrent": True,
    },
    "rollback": {"operation": "reconcile", "allowed": EFFECTIVE_STATE_KEYS, "target": None},
}

# Explicit request schema for campaign creation. Anything outside this map is
# rejected; state, ownership, and database identities are never caller-set.
CREATE_FIELDS = {
    "name": str,
    "code": str,
    "business_unit_code": str,
    "business_unit_uuid": str,
    "environment": str,
    "campaign_type": str,
    "direction": str,
    "timezone": str,
    "calling_hour_start": (int, float),
    "calling_hour_end": (int, float),
    "consent_required": bool,
    "dnc_enforced": bool,
    "dialer_mode": str,
    "routing_strategy": str,
    "max_call_attempts": int,
    "max_retries": int,
    "purpose_code": str,
    "start_date": str,
    "end_date": str,
    "template_code": str,
    "application_code": str,
}
CREATE_ENVELOPE_FIELDS = {"idempotency_key", "correlation_id", "causation_id"}
CREATE_PASSTHROUGH = {
    "name", "code", "campaign_type", "direction", "timezone",
    "calling_hour_start", "calling_hour_end", "consent_required", "dnc_enforced",
    "dialer_mode", "routing_strategy", "max_call_attempts", "max_retries",
    "purpose_code", "start_date", "end_date",
}


class CampaignControlConflict(ValidationError):
    """A deterministic state, version, or idempotency conflict (HTTP 409)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _digest(document):
    return hashlib.sha256(canonical_json(document).encode()).hexdigest()


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValidationError(f"FIELD_INVALID:{name}")
    return value.strip()


def _observed_at(value):
    """Parse an RFC 3339 timestamp into the naive-UTC datetime Odoo stores."""
    text = _text(value, "observed_at", 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("FIELD_INVALID:observed_at") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class CcCampaignProvisioningRun(models.Model):
    _name = "cc.campaign.provisioning.run"
    _description = "Campaign Provisioning Run"
    _inherit = "cc.campaign.scoped.mixin"
    _order = "requested_at desc, id desc"

    run_uuid = fields.Char(required=True, readonly=True, index=True, copy=False)
    command = fields.Selection(
        [(key, key.replace("_", " ").title()) for key in COMMANDS],
        required=True,
        readonly=True,
        index=True,
    )
    operation = fields.Selection(
        [(operation, operation) for operation in OPERATIONS],
        readonly=True,
        index=True,
        help="Contract operation carried by the outbox event (none for validate).",
    )
    requested_state = fields.Selection(EFFECTIVE_STATES, required=True, readonly=True)
    configuration_version_id = fields.Many2one(
        "cc.campaign.configuration.version", readonly=True, ondelete="restrict"
    )
    configuration_version = fields.Integer(required=True, readonly=True)
    status = fields.Selection(
        RUN_STATUSES, required=True, default="pending", readonly=True, index=True
    )
    delivery_state = fields.Selection(
        RUN_DELIVERY_STATES, required=True, default="none", readonly=True, index=True
    )
    delivery_acknowledged_at = fields.Datetime(readonly=True)
    event_uuid = fields.Char(
        readonly=True,
        index=True,
        copy=False,
        help="Outbox event UUID; the contract command_id. Stable across retries.",
    )
    idempotency_key = fields.Char(required=True, readonly=True, index=True)
    correlation_id = fields.Char(required=True, readonly=True, index=True)
    causation_id = fields.Char(readonly=True)
    audit_reason = fields.Char(required=True, readonly=True)
    requested_by = fields.Many2one("res.users", required=True, readonly=True)
    requested_at = fields.Datetime(
        required=True, readonly=True, default=fields.Datetime.now, index=True
    )
    completed_at = fields.Datetime(readonly=True)
    readback_id = fields.Many2one("cc.campaign.readback", readonly=True, ondelete="restrict")
    error_code = fields.Char(readonly=True)
    error_detail = fields.Char(readonly=True)

    _run_uuid_unique = models.Constraint(
        "unique(run_uuid)", "Provisioning run UUIDs must be unique."
    )
    _event_uuid_unique = models.Constraint(
        "unique(event_uuid)", "A provisioning run binds one outbox command."
    )
    _idempotency_unique = models.Constraint(
        "unique(campaign_id, idempotency_key)",
        "Idempotency keys must be unique per campaign.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get("_cc_control_capability") is not CONTROL_CAPABILITY:
            raise AccessError("Provisioning runs are created by the campaign lifecycle.")
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get("_cc_control_capability") is not CONTROL_CAPABILITY:
            raise AccessError("Provisioning runs are updated by read-back only.")
        return super().write(vals)

    def unlink(self):
        raise AccessError("Provisioning runs are immutable evidence.")

    @api.model
    def _create_internal(self, vals):
        return self.sudo().with_context(_cc_control_capability=CONTROL_CAPABILITY).create(vals)

    def _control_write(self, vals):
        return self.sudo().with_context(_cc_control_capability=CONTROL_CAPABILITY).write(vals)

    def control_document(self):
        self.ensure_one()
        return {
            "run_id": self.run_uuid,
            "command": self.command,
            "operation": self.operation or None,
            "command_id": self.event_uuid or None,
            "status": self.status,
            "delivery_state": self.delivery_state,
            "delivery_acknowledged_at": (
                fields.Datetime.to_string(self.delivery_acknowledged_at)
                if self.delivery_acknowledged_at
                else None
            ),
            "requested_state": self.requested_state,
            "configuration_version": self.configuration_version,
            "idempotency_key": self.idempotency_key,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id or None,
            "audit_reason": self.audit_reason,
            "requested_at": fields.Datetime.to_string(self.requested_at),
            "completed_at": (
                fields.Datetime.to_string(self.completed_at) if self.completed_at else None
            ),
            "readback_id": self.readback_id.readback_uuid or None,
            "error": (
                {"code": self.error_code, "detail": self.error_detail or None}
                if self.error_code
                else None
            ),
        }


class CcCampaignReadback(models.Model):
    """Immutable receipt for one ``campaign.actual_state.write`` request."""

    _name = "cc.campaign.readback"
    _description = "Campaign Actual-State Read-back Receipt"
    _inherit = "cc.campaign.scoped.mixin"
    _order = "received_at desc, id desc"

    readback_uuid = fields.Char(required=True, readonly=True, index=True, copy=False)
    run_id = fields.Many2one("cc.campaign.provisioning.run", readonly=True, ondelete="restrict")
    idempotency_key = fields.Char(required=True, readonly=True, index=True)
    request_hash = fields.Char(required=True, size=64, readonly=True)
    attempt = fields.Integer(required=True, readonly=True)
    event_uuid = fields.Char(required=True, readonly=True, index=True)
    command_id = fields.Char(required=True, readonly=True, index=True)
    operation = fields.Selection(
        [(operation, operation) for operation in OPERATIONS], required=True, readonly=True
    )
    effective_state = fields.Selection(EFFECTIVE_STATES, required=True, readonly=True)
    configuration_version = fields.Integer(required=True, readonly=True)
    manifest_ref = fields.Char(required=True, readonly=True)
    manifest_hash = fields.Char(required=True, readonly=True)
    evidence_json = fields.Json(readonly=True)
    observed_at = fields.Datetime(required=True, readonly=True)
    received_at = fields.Datetime(required=True, readonly=True, default=fields.Datetime.now)
    correlation_id = fields.Char(required=True, readonly=True, index=True)
    causation_id = fields.Char(required=True, readonly=True)
    outcome = fields.Selection(
        [
            ("completed", "Completed"),
            ("in_progress", "In Progress"),
            ("confirmed", "Confirmed"),
            ("drift", "Drift"),
            ("blocked", "Blocked"),
        ],
        required=True,
        readonly=True,
    )

    _readback_uuid_unique = models.Constraint(
        "unique(readback_uuid)", "Read-back receipts must be unique."
    )
    _readback_idempotency_unique = models.Constraint(
        "unique(campaign_id, idempotency_key)",
        "Read-back idempotency keys must be unique per campaign.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get("_cc_control_capability") is not CONTROL_CAPABILITY:
            raise AccessError("Read-back receipts are created by the lifecycle only.")
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError("Read-back receipts are immutable.")

    def unlink(self):
        raise AccessError("Read-back receipts are immutable evidence.")

    def receipt(self):
        """Contract response: bound echoes plus the receipt identifier."""
        self.ensure_one()
        return {
            "status": "APPLIED",
            "event_uuid": self.event_uuid,
            "command_id": self.command_id,
            "configuration_version": self.configuration_version,
            "effective_state": self.effective_state,
            "correlation_id": self.correlation_id,
            "readback_id": self.readback_uuid,
            "outcome": self.outcome.upper(),
        }


class CcCampaignLifecycle(models.Model):
    _inherit = "cc.campaign"

    control_status = fields.Selection(
        CONTROL_STATUSES, default="draft", required=True, readonly=True, index=True, copy=False
    )
    desired_state = fields.Selection(
        EFFECTIVE_STATES, default="absent", required=True, readonly=True, index=True, copy=False
    )
    effective_state = fields.Selection(
        EFFECTIVE_STATES, default="unknown", required=True, readonly=True, index=True, copy=False
    )
    configuration_version = fields.Integer(
        compute="_compute_configuration_version", readonly=True
    )
    control_blocked_reason_json = fields.Json(readonly=True, copy=False)
    provisioning_run_ids = fields.One2many(
        "cc.campaign.provisioning.run", "campaign_id", readonly=True
    )
    readback_ids = fields.One2many("cc.campaign.readback", "campaign_id", readonly=True)

    @api.depends("configuration_version_id.version")
    def _compute_configuration_version(self):
        for campaign in self:
            campaign.configuration_version = campaign.configuration_version_id.version or 0

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get("_cc_control_capability") is not CONTROL_CAPABILITY:
            for vals in vals_list:
                if CONTROL_FIELDS & vals.keys():
                    raise AccessError("Campaign control state is system controlled.")
        return super().create(vals_list)

    def write(self, vals):
        if (
            CONTROL_FIELDS & vals.keys()
            and self.env.context.get("_cc_control_capability") is not CONTROL_CAPABILITY
        ):
            raise AccessError("Campaign control state is system controlled.")
        return super().write(vals)

    def _control_write(self, vals):
        return self.sudo().with_context(_cc_control_capability=CONTROL_CAPABILITY).write(vals)

    def _control_lock(self):
        """Serialize lifecycle mutations per campaign for the transaction."""
        self.ensure_one()
        self.lock_for_update()
        self.invalidate_recordset()
        return self

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    @api.model
    def control_organization_public_id(self):
        """The organization (tenant) public ID this Odoo deployment serves."""
        value = self.env["ir.config_parameter"].sudo().get_param(ORGANIZATION_PARAMETER, "")
        return str(value or "").strip()

    @api.model
    def control_lookup(self, public_id):
        """Resolve a campaign by canonical code, workspace UUID, or legacy UUID."""
        if not isinstance(public_id, str) or not public_id:
            return self.browse()
        return self.sudo().with_context(active_test=False).search(
            [
                "|",
                "|",
                ("code", "=", public_id.strip().upper()),
                ("workspace_uuid", "=", public_id),
                ("integration_uuid", "=", public_id),
            ],
            limit=1,
        )

    @api.model
    def control_lookup_scoped(self, organization_public_id, business_unit_public_id, campaign_public_id):
        """Resolve a campaign by the contract scope triple; empty when any part mismatches."""
        organization = self.control_organization_public_id()
        if not organization or organization != _text(organization_public_id, "organization_public_id", 128):
            return self.browse()
        unit_code = _text(business_unit_public_id, "business_unit_public_id", 128).upper()
        campaign = self.control_lookup(_text(campaign_public_id, "campaign_public_id", 128))
        if not campaign or campaign.cc_business_unit_id.code != unit_code:
            return self.browse()
        return campaign

    def _control_environment(self):
        self.ensure_one()
        return ENVIRONMENT_CODES.get(self.environment, "STAGING")

    def _control_manifest_ref(self, version=None):
        self.ensure_one()
        number = version.version if version else self.configuration_version
        return f"odoo://cc.campaign/{self.workspace_uuid}/configuration-versions/{number}"

    def _control_manifest_hash(self, version=None):
        self.ensure_one()
        version = version or self.configuration_version_id
        digest = version.configuration_hash if version else _digest({})
        return f"sha256:{digest}"

    # ------------------------------------------------------------------
    # Projections
    # ------------------------------------------------------------------
    def control_document(self):
        """Administrative platform projection (``/platform/v1/campaigns``)."""
        self.ensure_one()
        version = self.configuration_version_id
        runs = self.provisioning_run_ids
        pending = runs.filtered(lambda run: run.status == "pending")
        return {
            "campaign_public_id": self.code,
            "campaign_uuid": self.workspace_uuid,
            "organization_public_id": self.control_organization_public_id() or None,
            "business_unit_public_id": self.cc_business_unit_id.code,
            "environment": self._control_environment(),
            "control_status": self.control_status,
            "desired_state": self.desired_state,
            "effective_state": self.effective_state,
            "configuration_version": self.configuration_version,
            "manifest_ref": self._control_manifest_ref(),
            "manifest_hash": self._control_manifest_hash(),
            "configuration_state": version.state or None,
            "validation_errors": version.validation_errors_json or [],
            "blocked_reason": self.control_blocked_reason_json or [],
            "pending_run_ids": pending.mapped("run_uuid"),
            "last_run": runs[:1].control_document() if runs else None,
        }

    def contract_read_document(self):
        """``campaigns.read`` response (``internal://odoo/campaigns/read/v1``)."""
        self.ensure_one()
        return {
            "campaign_public_id": self.code,
            "organization_public_id": self.control_organization_public_id(),
            "business_unit_public_id": self.cc_business_unit_id.code,
            "configuration_version": self.configuration_version,
            "lifecycle_status": self.control_status,
            "effective_state": self.effective_state,
            "desired_state": self.desired_state,
        }

    def contract_desired_state_document(self):
        """``desired_state.read`` response (``internal://odoo/desired-state/read/v1``)."""
        self.ensure_one()
        return {
            "campaign_public_id": self.code,
            "configuration_version": self.configuration_version,
            "desired_state": self.desired_state,
            "manifest_ref": self._control_manifest_ref(),
            "manifest_hash": self._control_manifest_hash(),
        }

    # ------------------------------------------------------------------
    # Configuration snapshots
    # ------------------------------------------------------------------
    def _control_configuration_document(self):
        self.ensure_one()
        template_version = self.template_version_id
        return {
            "schema_version": CONFIGURATION_SCHEMA,
            "campaign_public_id": self.code,
            "campaign_uuid": self.workspace_uuid,
            "organization_public_id": self.control_organization_public_id(),
            "business_unit_public_id": self.cc_business_unit_id.code,
            "environment": self._control_environment(),
            "application": self.application_id.code or None,
            "template": {
                "code": self.template_id.code or None,
                "version": template_version.version or None,
                "content_hash": template_version.content_hash or None,
            },
            "configuration": {
                "name": self.name,
                "direction": self.direction,
                "campaign_type": self.campaign_type,
                "timezone": self.timezone,
                "calling_hour_start": self.calling_hour_start,
                "calling_hour_end": self.calling_hour_end,
                "consent_required": self.consent_required,
                "dnc_enforced": self.dnc_enforced,
                "dialer_mode": self.dialer_mode,
                "routing_strategy": self.routing_strategy,
                "max_call_attempts": self.max_call_attempts,
                "max_retries": self.max_retries,
                "purpose_code": self.purpose_code or None,
                "teams": sorted(self.team_ids.mapped("name")),
                "supervisors": sorted(self.supervisor_ids.mapped("login")),
                "routing_rules": [
                    {
                        "code": rule.code,
                        "sequence": rule.sequence,
                        "priority": rule.priority,
                        "channel": rule.channel or None,
                        "language_code": rule.language_code or None,
                        "territory": rule.territory or None,
                        "skill": rule.skill or None,
                        "is_fallback": rule.is_fallback,
                    }
                    for rule in self._control_routing_rules()
                ],
            },
        }

    def _control_routing_rules(self):
        """Routing rules are an optional projection: the routing-rule model is
        not part of every reviewed baseline, so the snapshot carries an empty
        list until call.center.campaign exposes routing_rule_ids."""
        self.ensure_one()
        if "routing_rule_ids" not in self._fields:
            return []
        return self.routing_rule_ids.filtered("active").sorted(
            key=lambda rule: (rule.sequence, rule.priority, rule.id)
        )

    def _control_validation_errors(self, document):
        self.ensure_one()
        errors = []
        if not self.cc_business_unit_id:
            errors.append("BUSINESS_UNIT_REQUIRED")
        if not document["organization_public_id"]:
            errors.append("ORGANIZATION_IDENTITY_NOT_CONFIGURED")
        configuration = document["configuration"]
        if configuration["calling_hour_start"] >= configuration["calling_hour_end"]:
            errors.append("CALLING_HOURS_INVALID")
        if self.identifier_status != "canonical":
            errors.append("CANONICAL_IDENTIFIER_REQUIRED")
        if self.template_id and not self.template_version_id:
            errors.append("TEMPLATE_VERSION_REQUIRED")
        if self.template_version_id and self.template_version_id.state != "approved":
            errors.append("TEMPLATE_VERSION_NOT_APPROVED")
        return sorted(set(errors))

    def _control_next_version_number(self):
        self.ensure_one()
        versions = self.env["cc.campaign.configuration.version"].sudo().search(
            [("campaign_id", "=", self.id)], order="version desc", limit=1
        )
        return (versions.version or 0) + 1

    def _control_snapshot(self):
        """Return the current configuration version, creating one on change."""
        self.ensure_one()
        document = self._control_configuration_document()
        errors = self._control_validation_errors(document)
        current = self.configuration_version_id
        if (
            current
            and current.configuration_hash == _digest(document)
            and (current.validation_errors_json or []) == errors
        ):
            return current
        version = self.env["cc.campaign.configuration.version"]._create_internal(
            {
                "campaign_id": self.id,
                "template_version_id": self.template_version_id.id or False,
                "version": self._control_next_version_number(),
                "state": "blocked" if errors else "published",
                "configuration_json": document,
                "validation_errors_json": errors,
                "predecessor_id": current.id or False,
            }
        )
        if current and current.state == "published":
            current._control_write({"state": "superseded"})
        return version

    def _control_rollback_version(self, target_configuration_version):
        self.ensure_one()
        try:
            target_number = int(target_configuration_version)
        except (TypeError, ValueError) as exc:
            raise ValidationError("TARGET_CONFIGURATION_VERSION_INVALID") from exc
        target = self.env["cc.campaign.configuration.version"].sudo().search(
            [("campaign_id", "=", self.id), ("version", "=", target_number)], limit=1
        )
        if not target:
            raise ValidationError("TARGET_CONFIGURATION_VERSION_UNKNOWN")
        current = self.configuration_version_id
        if target == current:
            raise CampaignControlConflict("ROLLBACK_TARGET_IS_CURRENT")
        if target.validation_errors_json:
            raise CampaignControlConflict("ROLLBACK_TARGET_BLOCKED")
        version = self.env["cc.campaign.configuration.version"]._create_internal(
            {
                "campaign_id": self.id,
                "template_version_id": target.template_version_id.id or False,
                "version": self._control_next_version_number(),
                "state": "published",
                "configuration_json": target.configuration_json,
                "validation_errors_json": [],
                "predecessor_id": current.id or False,
                "source_version_id": target.id,
            }
        )
        if current:
            current._control_write({"state": "rolled_back"})
        return version

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def _control_outbox_event(self, run_uuid, event_uuid, event_key, operation, version, context):
        self.ensure_one()
        environment = self._control_environment()
        organization = self.control_organization_public_id()
        payload = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": event_uuid,
            "event_type": EVENT_TYPE_BY_OPERATION[operation],
            "command_id": event_uuid,
            "operation": operation,
            "run_id": run_uuid,
            "application_id": "odoo.cc_campaign",
            "organization_public_id": organization,
            "business_unit_public_id": self.cc_business_unit_id.code,
            "campaign_public_id": self.code,
            "campaign_uuid": self.workspace_uuid,
            "environment": environment.lower(),
            "requested_state": context["requested_state"],
            "configuration_version": version.version,
            "manifest_ref": self._control_manifest_ref(version),
            "manifest_hash": self._control_manifest_hash(version),
            "configuration": version.configuration_json,
            "expected_configuration_version": context["expected_configuration_version"],
            "audit_reason": context["audit_reason"],
            "correlation_id": context["correlation_id"],
            "causation_id": context["causation_id"],
            "idempotency_key": context["idempotency_key"],
            "occurred_at": fields.Datetime.to_string(fields.Datetime.now()),
        }
        return self.env["codestra.runtime.integration.outbox"].sudo()._create_internal(
            {
                "event_uuid": event_uuid,
                "deterministic_event_key": event_key,
                "idempotency_key": event_key,
                "event_type": payload["event_type"],
                "schema_version": EVENT_SCHEMA_VERSION,
                "record_environment": environment,
                "aggregate_type": self._name,
                "aggregate_record_id": self.id,
                "aggregate_uuid": self.workspace_uuid,
                "integration_uuid": self.workspace_uuid,
                "organization_public_id": organization,
                "business_unit_code": self.cc_business_unit_id.code,
                "campaign_id": self.legacy_campaign_id.id,
                # Control commands are identified by event_uuid alone; the
                # design-request revision counter is never used for them.
                "design_request_revision": 0,
                "payload_json": payload,
                "payload_hash": _digest(payload),
                "correlation_id": context["correlation_id"],
                "causation_id": context["causation_id"],
                "delivery_state": "pending",
                "next_attempt_at": fields.Datetime.now(),
            }
        )

    def control_command(
        self,
        command,
        *,
        idempotency_key,
        audit_reason,
        expected_configuration_version,
        correlation_id,
        causation_id=None,
        target_configuration_version=None,
        external_effects_enabled=False,
    ):
        """Queue one allowlisted lifecycle command.

        Returns ``(run, replayed)``. A retry with the same Idempotency-Key
        returns the original run without re-evaluating state or version.
        """
        self.ensure_one()
        spec = COMMANDS.get(command)
        if spec is None:
            raise ValidationError("UNKNOWN_COMMAND")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValidationError("IDEMPOTENCY_KEY_REQUIRED")
        if not isinstance(audit_reason, str) or not audit_reason.strip():
            raise ValidationError("AUDIT_REASON_REQUIRED")
        if not correlation_id:
            raise ValidationError("CORRELATION_ID_REQUIRED")
        try:
            expected = int(expected_configuration_version)
        except (TypeError, ValueError) as exc:
            raise ValidationError("EXPECTED_CONFIGURATION_VERSION_INVALID") from exc
        idempotency_key = idempotency_key.strip()

        self._control_lock()
        Run = self.env["cc.campaign.provisioning.run"].sudo()
        existing = Run.search(
            [("campaign_id", "=", self.id), ("idempotency_key", "=", idempotency_key)],
            limit=1,
        )
        if existing:
            if existing.command != command:
                raise CampaignControlConflict("IDEMPOTENCY_KEY_REUSED")
            return existing, True

        if expected != self.configuration_version:
            raise CampaignControlConflict("CONFIGURATION_VERSION_CONFLICT")
        if self.desired_state not in spec["allowed"]:
            raise CampaignControlConflict("INVALID_STATE_TRANSITION")
        if not spec.get("concurrent") and Run.search_count(
            [
                ("campaign_id", "=", self.id),
                ("status", "=", "pending"),
                ("operation", "!=", "reconcile"),
            ]
        ):
            raise CampaignControlConflict("PROVISIONING_RUN_PENDING")
        if spec.get("gated") and not external_effects_enabled:
            raise CampaignControlConflict("EXTERNAL_EFFECTS_DISABLED")
        if spec.get("target") and self.control_status != "validated":
            raise CampaignControlConflict("CONFIGURATION_NOT_VALIDATED")
        if spec["operation"] and not self.control_organization_public_id():
            raise ValidationError("ORGANIZATION_IDENTITY_NOT_CONFIGURED")

        run_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{RUN_NAMESPACE}:{self.workspace_uuid}:{idempotency_key}"))
        event_key = f"{EVENT_NAMESPACE}:{self.workspace_uuid}:{idempotency_key}"
        event_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, event_key))
        run_values = {
            "campaign_id": self.id,
            "run_uuid": run_uuid,
            "command": command,
            "idempotency_key": idempotency_key,
            "correlation_id": correlation_id,
            "causation_id": causation_id or False,
            "audit_reason": audit_reason.strip(),
            "requested_by": self.env.user.id,
        }
        campaign_values = {}

        if command == "validate":
            version = self._control_snapshot()
            errors = version.validation_errors_json or []
            campaign_values = {
                "control_status": "blocked" if errors else "validated",
                "configuration_version_id": version.id,
                "control_blocked_reason_json": errors or False,
            }
            run = Run._create_internal(
                {
                    **run_values,
                    "requested_state": self.desired_state,
                    "configuration_version_id": version.id,
                    "configuration_version": version.version,
                    "status": "succeeded" if not errors else "failed",
                    "delivery_state": "none",
                    "completed_at": fields.Datetime.now(),
                    "error_code": "CONFIGURATION_BLOCKED" if errors else False,
                    "error_detail": ",".join(errors) if errors else False,
                }
            )
        else:
            if command == "rollback":
                version = self._control_rollback_version(target_configuration_version)
                campaign_values = {
                    "control_status": "validated",
                    "configuration_version_id": version.id,
                    "control_blocked_reason_json": False,
                }
            else:
                version = self.configuration_version_id
                if not version or version.validation_errors_json:
                    raise CampaignControlConflict("CONFIGURATION_NOT_VALIDATED")
            requested_state = spec["target"] or self.desired_state
            context = {
                "idempotency_key": idempotency_key,
                "audit_reason": audit_reason.strip(),
                "expected_configuration_version": expected,
                "correlation_id": correlation_id,
                "causation_id": causation_id or None,
                "requested_state": requested_state,
            }
            self._control_outbox_event(
                run_uuid, event_uuid, event_key, spec["operation"], version, context
            )
            run = Run._create_internal(
                {
                    **run_values,
                    "operation": spec["operation"],
                    "requested_state": requested_state,
                    "configuration_version_id": version.id,
                    "configuration_version": version.version,
                    "status": "pending",
                    "delivery_state": "queued",
                    "event_uuid": event_uuid,
                }
            )
            if spec["target"]:
                campaign_values["desired_state"] = spec["target"]
        # effective_state deliberately stays as the last provider read-back.
        if campaign_values:
            self._control_write(campaign_values)
        return run, False

    # ------------------------------------------------------------------
    # Read-back (campaign.actual_state.write)
    # ------------------------------------------------------------------
    @api.model
    def _control_readback_validate(self, body):
        if not isinstance(body, dict):
            raise ValidationError("JSON object required")
        missing = [name for name in READBACK_REQUIRED_FIELDS if name not in body]
        if missing:
            raise ValidationError("FIELD_REQUIRED:" + ",".join(missing))
        unknown = sorted(set(body) - set(READBACK_REQUIRED_FIELDS))
        if unknown:
            raise ValidationError("UNKNOWN_FIELDS:" + ",".join(unknown))
        document = {
            "event_uuid": _text(body["event_uuid"], "event_uuid", 128),
            "command_id": _text(body["command_id"], "command_id", 128),
            "idempotency_key": _text(body["idempotency_key"], "idempotency_key", 160),
            "organization_public_id": _text(body["organization_public_id"], "organization_public_id", 128),
            "business_unit_public_id": _text(body["business_unit_public_id"], "business_unit_public_id", 128),
            "campaign_public_id": _text(body["campaign_public_id"], "campaign_public_id", 128),
            "manifest_ref": _text(body["manifest_ref"], "manifest_ref", 256),
            "manifest_hash": _text(body["manifest_hash"], "manifest_hash", 80),
            "correlation_id": _text(body["correlation_id"], "correlation_id", 128),
            "causation_id": _text(body["causation_id"], "causation_id", 128),
        }
        for name, minimum in (("attempt", 1), ("configuration_version", 0)):
            value = body[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValidationError(f"FIELD_INVALID:{name}")
            document[name] = value
        if body["operation"] not in OPERATIONS:
            raise ValidationError("FIELD_INVALID:operation")
        if body["effective_state"] not in EFFECTIVE_STATE_KEYS:
            raise ValidationError("FIELD_INVALID:effective_state")
        if not isinstance(body["evidence"], dict):
            raise ValidationError("FIELD_INVALID:evidence")
        document.update(
            {
                "operation": body["operation"],
                "effective_state": body["effective_state"],
                "evidence": body["evidence"],
                "observed_at": _observed_at(body["observed_at"]),
            }
        )
        return document

    def control_readback(self, body):
        """Apply a provider-confirmed effective state posted by Middleware.

        Returns the receipt document. A replay (same key, same body) returns
        the original receipt; the same key with a different body is a 409.
        """
        self.ensure_one()
        document = self._control_readback_validate(body)
        request_hash = _digest({name: body[name] for name in READBACK_REQUIRED_FIELDS})
        self._control_lock()
        Readback = self.env["cc.campaign.readback"].sudo()
        prior = Readback.search(
            [("campaign_id", "=", self.id), ("idempotency_key", "=", document["idempotency_key"])],
            limit=1,
        )
        if prior:
            if prior.request_hash != request_hash:
                raise CampaignControlConflict("IDEMPOTENCY_CONFLICT")
            return prior.receipt()

        if document["configuration_version"] != self.configuration_version:
            raise CampaignControlConflict("STALE_CONFIGURATION_VERSION")
        if document["manifest_hash"] != self._control_manifest_hash():
            raise CampaignControlConflict("STALE_CONFIGURATION_VERSION")

        Run = self.env["cc.campaign.provisioning.run"].sudo()
        run = Run.search(
            [("campaign_id", "=", self.id), ("event_uuid", "=", document["command_id"])],
            limit=1,
        )
        if not run:
            raise ValidationError("COMMAND_ID_UNKNOWN")
        if run.event_uuid != document["event_uuid"] or run.operation != document["operation"]:
            raise ValidationError("COMMAND_BINDING_MISMATCH")

        observed = document["effective_state"]
        now = fields.Datetime.now()
        campaign_values = {}
        run_values = {}
        drift_reason = [
            "PROVIDER_STATE_DRIFT",
            f"DESIRED:{self.desired_state}",
            f"OBSERVED:{observed}",
        ]
        if run.status != "pending":
            # A fresh observation for a finished command: informational unless
            # it diverges from what Odoo already believes.
            if observed in {self.effective_state, self.desired_state}:
                outcome = "confirmed"
                if observed != self.effective_state:
                    campaign_values = {"effective_state": observed}
            else:
                outcome = "drift"
                campaign_values = {
                    "effective_state": observed,
                    "control_status": "blocked",
                    "control_blocked_reason_json": drift_reason,
                }
        elif observed == run.requested_state:
            outcome = "completed"
            run_values = {"status": "succeeded", "completed_at": now}
            if self.effective_state != observed:
                campaign_values["effective_state"] = observed
        elif run.operation == "reconcile":
            # Reconcile reports whatever the provider holds; a divergence from
            # the desired state is real drift, not execution lag.
            outcome = "drift"
            run_values = {"status": "succeeded", "completed_at": now}
            campaign_values = {
                "effective_state": observed,
                "control_status": "blocked",
                "control_blocked_reason_json": drift_reason,
            }
        elif observed == self.effective_state:
            # Normal execution lag: providers have not moved yet.
            outcome = "in_progress"
        else:
            outcome = "blocked"
            run_values = {
                "status": "failed",
                "completed_at": now,
                "error_code": "UNEXPECTED_PROVIDER_STATE",
                "error_detail": f"requested {run.requested_state}, observed {observed}",
            }
            campaign_values = {
                "effective_state": observed,
                "control_status": "blocked",
                "control_blocked_reason_json": drift_reason,
            }

        receipt = Readback.with_context(_cc_control_capability=CONTROL_CAPABILITY).create(
            {
                "campaign_id": self.id,
                "run_id": run.id,
                "readback_uuid": str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{READBACK_NAMESPACE}:{self.workspace_uuid}:{document['idempotency_key']}",
                    )
                ),
                "idempotency_key": document["idempotency_key"],
                "request_hash": request_hash,
                "attempt": document["attempt"],
                "event_uuid": document["event_uuid"],
                "command_id": document["command_id"],
                "operation": document["operation"],
                "effective_state": observed,
                "configuration_version": document["configuration_version"],
                "manifest_ref": document["manifest_ref"],
                "manifest_hash": document["manifest_hash"],
                "evidence_json": document["evidence"],
                "observed_at": document["observed_at"],
                "correlation_id": document["correlation_id"],
                "causation_id": document["causation_id"],
                "outcome": outcome,
            }
        )
        if run_values:
            run._control_write({**run_values, "readback_id": receipt.id})
        if campaign_values:
            self._control_write(campaign_values)
        return receipt.receipt()

    # ------------------------------------------------------------------
    # Outbox delivery hooks (never business outcomes)
    # ------------------------------------------------------------------
    def _control_delivery_acknowledged(self, event_uuid):
        """Middleware acknowledged durable intake; nothing else changes."""
        self.ensure_one()
        run = self.env["cc.campaign.provisioning.run"].sudo().search(
            [
                ("campaign_id", "=", self.id),
                ("event_uuid", "=", event_uuid),
                ("delivery_state", "=", "queued"),
            ],
            limit=1,
        )
        if run:
            run._control_write(
                {"delivery_state": "accepted", "delivery_acknowledged_at": fields.Datetime.now()}
            )
        return bool(run)

    def _control_delivery_dead_lettered(self, event_uuid):
        """Outbox delivery exhausted retries before intake; the command never ran."""
        self.ensure_one()
        run = self.env["cc.campaign.provisioning.run"].sudo().search(
            [("campaign_id", "=", self.id), ("event_uuid", "=", event_uuid), ("status", "=", "pending")],
            limit=1,
        )
        if not run:
            return False
        run._control_write(
            {
                "status": "failed",
                "delivery_state": "dead_letter",
                "completed_at": fields.Datetime.now(),
                "error_code": "DELIVERY_DEAD_LETTER",
                "error_detail": "Outbox delivery exhausted retries before intake.",
            }
        )
        self._control_write(
            {
                "control_status": "blocked",
                "control_blocked_reason_json": ["DELIVERY_DEAD_LETTER", f"COMMAND:{event_uuid}"],
            }
        )
        return True

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    @api.model
    def control_create_values(self, document, *, idempotency_key):
        """Validate an explicit, allowlisted creation request.

        Returns ``(business_unit, values)`` so the caller can assert tenant
        scope before anything is written.
        """
        if not isinstance(document, dict):
            raise ValidationError("JSON object required")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValidationError("IDEMPOTENCY_KEY_REQUIRED")
        body = {key: value for key, value in document.items() if key not in CREATE_ENVELOPE_FIELDS}
        unknown = sorted(set(body) - set(CREATE_FIELDS))
        if unknown:
            raise ValidationError("UNKNOWN_FIELDS:" + ",".join(unknown))
        for key, expected_type in CREATE_FIELDS.items():
            if key in body and (
                not isinstance(body[key], expected_type)
                or (expected_type is int and isinstance(body[key], bool))
            ):
                raise ValidationError(f"FIELD_TYPE_INVALID:{key}")
        for key in ("name", "code"):
            if not body.get(key, "").strip():
                raise ValidationError(f"FIELD_REQUIRED:{key}")

        Unit = self.env["cc.business.unit"].sudo().with_context(active_test=False)
        unit = Unit.browse()
        if body.get("business_unit_code"):
            unit = Unit.search([("code", "=", body["business_unit_code"].strip().upper())], limit=1)
        elif body.get("business_unit_uuid"):
            unit = Unit.search(
                [
                    "|",
                    ("scope_uuid", "=", body["business_unit_uuid"]),
                    ("integration_uuid", "=", body["business_unit_uuid"]),
                ],
                limit=1,
            )
        else:
            raise ValidationError("FIELD_REQUIRED:business_unit_code")
        if not unit:
            raise ValidationError("BUSINESS_UNIT_UNKNOWN")

        environment = body.get("environment", "staging").strip().lower()
        environment = {"test": "development"}.get(environment, environment)
        if environment not in ENVIRONMENT_CODES:
            raise ValidationError("FIELD_VALUE_INVALID:environment")

        values = {key: body[key] for key in CREATE_PASSTHROUGH if key in body}
        values["code"] = values["code"].strip().upper()
        values["cc_business_unit_id"] = unit.id
        values["environment"] = environment
        # Middleware-created campaigns are configured through this control
        # lifecycle, not the UI design-preview automation.
        values["design_automation_enabled"] = False
        values["workspace_uuid"] = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{CREATE_NAMESPACE}:{unit.code}:{idempotency_key.strip()}")
        )
        if body.get("application_code"):
            application = self.env["cc.application"].sudo().search(
                [("code", "=", body["application_code"])], limit=1
            )
            if not application:
                raise ValidationError("APPLICATION_UNKNOWN")
            values["application_id"] = application.id
        if body.get("template_code"):
            template = self.env["cc.campaign.template"].sudo().search(
                [("code", "=", body["template_code"])], limit=1
            )
            if not template:
                raise ValidationError("TEMPLATE_UNKNOWN")
            values["template_id"] = template.id
            values["template_version_id"] = template.current_version_id.id or False
            values.setdefault("application_id", template.application_id.id)
        return unit, values

    @api.model
    def control_create(self, values):
        """Return ``(campaign, created)`` honouring the idempotent identity."""
        Campaign = self.sudo().with_context(active_test=False)
        prior = Campaign.search([("workspace_uuid", "=", values["workspace_uuid"])], limit=1)
        if prior:
            return prior, False
        return Campaign.create(values), True
