import hashlib
import json
import os
import re
import ssl
import uuid
from datetime import timedelta
from urllib import error, parse, request

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

EVENT_TYPE = "campaign.design.requested.v1"
SCHEMA_VERSION = "1.0"
MAX_PAYLOAD_BYTES = 65536
MAX_RESPONSE_BYTES = 65536
OUTBOX_PRODUCER_CAPABILITY = object()
OUTBOX_WORKER_CAPABILITY = object()
ALLOWED_TRANSITIONS = {
    "pending": {"processing", "superseded"},
    "processing": {"processing", "delivered", "failed"},
    "failed": {"processing", "dead_letter", "superseded"},
    "delivered": set(),
    "dead_letter": {"superseded"},
    "superseded": set(),
}


class CampaignDeliveryConfigurationError(ValidationError):
    """No delivery was attempted; retry after deployment configuration is fixed."""


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class CodestraIntegrationOutbox(models.Model):
    _name = "codestra.runtime.integration.outbox"
    _description = "Immutable Codestra Integration Outbox"
    _order = "created_at, id"

    event_uuid = fields.Char(required=True, index=True, copy=False, readonly=True)
    deterministic_event_key = fields.Char(
        required=True, index=True, copy=False, readonly=True
    )
    idempotency_key = fields.Char(required=True, index=True, copy=False, readonly=True)
    event_type = fields.Char(required=True, index=True, readonly=True)
    schema_version = fields.Char(required=True, readonly=True)
    record_environment = fields.Selection(
        [("TEST", "Test"), ("STAGING", "Staging"), ("PRODUCTION", "Production")],
        required=True,
        default="STAGING",
        index=True,
        readonly=True,
    )
    aggregate_type = fields.Char(required=True, index=True, readonly=True)
    aggregate_record_id = fields.Integer(index=True, readonly=True)
    aggregate_uuid = fields.Char(required=True, index=True, readonly=True)
    integration_uuid = fields.Char(required=True, index=True, readonly=True)
    organization_public_id = fields.Char(index=True, readonly=True)
    business_unit_code = fields.Char(required=True, index=True, readonly=True)
    campaign_id = fields.Many2one(
        "call.center.campaign",
        required=True,
        ondelete="restrict",
        index=True,
        readonly=True,
    )
    design_request_revision = fields.Integer(required=True, readonly=True)
    payload_json = fields.Json(required=True, readonly=True)
    payload_hash = fields.Char(required=True, size=64, readonly=True)
    correlation_id = fields.Char(required=True, index=True, readonly=True)
    causation_id = fields.Char(index=True, readonly=True)
    delivery_state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("delivered", "Delivered"),
            ("failed", "Failed"),
            ("dead_letter", "Dead Letter"),
            ("superseded", "Superseded"),
        ],
        required=True,
        default="pending",
        index=True,
        readonly=True,
    )
    retry_count = fields.Integer(default=0, required=True, readonly=True)
    next_attempt_at = fields.Datetime(index=True, readonly=True)
    processing_started_at = fields.Datetime(index=True, readonly=True)
    lease_consumer_id = fields.Char(index=True, readonly=True)
    lease_token_hash = fields.Char(size=64, readonly=True)
    lease_generation = fields.Integer(default=0, required=True, readonly=True)
    lease_expires_at = fields.Datetime(index=True, readonly=True)
    lease_heartbeat_at = fields.Datetime(readonly=True)
    created_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True, readonly=True
    )
    delivered_at = fields.Datetime(readonly=True)
    acknowledged_at = fields.Datetime(readonly=True)
    completed_at = fields.Datetime(readonly=True)
    last_error_code = fields.Char(readonly=True)
    last_error_class = fields.Char(readonly=True)
    last_error_safe_message = fields.Char(readonly=True)
    last_error_fingerprint = fields.Char(size=64, readonly=True)
    policy_hash = fields.Char(size=64, readonly=True)
    integration_status = fields.Selection(
        [
            ("REQUESTED", "Requested"),
            ("PROCESSING", "Processing"),
            ("COMPLETED", "Completed"),
            ("FAILED", "Failed"),
        ],
        default="REQUESTED",
        required=True,
        readonly=True,
        index=True,
    )
    result_inbox_ids = fields.One2many(
        "codestra.integration.result.inbox",
        "originating_outbox_id",
        readonly=True,
    )

    _event_uuid_unique = models.Constraint(
        "unique(event_uuid)", "Outbox event UUIDs must be unique."
    )
    _event_key_unique = models.Constraint(
        "unique(deterministic_event_key)",
        "Outbox deterministic event keys must be unique.",
    )
    _environment_idempotency_unique = models.Constraint(
        "unique(record_environment, idempotency_key)",
        "Outbox idempotency keys must be unique within an environment.",
    )
    # Only design requests are once-per-revision. Other producers (campaign
    # control commands) never populate design_request_revision and identify
    # themselves by a globally unique event_uuid instead.
    _design_revision_once = models.UniqueIndex(
        "(event_type, integration_uuid, design_request_revision)"
        f" WHERE event_type = '{EVENT_TYPE}'",
        "A campaign design revision may only be requested once.",
    )
    _payload_hash_format = models.Constraint(
        "check(length(payload_hash) = 64)", "Payload hashes must be SHA-256 values."
    )
    _retry_nonnegative = models.Constraint(
        "check(retry_count >= 0)", "Retry counts cannot be negative."
    )
    _lease_generation_nonnegative = models.Constraint(
        "check(lease_generation >= 0)", "Lease generations cannot be negative."
    )

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("_codestra_outbox_capability")
            is not OUTBOX_PRODUCER_CAPABILITY
        ):
            raise AccessError(
                "Outbox records may only be created by an internal producer."
            )
        for vals in vals_list:
            encoded = canonical_json(vals.get("payload_json", {})).encode()
            if len(encoded) > MAX_PAYLOAD_BYTES:
                raise ValidationError(
                    "Outbox payload exceeds the configured size limit."
                )
            expected = hashlib.sha256(encoded).hexdigest()
            if vals.get("payload_hash") != expected:
                raise ValidationError("Outbox payload hash does not match its payload.")
            environment = str(
                vals.get("payload_json", {}).get("environment", "staging")
            ).upper()
            if environment not in {"TEST", "STAGING", "PRODUCTION"}:
                raise ValidationError("Outbox record environment is invalid.")
            vals.setdefault("record_environment", environment)
            vals.setdefault("idempotency_key", vals.get("deterministic_event_key"))
        return super().create(vals_list)

    def write(self, vals):
        immutable = {
            "event_uuid",
            "deterministic_event_key",
            "idempotency_key",
            "event_type",
            "schema_version",
            "record_environment",
            "aggregate_type",
            "aggregate_record_id",
            "aggregate_uuid",
            "integration_uuid",
            "organization_public_id",
            "business_unit_code",
            "campaign_id",
            "design_request_revision",
            "payload_json",
            "payload_hash",
            "correlation_id",
            "causation_id",
            "created_at",
            "policy_hash",
        }
        if immutable & vals.keys():
            raise AccessError("Accepted outbox payloads are immutable.")
        if (
            self.env.context.get("_codestra_outbox_capability")
            is not OUTBOX_WORKER_CAPABILITY
        ):
            raise AccessError("Outbox state is controlled by the delivery worker.")
        if "delivery_state" in vals:
            for record in self:
                if (
                    vals["delivery_state"]
                    not in ALLOWED_TRANSITIONS[record.delivery_state]
                ):
                    raise ValidationError(
                        "Invalid outbox transition "
                        f"{record.delivery_state} -> {vals['delivery_state']}."
                    )
        return super().write(vals)

    def unlink(self):
        raise AccessError("Outbox history cannot be deleted.")

    @api.model
    def _create_internal(self, vals):
        return self.with_context(
            _codestra_outbox_capability=OUTBOX_PRODUCER_CAPABILITY
        ).create(vals)

    def _worker_write(self, vals):
        return self.with_context(
            _codestra_outbox_capability=OUTBOX_WORKER_CAPABILITY
        ).write(vals)

    @api.model
    def create_event(
        self,
        *,
        event_type,
        aggregate,
        payload,
        correlation_id,
        idempotency_key,
        schema_version=SCHEMA_VERSION,
        causation_id=None,
        policy_hash=None,
        aggregate_version=None,
        environment=None,
        organization_public_id=None,
        campaign=None,
    ):
        """Create an immutable event inside the caller's Odoo transaction.

        This method intentionally does not commit, open another cursor, or
        perform network I/O. The business mutation and outbox record therefore
        commit or roll back together.
        """
        aggregate.ensure_one()
        if not event_type or not idempotency_key or not correlation_id:
            raise ValidationError(
                "Event type, idempotency key and correlation ID are required."
            )
        campaign = campaign or (
            aggregate
            if aggregate._name == "call.center.campaign"
            else getattr(aggregate, "call_center_campaign_id", False)
            or getattr(aggregate, "campaign_id", False)
        )
        if not campaign or len(campaign) != 1:
            raise ValidationError(
                "A generic integration event requires one authoritative campaign."
            )
        unit = getattr(aggregate, "business_unit_id", False) or campaign.business_unit_id
        business_unit_code = (unit.code or "").upper()
        aggregate_public_id = (
            getattr(aggregate, "integration_uuid", False)
            or getattr(aggregate, "external_source_id", False)
            or getattr(aggregate, "codestra_employee_number", False)
            or getattr(aggregate, "code", False)
        )
        if not aggregate_public_id or not business_unit_code:
            raise ValidationError(
                "The aggregate requires canonical public and business-unit identities."
            )
        aggregate_version = aggregate_version or getattr(
            aggregate, "desired_state_version", False
        )
        if not isinstance(aggregate_version, int) or aggregate_version < 1:
            raise ValidationError("Aggregate version must be a positive integer.")
        environment = (
            environment
            or self.env["ir.config_parameter"]
            .sudo()
            .get_param("codestra.integration.environment")
        )
        environment = str(environment or "").upper()
        if environment not in {"TEST", "STAGING", "PRODUCTION"}:
            raise ValidationError("Integration environment is not configured.")
        organization_public_id = (
            organization_public_id
            or self.env["ir.config_parameter"]
            .sudo()
            .get_param("codestra.integration.organization_public_id")
        )
        if not organization_public_id:
            raise ValidationError("Organization public identity is not configured.")
        clean_payload = dict(payload)
        clean_payload.setdefault("environment", environment.lower())
        encoded = canonical_json(clean_payload).encode()
        payload_hash = hashlib.sha256(encoded).hexdigest()
        event_key = (
            f"{environment}:{aggregate._name}:{aggregate_public_id}:"
            f"{aggregate_version}:{event_type}:{idempotency_key}"
        )
        existing = self.sudo().search(
            [("deterministic_event_key", "=", event_key)], limit=1
        )
        if existing:
            if (
                existing.payload_hash != payload_hash
                or existing.aggregate_type != aggregate._name
                or existing.aggregate_uuid != aggregate_public_id
                or existing.design_request_revision != aggregate_version
                or existing.event_type != event_type
                or existing.record_environment != environment
            ):
                raise ValidationError("IMMUTABLE_EVENT_BINDING_CONFLICT")
            return existing
        event_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, event_key))
        return self.sudo()._create_internal(
            {
                "event_uuid": event_uuid,
                "deterministic_event_key": event_key,
                "idempotency_key": idempotency_key,
                "event_type": event_type,
                "schema_version": schema_version,
                "record_environment": environment,
                "aggregate_type": aggregate._name,
                "aggregate_record_id": aggregate.id,
                "aggregate_uuid": aggregate_public_id,
                "integration_uuid": aggregate_public_id,
                "organization_public_id": organization_public_id,
                "business_unit_code": business_unit_code,
                "campaign_id": campaign.id,
                "design_request_revision": aggregate_version,
                "payload_json": clean_payload,
                "payload_hash": payload_hash,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "policy_hash": policy_hash,
                "delivery_state": "pending",
                "next_attempt_at": fields.Datetime.now(),
            }
        )

    @api.model
    def _claim_batch(
        self,
        limit=20,
        consumer_id=None,
        lease_ttl_seconds=30,
        record_environment=None,
        business_unit_codes=None,
        event_type_allowlist=None,
    ):
        """Claim due work with a short PostgreSQL transaction."""
        if limit < 1 or limit > 200:
            raise ValidationError("Outbox claim batch size must be between 1 and 200.")
        if lease_ttl_seconds < 10 or lease_ttl_seconds > 300:
            raise ValidationError(
                "Outbox lease TTL must be between 10 and 300 seconds."
            )
        # ORM writes are deferred: a delivered event may still look pending to
        # raw SQL until these exact predicate/order fields have been flushed.
        self.flush_model([
            "delivery_state", "next_attempt_at", "lease_expires_at",
            "record_environment", "business_unit_code", "event_type", "created_at",
        ])
        business_unit_codes = list(business_unit_codes or ())
        event_type_allowlist = list(event_type_allowlist or ())
        self.env.cr.execute(
            """
            SELECT id
              FROM codestra_runtime_integration_outbox
             WHERE ((
                    delivery_state IN ('pending', 'failed')
                    AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                   )
                OR (
                    delivery_state = 'processing'
                    AND lease_expires_at <= now()
                   ))
               AND (%s = FALSE OR record_environment = %s)
               AND (%s = FALSE OR business_unit_code = ANY(%s))
               AND (%s = FALSE OR event_type = ANY(%s))
             ORDER BY created_at, id
             FOR UPDATE SKIP LOCKED
             LIMIT %s
            """,
            (
                bool(record_environment),
                record_environment or "",
                bool(business_unit_codes),
                business_unit_codes or [""],
                bool(event_type_allowlist),
                event_type_allowlist or [""],
                limit,
            ),
        )
        records = self.browse([row[0] for row in self.env.cr.fetchall()])
        issued_tokens = {}
        if records:
            now = fields.Datetime.now()
            for record in records:
                token = uuid.uuid4().hex
                issued_tokens[record.id] = token
                record._worker_write(
                    {
                        "delivery_state": "processing",
                        "processing_started_at": now,
                        "lease_consumer_id": consumer_id or "legacy-worker",
                        "lease_token_hash": hashlib.sha256(token.encode()).hexdigest(),
                        "lease_generation": record.lease_generation + 1,
                        "lease_expires_at": now + timedelta(seconds=lease_ttl_seconds),
                        "lease_heartbeat_at": now,
                    }
                )
            records.flush_recordset(
                [
                    "delivery_state",
                    "processing_started_at",
                    "lease_consumer_id",
                    "lease_token_hash",
                    "lease_generation",
                    "lease_expires_at",
                    "lease_heartbeat_at",
                ]
            )
        return records.with_context(_codestra_lease_tokens=issued_tokens)

    def _issued_lease_token(self):
        self.ensure_one()
        return self.env.context.get("_codestra_lease_tokens", {}).get(self.id)

    def _verify_lease(self, consumer_id, lease_token, lease_generation):
        self.ensure_one()
        token_hash = hashlib.sha256((lease_token or "").encode()).hexdigest()
        if (
            self.delivery_state != "processing"
            or not lease_token
            or self.lease_consumer_id != consumer_id
            or self.lease_token_hash != token_hash
            or self.lease_generation != lease_generation
            or not self.lease_expires_at
            or self.lease_expires_at <= fields.Datetime.now()
        ):
            raise ValidationError("LEASE_GENERATION_MISMATCH")
        return True

    def _renew_lease(
        self, consumer_id, lease_token, lease_generation, extension_seconds=30
    ):
        self._verify_lease(consumer_id, lease_token, lease_generation)
        if extension_seconds < 10 or extension_seconds > 300:
            raise ValidationError("Lease extension must be between 10 and 300 seconds.")
        now = fields.Datetime.now()
        self._worker_write(
            {
                "lease_expires_at": now + timedelta(seconds=extension_seconds),
                "lease_heartbeat_at": now,
            }
        )
        return self

    def _release_lease(self, consumer_id, lease_token, lease_generation):
        self._verify_lease(consumer_id, lease_token, lease_generation)
        self._worker_write(
            {
                "delivery_state": "failed",
                "processing_started_at": False,
                "lease_consumer_id": False,
                "lease_token_hash": False,
                "lease_expires_at": False,
                "lease_heartbeat_at": False,
                "next_attempt_at": fields.Datetime.now(),
            }
        )
        return self

    @api.model
    def _middleware_configuration(self):
        endpoint = os.environ.get("CODESTRA_MIDDLEWARE_CAMPAIGN_DESIGN_URL", "")
        token_file = os.environ.get("CODESTRA_MIDDLEWARE_TOKEN_FILE", "")
        try:
            parsed = parse.urlparse(endpoint)
            port = parsed.port
        except ValueError:
            raise CampaignDeliveryConfigurationError("The middleware endpoint is invalid.") from None
        if (
            any(ord(char) <= 32 or ord(char) > 126 for char in endpoint)
            or port == 0
            or "?" in endpoint or "#" in endpoint
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/api/v1/campaign-designs/preview"
        ):
            raise CampaignDeliveryConfigurationError(
                "The exact HTTPS middleware design endpoint is required."
            )
        if not token_file or not os.path.isabs(token_file):
            raise CampaignDeliveryConfigurationError(
                "The middleware credential must use an absolute secret file."
            )
        try:
            with open(token_file, encoding="utf-8") as handle:
                token = handle.read(16385).rstrip("\r\n")
        except (OSError, UnicodeError):
            raise CampaignDeliveryConfigurationError("The middleware credential file is unavailable.") from None
        if not token or len(token) > 16384 or any(not 33 <= ord(char) <= 126 for char in token):
            raise CampaignDeliveryConfigurationError("The middleware credential file is invalid.")
        return endpoint, token

    def _campaign_tenant_id(self):
        self.ensure_one()
        try:
            mapping = json.loads(os.environ.get("CODESTRA_MIDDLEWARE_CAMPAIGN_TENANTS", "{}"))
        except (TypeError, ValueError) as exc:
            raise CampaignDeliveryConfigurationError("The campaign tenant mapping is invalid.") from None
        tenant = mapping.get(self.business_unit_code) if isinstance(mapping, dict) else None
        if (not isinstance(tenant, str) or not tenant or len(tenant) > 128
                or any(not 33 <= ord(char) <= 126 for char in tenant) or "*" in tenant):
            raise CampaignDeliveryConfigurationError("An explicit campaign business-unit tenant binding is required.")
        return tenant

    def _defer_configuration_failure(self):
        self.ensure_one()
        self._worker_write({
            "delivery_state": "failed",
            "next_attempt_at": fields.Datetime.now() + timedelta(seconds=60),
            "processing_started_at": False,
            "lease_consumer_id": False,
            "lease_token_hash": False,
            "lease_expires_at": False,
            "lease_heartbeat_at": False,
            "last_error_code": "CONFIGURATION_BLOCKED",
            "last_error_class": "CampaignDeliveryConfigurationError",
            "last_error_safe_message": "Campaign delivery configuration requires correction.",
            "last_error_fingerprint": False,
        })

    @api.model
    def _supersede_legacy_design_requests(self):
        """Upgrade queued previews without rewriting accepted payloads or keys.

        Run in the module-upgrade transaction with delivery workers stopped.
        A processing event may already have reached Middleware: refuse the
        upgrade until its original operation is reconciled. Never resend a
        modified payload with that event's immutable idempotency identity.
        """
        self.flush_model()
        legacy = self.search([
            ("event_type", "=", EVENT_TYPE),
            ("delivery_state", "in", ["pending", "failed", "dead_letter", "processing"]),
        ]).filtered(lambda event: "design_request_revision" not in event.payload_json)
        campaigns = legacy.mapped("campaign_id").sorted("id")
        campaigns._lock_automatic_design_rows()
        for campaign in campaigns:
            events = self.search([
                ("campaign_id", "=", campaign.id), ("event_type", "=", EVENT_TYPE),
            ])
            self.env.cr.execute(
                "SELECT id FROM codestra_runtime_integration_outbox WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                [events.ids],
            )
            events.invalidate_recordset()
            queued = events.filtered(lambda event: (
                event.delivery_state in {"pending", "failed", "dead_letter", "processing"}
                and "design_request_revision" not in event.payload_json
            ))
            if queued.filtered(lambda event: event.delivery_state == "processing"):
                raise ValidationError(
                    "Reconcile processing legacy campaign previews and stop delivery workers before upgrading."
                )
            if not queued:
                continue
            queued._worker_write({
                "delivery_state": "superseded",
                "next_attempt_at": False,
                "processing_started_at": False,
                "lease_consumer_id": False,
                "lease_token_hash": False,
                "lease_expires_at": False,
                "lease_heartbeat_at": False,
                "completed_at": fields.Datetime.now(),
                "last_error_code": "LEGACY_PREVIEW_SUPERSEDED",
                "last_error_class": False,
                "last_error_safe_message": "Legacy preview superseded by a revision-bound request.",
                "last_error_fingerprint": False,
            })
            revisions = self.env["call.center.campaign.design.revision"].sudo().search([
                ("event_uuid", "in", queued.mapped("event_uuid")),
                ("state", "in", ["requested", "hash_only", "ready", "approved"]),
            ])
            revisions._system_write({"state": "superseded"})
            if campaign.design_request_revision in queued.mapped("design_request_revision"):
                # Produce only the latest desired design. Older undelivered
                # revisions must not overwrite an already newer valid request.
                campaign._create_design_request_event(
                    revision=max([campaign.design_request_revision] + events.mapped("design_request_revision")) + 1
                )
        return len(legacy)

    def _send_to_middleware(self):
        self.ensure_one()
        endpoint, token = self._middleware_configuration()
        body = canonical_json(self.payload_json).encode()
        outbound = request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Idempotency-Key": self.event_uuid,
                "X-Correlation-ID": self.correlation_id,
                "X-Business-Unit": self.business_unit_code,
                "X-Tenant-ID": self._campaign_tenant_id(),
            },
        )
        context = ssl.create_default_context()
        # _middleware_configuration bounds this request to credential-free HTTPS.
        with request.urlopen(  # nosec B310
            outbound, timeout=10, context=context
        ) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValidationError("Middleware response exceeds the size limit.")
            result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValidationError("Middleware response must be a JSON object.")
        manifest_hash = result.get("manifest_hash")
        design_revision = result.get("design_revision")
        if (
            not isinstance(manifest_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest_hash) is None
            or isinstance(design_revision, bool)
            or not isinstance(design_revision, int)
            or design_revision <= 0
        ):
            raise ValidationError(
                "Middleware did not confirm a committed design revision."
            )
        return result

    def _lock_campaign_for_finalization(self):
        self.ensure_one()
        campaign = self.campaign_id
        self.env.cr.execute(
            "SELECT id FROM call_center_campaign WHERE id = %s FOR UPDATE",
            [campaign.id],
        )
        campaign.invalidate_recordset(
            ["design_request_revision", "design_request_state"]
        )
        return campaign

    def _finalize_delivery_success(self, result):
        self.ensure_one()
        campaign = self._lock_campaign_for_finalization()
        if campaign.design_request_revision == self.design_request_revision:
            campaign._write_integration_state(
                {
                    "design_request_state": "delivered",
                    "middleware_design_revision": result["design_revision"],
                    "last_design_delivery_at": fields.Datetime.now(),
                }
            )
        self._worker_write(
            {
                "delivery_state": "delivered",
                "delivered_at": fields.Datetime.now(),
                "processing_started_at": False,
                "last_error_code": False,
                "last_error_class": False,
                "last_error_safe_message": False,
                "last_error_fingerprint": False,
                "completed_at": fields.Datetime.now(),
            }
        )

    def _finalize_delivery_failure(self, exc):
        self.ensure_one()
        retry_count = self.retry_count + 1
        terminal = retry_count >= 5
        fingerprint = hashlib.sha256(type(exc).__name__.encode()).hexdigest()
        self._worker_write(
            {
                "delivery_state": "failed",
                "retry_count": retry_count,
                "next_attempt_at": fields.Datetime.now()
                + timedelta(seconds=min(300, 2**retry_count)),
                "processing_started_at": False,
                "last_error_code": "DELIVERY_FAILED",
                "last_error_class": type(exc).__name__[:128],
                "last_error_safe_message": "Integration delivery failed.",
                "last_error_fingerprint": fingerprint,
            }
        )
        if terminal:
            self._worker_write({"delivery_state": "dead_letter"})
            if self.event_type == EVENT_TYPE:
                campaign = self._lock_campaign_for_finalization()
                if campaign.design_request_revision == self.design_request_revision:
                    campaign._write_integration_state(
                        {"design_request_state": "dead_letter"}
                    )
            self._on_dead_letter()
        return terminal

    def _on_acknowledged(self):
        """Hook: Middleware acknowledged delivery (never a business outcome)."""
        self.ensure_one()

    def _on_dead_letter(self):
        """Hook for producers that track delivery of their own event types."""
        self.ensure_one()

    @api.model
    def _cron_deliver_campaign_design_events(self):
        claimed = self._claim_batch()
        claimed_ids = claimed.ids
        self.env.cr.commit()
        for event_id in claimed_ids:
            event = self.browse(event_id).exists()
            if not event or event.delivery_state != "processing":
                continue
            try:
                result = event._send_to_middleware()
                event._finalize_delivery_success(result)
                self.env.cr.commit()
            except CampaignDeliveryConfigurationError:
                self.env.cr.rollback()
                event = self.browse(event_id).exists()
                if event:
                    event._defer_configuration_failure()
                    self.env.cr.commit()
            except (
                error.URLError,
                OSError,
                TimeoutError,
                ValidationError,
                ValueError,
            ) as exc:
                self.env.cr.rollback()
                event = self.browse(event_id).exists()
                if not event:
                    continue
                event._finalize_delivery_failure(exc)
                self.env.cr.commit()


class CallCenterCampaignOutboxProducer(models.Model):
    _inherit = "call.center.campaign"

    design_automation_enabled = fields.Boolean(
        default=False,
        help="Generate immutable design-request events. Enabling does not authorize provisioning.",
    )
    purpose_code = fields.Char(copy=False)
    design_request_revision = fields.Integer(default=0, readonly=True, copy=False)
    design_request_state = fields.Selection(
        [
            ("not_requested", "Not Requested"),
            ("pending", "Pending"),
            ("delivered", "Delivered"),
            ("dead_letter", "Dead Letter"),
        ],
        default="not_requested",
        required=True,
        readonly=True,
        copy=False,
    )
    last_design_event_uuid = fields.Char(readonly=True, copy=False)
    last_design_delivery_at = fields.Datetime(readonly=True, copy=False)
    middleware_design_revision = fields.Integer(readonly=True, copy=False)
    provisioning_state = fields.Selection(
        [
            ("blocked", "Blocked"),
            ("provisioned_disabled", "Provisioned Disabled"),
            ("activation_pending", "Activation Pending"),
            ("active", "Active"),
        ],
        default="blocked",
        required=True,
        readonly=True,
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        protected = {
            "integration_uuid",
            "design_request_revision",
            "design_request_state",
            "last_design_event_uuid",
            "last_design_delivery_at",
            "middleware_design_revision",
            "provisioning_state",
        }
        # A caller may never set integration state. The cc.campaign _inherits
        # delegation injects the parent's own defaults when it creates this
        # record, so only a value that differs from the field default is a
        # claim.
        defaults = self.default_get(sorted(protected))
        for vals in vals_list:
            claimed = {
                name
                for name in protected & vals.keys()
                if vals[name] != defaults.get(name)
            }
            if claimed:
                raise AccessError("Campaign integration state is system controlled.")
            if vals.get("design_automation_enabled"):
                vals["integration_uuid"] = str(uuid.uuid4())
        campaigns = super().create(vals_list)
        for campaign in campaigns.filtered("design_automation_enabled"):
            campaign._create_design_request_event()
        return campaigns

    def write(self, vals):
        protected = {
            "integration_uuid",
            "design_request_revision",
            "design_request_state",
            "last_design_event_uuid",
            "last_design_delivery_at",
            "middleware_design_revision",
            "provisioning_state",
        }
        if protected & vals.keys():
            raise AccessError("Campaign integration state is system controlled.")
        design_fields = {
            "business_unit_id",
            "purpose_code",
            "direction",
            "timezone",
            "calling_hour_start",
            "calling_hour_end",
            "consent_required",
            "dnc_enforced",
            "team_ids",
            "supervisor_ids",
        }
        must_lock = bool(design_fields & vals.keys()) or (
            vals.get("design_automation_enabled") is True
        )
        if must_lock and self:
            self.env.cr.execute(
                "SELECT id FROM call_center_campaign WHERE id = ANY(%s) FOR UPDATE",
                [self.ids],
            )
            self.invalidate_recordset()
        was_enabled = {
            campaign.id: campaign.design_automation_enabled for campaign in self
        }
        before = {
            campaign.id: {
                field: campaign[field] for field in design_fields & vals.keys()
            }
            for campaign in self.filtered("design_automation_enabled")
        }
        result = super().write(vals)
        for campaign in self.filtered("design_automation_enabled"):
            if not was_enabled.get(campaign.id):
                if not campaign.integration_uuid:
                    campaign._write_integration_state(
                        {"integration_uuid": str(uuid.uuid4())}
                    )
                campaign._create_design_request_event()
                continue
            changed = any(
                campaign[field] != value
                for field, value in before.get(campaign.id, {}).items()
            )
            if changed:
                campaign._create_design_request_event()
        return result

    def _write_integration_state(self, vals):
        """Private, non-RPC state update used only by trusted model methods."""
        return super().write(vals)

    def _create_design_request_event(self, revision=None):
        self.ensure_one()
        unit_code = (self.business_unit_id.code or "").upper()
        purpose = (self.purpose_code or "").upper()
        allowed_units = {"MOY", "COD", "SCP", "MBL", "RLP", "FTP", "TRX", "CAL", "TEST"}
        if unit_code not in allowed_units:
            raise ValidationError(
                "Design automation requires an approved business unit."
            )
        if not purpose or not purpose.replace("_", "").isalnum() or len(purpose) > 16:
            raise ValidationError(
                "Design automation requires a canonical purpose code."
            )
        if self.direction not in {"inbound", "outbound", "blended"}:
            raise ValidationError("Design automation requires a supported direction.")
        if not self.integration_uuid:
            raise ValidationError("Design automation requires an integration UUID.")
        revision = revision or self.design_request_revision + 1
        event_key = f"{EVENT_TYPE}:{self.integration_uuid}:{revision}"
        event_uuid = str(uuid.uuid5(uuid.UUID(self.integration_uuid), event_key))
        correlation_id = str(
            uuid.uuid5(uuid.UUID(self.integration_uuid), f"correlation:{event_key}")
        )
        payload = {
            "event_id": event_uuid,
            "design_request_revision": revision,
            "integration_uuid": self.integration_uuid,
            "odoo_campaign_id": self.id,
            "environment": "staging",
            "business_unit": unit_code,
            "purpose": purpose,
            "direction": self.direction,
            "owner_user_id": self.create_uid.id,
            "supervisor_user_id": self.supervisor_ids[:1].id or self.create_uid.id,
            "correlation_id": correlation_id,
            "design_configuration": {
                "time_zone": self.timezone,
                "calling_hour_start": self.calling_hour_start,
                "calling_hour_end": self.calling_hour_end,
                "consent_required": self.consent_required,
                "dnc_enforced": self.dnc_enforced,
                "team_ids": sorted(self.team_ids.ids),
                "supervisor_ids": sorted(self.supervisor_ids.ids),
            },
        }
        digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        existing = (
            self.env["codestra.runtime.integration.outbox"]
            .sudo()
            .search([("deterministic_event_key", "=", event_key)], limit=1)
        )
        if existing:
            if existing.payload_hash != digest:
                raise ValidationError(
                    "Existing design event has a conflicting payload hash."
                )
            return existing
        self.env["codestra.runtime.integration.outbox"].sudo()._create_internal(
            {
                "event_uuid": event_uuid,
                "deterministic_event_key": event_key,
                "idempotency_key": event_key,
                "event_type": EVENT_TYPE,
                "schema_version": SCHEMA_VERSION,
                "aggregate_type": self._name,
                "aggregate_record_id": self.id,
                "aggregate_uuid": self.integration_uuid,
                "integration_uuid": self.integration_uuid,
                "business_unit_code": unit_code,
                "campaign_id": self.id,
                "design_request_revision": revision,
                "payload_json": payload,
                "payload_hash": digest,
                "correlation_id": correlation_id,
                "delivery_state": "pending",
            }
        )
        self._write_integration_state(
            {
                "design_request_revision": revision,
                "design_request_state": "pending",
                "last_design_event_uuid": event_uuid,
            }
        )
        return (
            self.env["codestra.runtime.integration.outbox"]
            .sudo()
            .search([("deterministic_event_key", "=", event_key)], limit=1)
        )
