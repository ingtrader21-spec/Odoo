from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from typing import ClassVar

from odoo import fields, http
from odoo.http import request
from odoo.exceptions import AccessError, ValidationError

PREFIX = "CODESTRA-INTEGRATION-TEST-"
# PostgreSQL unique_violation. Matched on the driver's SQLSTATE so the
# controller does not import a database driver of its own.
UNIQUE_VIOLATION = "23505"


class CodestraMiddlewareBridge(http.Controller):
    CRM_LEAD_CREATE_FIELDS: ClassVar[set[str]] = {
        "name", "contact_name", "email", "phone", "company_name", "source",
        "campaign", "description", "customer_reference", "external_id", "middleware_id",
        "form_type", "form_version", "source_site", "consent_timestamp",
        "consent_disclosure_version", "sms_consent", "email_marketing_consent",
        "phone_consent", "consent_correlation_id", "consent_status",
        "allow_external_contact", "do_not_call", "suppression_reason",
        "consent_source", "consent_evidence_reference", "review_required",
        "initial_stage", "requested_by", "provenance_method",
        "provenance_reference", "provenance_legal_basis", "provenance_digest",
        "preferred_language", "company_domain", "company_industry",
        "tags",
    }
    CRM_LEAD_PATCH_FIELDS: ClassVar[set[str]] = {
        "name", "contact_name", "email", "phone", "company_name", "source",
        "campaign", "description",
    }
    COMMAND_FIELDS: ClassVar[set[str]] = {
        "command_id", "command_type", "command_version", "target", "tenant_id",
        "requested_by", "correlation_id", "idempotency_key", "capability", "payload",
    }
    COMMAND_PAYLOAD_FIELDS: ClassVar[set[str]] = {
        "lead_source", "source_record_id", "initial_stage", "review_required",
        "allow_external_contact", "provenance", "consent", "lead",
    }
    PROVENANCE_FIELDS: ClassVar[set[str]] = {
        "method", "captured_by", "source_reference", "legal_basis", "content_digest",
    }
    CONSENT_FIELDS: ClassVar[set[str]] = {
        "status", "captured_at", "policy_version", "channels",
    }
    LEAD_FIELDS: ClassVar[set[str]] = {
        "name", "description", "contact", "company", "campaign_code", "tags",
    }
    def _json(self, status, value):
        return request.make_json_response(value, status=status)

    def _body(self):
        raw = request.httprequest.get_data()
        if len(raw) > 131072:
            return None, self._json(413, {"error": "body_too_large"})
        if not raw:
            return {}, None
        try:
            value = json.loads(raw)
        except ValueError:
            return None, self._json(422, {"error": "invalid_json"})
        if not isinstance(value, dict):
            return None, self._json(422, {"error": "object_required"})
        return value, None

    def _authenticate(self, body, tenant_allowlist_parameter=None, service_user_parameter="codestra.middleware.service_user_id"):
        headers = request.httprequest.headers
        timestamp = headers.get("X-Codestra-Timestamp", "")
        event_id = headers.get("X-Codestra-Event-ID", "")
        supplied = headers.get("X-Codestra-Signature", "").removeprefix("sha256=")
        tenant = headers.get("X-Tenant-ID", "")
        correlation = headers.get("X-Correlation-ID", "")
        idempotency = headers.get("Idempotency-Key", "")
        params = request.env["ir.config_parameter"].sudo()
        expected_tenant = params.get_param("codestra.middleware.tenant_id")
        try:
            fresh = abs(int(time.time()) - int(timestamp)) <= 300
        except (TypeError, ValueError):
            fresh = False
        if not all((timestamp, event_id, supplied, tenant, correlation, idempotency)):
            return None, self._json(401, {"error": "missing_authentication"})
        if not fresh:
            return None, self._json(401, {"error": "expired_timestamp"})
        allowed_tenants = {expected_tenant} if expected_tenant else set()
        if tenant_allowlist_parameter:
            allowed_tenants.update(item.strip() for item in (params.get_param(tenant_allowlist_parameter) or "").split(",") if item.strip())
        if not any(hmac.compare_digest(tenant, item) for item in allowed_tenants):
            return None, self._json(403, {"error": "tenant_rejected"})
        # Each tenant is bound to its own secret and its own service identity,
        # so one tenant's credential cannot authenticate another. Global values
        # belong only to the explicitly configured default tenant; adding an
        # allowlist entry must never lend it that tenant's credential/principal.
        tenant_scope = "codestra.middleware.tenant." + tenant + "."
        default_tenant = bool(expected_tenant) and hmac.compare_digest(tenant, expected_tenant)
        secret = (
            params.get_param(tenant_scope + "inbound_hmac_secret")
            or (params.get_param("codestra.middleware.inbound_hmac_secret") if default_tenant else None)
        )
        # The security headers are covered by the signature; otherwise they can
        # be swapped freely on an otherwise valid signed body.
        canonical = b"\n".join((
            timestamp.encode(), event_id.encode(), request.httprequest.method.encode(),
            request.httprequest.path.encode(),
            tenant.encode(), correlation.encode(), idempotency.encode(),
            body,
        ))
        expected = hmac.new((secret or "").encode(), canonical, hashlib.sha256).hexdigest()
        if not secret or not hmac.compare_digest(expected, supplied):
            return None, self._json(401, {"error": "invalid_signature"})
        configured_user = (
            params.get_param(tenant_scope + service_user_parameter)
            or (params.get_param(service_user_parameter, "0") if default_tenant else "0")
        )
        try:
            user_id = int(configured_user)
        except (TypeError, ValueError):
            return None, self._json(403, {"error": "service_identity_rejected"})
        user = request.env["res.users"].sudo().browse(user_id).exists()
        group_xmlid = "codestra_middleware_bridge.group_codestra_crm_api" if service_user_parameter == "codestra.crm.service_user_id" else "codestra_middleware_bridge.group_codestra_middleware_bridge"
        group = request.env.ref(group_xmlid)
        if not user or group not in user.group_ids or not user.active:
            return None, self._json(403, {"error": "service_identity_rejected"})
        # auth="none" starts without an ORM user in Odoo 19. Establish the
        # verified service identity for the whole request/flush lifecycle.
        request.update_env(user=user.id)
        return {
            "event_id": event_id, "tenant_id": tenant,
            "correlation_id": correlation, "idempotency_key": idempotency,
            "request_hash": hashlib.sha256(body).hexdigest(), "user": user,
        }, None

    def _begin(self, operation, allow_event_replay=False, tenant_allowlist_parameter=None, service_user_parameter="codestra.middleware.service_user_id"):
        body = request.httprequest.get_data()
        auth, error = self._authenticate(body, tenant_allowlist_parameter, service_user_parameter)
        if error:
            return None, None, error
        evidence = request.env["codestra.middleware.request"].with_user(auth["user"])
        # Event IDs are globally unique in the ledger. Preserve that collision
        # check, but never return another tenant's recorded response.
        replay = evidence.search([("event_id", "=", auth["event_id"])], limit=1)
        if replay:
            if replay.tenant_id != auth["tenant_id"]:
                return None, None, self._json(409, {"error": "replayed_event_id"})
            if allow_event_replay and replay.request_hash == auth["request_hash"] and replay.operation == operation:
                value = json.loads(replay.response_json)
                value["duplicate"] = True
                return None, None, self._json(200, value)
            return None, None, self._json(409, {"error": "replayed_event_id"})
        prior = evidence.search([
            ("tenant_id", "=", auth["tenant_id"]),
            ("idempotency_key", "=", auth["idempotency_key"]),
        ], limit=1)
        if prior:
            if prior.request_hash != auth["request_hash"] or prior.operation != operation:
                return None, None, self._json(409, {"error": "idempotency_conflict"})
            value = json.loads(prior.response_json)
            value["duplicate"] = True
            return None, None, self._json(200, value)
        payload, error = self._body()
        return auth, payload, error

    def _complete(self, auth, operation, value, partner=None, status=200):
        event_model = request.env["codestra.integration.event"].with_user(auth["user"])
        # Only a deliberately configured synthetic environment records synthetic
        # evidence; a real command must not be audited as a test.
        synthetic = bool(request.env["ir.config_parameter"].sudo().get_param(
            "codestra.middleware.synthetic_test"
        ))
        event = event_model.register_event(
            "middleware.odoo." + operation, "middleware", "odoo",
            {"synthetic_test": synthetic, "partner_id": partner.id if partner else None},
            correlation_id=auth["correlation_id"],
            idempotency_key=f'middleware:{operation}:{auth["idempotency_key"]}',
        )
        request.env["codestra.integration.audit"].with_user(auth["user"])._append(
            event, "middleware." + operation, "success",
            {"partner_id": partner.id if partner else None, "tenant_id": auth["tenant_id"]},
        )
        value.update({"correlation_id": auth["correlation_id"], "duplicate": False})
        request.env["codestra.middleware.request"].with_user(auth["user"]).create({
            "event_id": auth["event_id"], "idempotency_key": auth["idempotency_key"],
            "request_hash": auth["request_hash"], "operation": operation,
            "correlation_id": auth["correlation_id"], "tenant_id": auth["tenant_id"],
            "response_json": json.dumps(value, sort_keys=True),
            "partner_id": partner.id if partner else False,
        })
        return self._json(status, value)

    def _serialized(self, auth, run):
        """Run a write path so a concurrent duplicate cannot double-write.

        The tenant/idempotency uniqueness constraint is the serialization
        point. If a concurrent request records its evidence first, this one
        rolls back to the savepoint and returns the recorded outcome rather
        than surfacing an unhandled server error.
        """
        try:
            with request.env.cr.savepoint():
                return run()
        except Exception as error:
            causes = (error, getattr(error, "__cause__", None))
            if not any(getattr(cause, "pgcode", None) == UNIQUE_VIOLATION for cause in causes):
                raise
        request.env.invalidate_all()
        prior = request.env["codestra.middleware.request"].with_user(auth["user"]).search([
            ("tenant_id", "=", auth["tenant_id"]),
            ("idempotency_key", "=", auth["idempotency_key"]),
        ], limit=1)
        if not prior:
            return self._json(409, {"error": "idempotency_conflict"})
        value = json.loads(prior.response_json)
        value["duplicate"] = True
        return self._json(200, value)

    def _partner(self, auth, partner_id):
        partner = request.env["res.partner"].with_user(auth["user"]).browse(partner_id).exists()
        if not partner or not (partner.name or "").startswith(PREFIX):
            return None
        return partner

    def _crm_scope(self, auth):
        user = auth["user"]
        group = request.env.ref("codestra_middleware_bridge.group_codestra_crm_api")
        units = user.call_center_business_unit_ids
        if group not in user.group_ids or len(units) != 1 or len(user.company_ids) != 1:
            return None
        unit = units[0]
        if unit.company_id != user.company_ids[0]:
            return None
        return unit

    def _crm_mapping(self, auth, external_id):
        return request.env["codestra.crm.external.mapping"].with_user(auth["user"]).search([
            ("customer_key", "=", auth["tenant_id"]), ("external_id", "=", external_id),
            ("model", "=", "crm.lead"),
        ], limit=1)

    @staticmethod
    def _crm_lead_value(lead, mapping):
        return {
            "external_id": mapping.external_id, "middleware_id": mapping.middleware_id,
            "name": lead.name, "contact_name": lead.contact_name,
            "email": lead.email_from, "phone": lead.phone,
            "company_name": lead.partner_name, "description": lead.description,
            "source": lead.source_id.name if lead.source_id else None,
            "campaign": lead.campaign_id.name if lead.campaign_id else None,
            "form_type": lead.codestra_form_type, "form_version": lead.codestra_form_version,
            "source_site": lead.codestra_source_site,
            "sms_consent": lead.codestra_sms_consent,
            "email_marketing_consent": lead.codestra_email_marketing_consent,
            "phone_consent": lead.codestra_phone_consent,
            "consent_correlation_id": lead.codestra_consent_correlation_id,
            "preferred_language": lead.codestra_preferred_language,
            "company_domain": lead.codestra_company_domain,
            "company_industry": lead.codestra_company_industry,
            "consent_status": lead.consent_status,
            "allow_external_contact": lead.codestra_allow_external_contact,
            "review_required": lead.codestra_review_required,
            "initial_stage": lead.codestra_initial_stage,
            "do_not_call": lead.do_not_call,
            "contact_eligibility": lead.contact_eligibility,
            "contact_eligibility_reason": lead.contact_eligibility_reason,
            "status": "active" if lead.active else "archived",
            "created_at": lead.create_date.isoformat() if lead.create_date else None,
            "updated_at": lead.write_date.isoformat() if lead.write_date else None,
        }

    def _crm_values(self, payload, allowed, unit, user):
        unsupported = sorted(set(payload) - allowed)
        if unsupported:
            return None, self._json(422, {"error": "unsupported_fields", "fields": unsupported})
        for field_name in (
            "sms_consent", "email_marketing_consent", "phone_consent",
            "allow_external_contact", "do_not_call", "review_required",
        ):
            if field_name in payload and not isinstance(payload[field_name], bool):
                return None, self._json(422, {"error": "invalid_boolean", "field": field_name})
        if payload.get("consent_status", "unknown") not in {
            "unknown", "granted", "denied", "not_applicable",
        }:
            return None, self._json(422, {"error": "invalid_consent_status"})
        if payload.get("suppression_reason", "optout") not in {
            "dnc", "optout", "complaint", "legal", "invalid", "fraud",
        }:
            return None, self._json(422, {"error": "invalid_suppression_reason"})
        if payload.get("initial_stage", "new") not in {"new", "review_pending"}:
            return None, self._json(422, {"error": "invalid_initial_stage"})
        if payload.get("review_required") and (
            payload.get("initial_stage") != "review_pending"
            or payload.get("allow_external_contact") is not False
        ):
            return None, self._json(422, {"error": "unsafe_review_state"})
        if payload.get("do_not_call") and payload.get("phone_consent"):
            return None, self._json(422, {"error": "conflicting_phone_consent"})
        # External contact is only representable when consent was granted for at
        # least one channel. This rejects the "unknown consent, contact allowed"
        # and "granted with every channel false" combinations the contract
        # otherwise permits.
        if payload.get("allow_external_contact") and (
            payload.get("consent_status") != "granted"
            or not any(
                payload.get(name)
                for name in ("phone_consent", "email_marketing_consent", "sms_consent")
            )
        ):
            return None, self._json(422, {"error": "consent_does_not_permit_contact"})
        values = {}
        mapping = {
            "name": "name", "contact_name": "contact_name", "email": "email_from",
            "phone": "phone", "company_name": "partner_name", "description": "description",
            "external_id": "external_source_id", "customer_reference": "source_detail",
        }
        for source, target in mapping.items():
            if source in payload:
                values[target] = payload[source]
        consent_mapping = {
            "form_type": "codestra_form_type", "form_version": "codestra_form_version",
            "source_site": "codestra_source_site", "consent_timestamp": "codestra_consent_timestamp",
            "consent_disclosure_version": "codestra_consent_disclosure_version",
            "sms_consent": "codestra_sms_consent",
            "email_marketing_consent": "codestra_email_marketing_consent",
            "phone_consent": "codestra_phone_consent",
            "consent_correlation_id": "codestra_consent_correlation_id",
            "allow_external_contact": "codestra_allow_external_contact",
            "review_required": "codestra_review_required",
            "initial_stage": "codestra_initial_stage",
            "requested_by": "codestra_requested_by",
            "provenance_method": "codestra_provenance_method",
            "provenance_reference": "codestra_provenance_reference",
            "provenance_legal_basis": "codestra_provenance_legal_basis",
            "provenance_digest": "codestra_provenance_digest",
            "preferred_language": "codestra_preferred_language",
            "company_domain": "codestra_company_domain",
            "company_industry": "codestra_company_industry",
        }
        for source, target in consent_mapping.items():
            if source in payload:
                values[target] = payload[source]
        if payload.get("source"):
            record = request.env["utm.source"].with_user(user).search(
                [("name", "=", payload["source"])], limit=1
            )
            if not record:
                return None, self._json(422, {"error": "unknown_source"})
            values["source_id"] = record.id
        # crm.lead.campaign_id is redefined by codestra_cc_crm as cc.campaign, so
        # the campaign code resolves against the governed workspace scoped to the
        # authorized business unit, never against utm.campaign.
        if payload.get("campaign"):
            # The service identity is scoped by the already-authorized legacy
            # business unit, but it deliberately has no human campaign
            # membership. Resolve that governed workspace under the explicit
            # unit boundary instead of granting a synthetic operator role.
            campaign = request.env["cc.campaign"].sudo().search([
                ("code", "=", payload["campaign"]),
                ("cc_business_unit_id.legacy_business_unit_id", "=", unit.id),
            ], limit=1)
            if not campaign:
                return None, self._json(422, {"error": "unknown_campaign"})
            values["campaign_id"] = campaign.id
            # Governed campaign records require an explicit source-list key.
            values["cc_source_list_key"] = (
                payload.get("customer_reference")
                or payload.get("external_id")
                or payload.get("source")
                or "codestra-middleware"
            )
        if "tags" in payload:
            tags = payload["tags"]
            if (
                not isinstance(tags, list)
                or len(tags) > 50
                or any(not isinstance(tag, str) or not tag or len(tag) > 128 for tag in tags)
                or len(tags) != len(set(tags))
            ):
                return None, self._json(422, {"error": "invalid_tags"})
            records = request.env["crm.tag"].with_user(user).search([("name", "in", tags)])
            unknown = sorted(set(tags) - set(records.mapped("name")))
            if unknown:
                return None, self._json(422, {"error": "unknown_tags", "tags": unknown})
            values["tag_ids"] = [(6, 0, records.ids)]
        if "initial_stage" in payload:
            stage_xmlid = (
                "codestra_middleware_bridge.crm_stage_middleware_review_pending"
                if payload["initial_stage"] == "review_pending"
                else "codestra_middleware_bridge.crm_stage_middleware_intake"
            )
            values["stage_id"] = request.env.ref(stage_xmlid).id
        values.update({"company_id": unit.company_id.id, "business_unit_id": unit.id})
        return values, None

    def _apply_crm_compliance(self, auth, lead, payload, unit):
        lead._codestra_apply_crm_compliance(auth, payload, unit)

    def _command_to_crm_payload(self, command, auth):
        if set(command) != self.COMMAND_FIELDS:
            return None, self._json(422, {"error": "invalid_command_fields"})
        if (
            command.get("command_type") != "crm.lead.upsert"
            or command.get("command_version") != "1.0"
            or command.get("target") != "odoo-19"
            or command.get("capability") != "ODOO_WRITE"
        ):
            return None, self._json(422, {"error": "unsupported_command"})
        try:
            uuid.UUID(str(command.get("command_id")))
        except (TypeError, ValueError, AttributeError):
            return None, self._json(422, {"error": "invalid_command_id"})
        for field_name in (
            "tenant_id", "requested_by", "correlation_id", "idempotency_key"
        ):
            value = command.get(field_name)
            if not isinstance(value, str) or not value.strip():
                return None, self._json(422, {
                    "error": "invalid_command_identity", "field": field_name,
                })
        if (
            command.get("command_id") != auth["event_id"]
            or command.get("tenant_id") != auth["tenant_id"]
            or command.get("correlation_id") != auth["correlation_id"]
            or command.get("idempotency_key") != auth["idempotency_key"]
        ):
            return None, self._json(422, {"error": "command_header_mismatch"})
        payload = command.get("payload")
        if not isinstance(payload, dict) or set(payload) != self.COMMAND_PAYLOAD_FIELDS:
            return None, self._json(422, {"error": "invalid_command_payload_fields"})
        provenance = payload.get("provenance")
        consent = payload.get("consent")
        lead = payload.get("lead")
        if (
            not isinstance(provenance, dict)
            or not self.PROVENANCE_FIELDS.issuperset(provenance)
            or not {"method", "captured_by", "source_reference", "legal_basis"}.issubset(provenance)
            or not isinstance(consent, dict)
            or not self.CONSENT_FIELDS.issuperset(consent)
            or not {"status", "channels"}.issubset(consent)
            or not isinstance(lead, dict)
            or not self.LEAD_FIELDS.issuperset(lead)
            or not {"name", "description", "contact", "company", "tags"}.issubset(lead)
        ):
            return None, self._json(422, {"error": "invalid_nested_command_fields"})
        channels = consent.get("channels")
        if not isinstance(channels, dict) or set(channels) != {"email", "sms", "phone"}:
            return None, self._json(422, {"error": "invalid_consent_channels"})
        if any(not isinstance(value, bool) for value in channels.values()):
            return None, self._json(422, {"error": "invalid_consent_channels"})
        if consent.get("status") not in {
            "granted", "denied", "not_applicable", "unknown",
        }:
            return None, self._json(422, {"error": "invalid_consent_status"})
        if provenance.get("method") not in {
            "submitted_by_person", "crawler_discovery", "scraper_import",
        } or provenance.get("legal_basis") not in {
            "consent", "legitimate_interest_review_required", "contract_request",
            "unknown_review_required",
        }:
            return None, self._json(422, {"error": "invalid_provenance"})
        for field_name in ("captured_by", "source_reference"):
            value = provenance.get(field_name)
            if not isinstance(value, str) or not value.strip():
                return None, self._json(422, {
                    "error": "invalid_provenance", "field": field_name,
                })
        digest = provenance.get("content_digest")
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            return None, self._json(422, {"error": "invalid_provenance_digest"})
        contact = lead.get("contact") or {}
        company = lead.get("company") or {}
        if (
            not isinstance(contact, dict)
            or not {"name", "email", "phone", "preferred_language"}.issuperset(contact)
            or not isinstance(company, dict)
            or not {"name", "domain", "industry"}.issuperset(company)
        ):
            return None, self._json(422, {"error": "invalid_lead_subject"})
        # Nested values are optional, but when present they must be non-empty
        # bounded strings. Without this a malformed value reaches the ORM and
        # becomes a server error instead of a deterministic 422.
        # Limits mirror odoo-lead-command.schema.json exactly. A narrower cap here
        # would reject payloads the published contract accepts.
        for container, container_name, limits in (
            (contact, "contact", {
                "name": 255, "email": 320, "phone": 64, "preferred_language": 16,
            }),
            (company, "company", {"name": 255, "domain": 255, "industry": 255}),
            (lead, "lead", {"name": 255, "campaign_code": 128, "description": 10000}),
        ):
            for field_name, limit in limits.items():
                value = container.get(field_name)
                if value is None:
                    continue
                if not isinstance(value, str) or not value.strip() or len(value) > limit:
                    return None, self._json(422, {
                        "error": "invalid_lead_subject_value",
                        "section": container_name,
                        "field": field_name,
                    })
        if (
            payload.get("review_required") is True
            and (
                payload.get("initial_stage") != "review_pending"
                or payload.get("allow_external_contact") is not False
            )
        ):
            return None, self._json(422, {"error": "unsafe_review_state"})
        if not all(isinstance(payload.get(field), bool) for field in ("review_required", "allow_external_contact")):
            return None, self._json(422, {"error": "invalid_command_boolean"})
        if payload.get("initial_stage") not in {"new", "review_pending"}:
            return None, self._json(422, {"error": "invalid_initial_stage"})
        for field_name in ("lead_source", "source_record_id"):
            value = payload.get(field_name)
            if not isinstance(value, str) or not value.strip():
                return None, self._json(422, {
                    "error": "invalid_command_payload", "field": field_name,
                })
        if not isinstance(lead.get("name"), str) or not lead["name"].strip():
            return None, self._json(422, {"error": "missing_lead_name"})
        captured_at = consent.get("captured_at")
        if captured_at:
            try:
                parsed_captured_at = datetime.fromisoformat(
                    str(captured_at).replace("Z", "+00:00")
                )
                if parsed_captured_at.tzinfo:
                    parsed_captured_at = parsed_captured_at.astimezone(
                        timezone.utc
                    ).replace(tzinfo=None)
                captured_at = fields.Datetime.to_string(parsed_captured_at)
            except (TypeError, ValueError):
                return None, self._json(422, {"error": "invalid_consent_timestamp"})
        return {
            "name": lead["name"],
            "description": lead.get("description"),
            "contact_name": contact.get("name"),
            "email": contact.get("email"),
            "phone": contact.get("phone"),
            "company_name": company.get("name"),
            "preferred_language": contact.get("preferred_language"),
            "company_domain": company.get("domain"),
            "company_industry": company.get("industry"),
            "source": payload["lead_source"],
            "campaign": lead.get("campaign_code"),
            "external_id": payload["source_record_id"],
            "middleware_id": command["command_id"],
            "initial_stage": payload["initial_stage"],
            "review_required": payload["review_required"],
            "allow_external_contact": payload["allow_external_contact"],
            "form_type": provenance["method"],
            "source_site": provenance["source_reference"],
            "consent_timestamp": captured_at,
            "consent_disclosure_version": consent.get("policy_version"),
            "email_marketing_consent": channels["email"],
            "sms_consent": channels["sms"],
            "phone_consent": channels["phone"],
            "consent_status": consent["status"],
            "consent_correlation_id": command["correlation_id"],
            "consent_source": provenance["captured_by"],
            "consent_evidence_reference": provenance.get("content_digest") or provenance["source_reference"],
            "do_not_call": consent["status"] == "denied",
            "suppression_reason": "optout",
            "requested_by": command["requested_by"],
            "provenance_method": provenance["method"],
            "provenance_reference": provenance["source_reference"],
            "provenance_legal_basis": provenance["legal_basis"],
            "provenance_digest": provenance.get("content_digest"),
            "tags": lead["tags"],
        }, None

    def _prepare_update_values(self, lead, values):
        """Strip immutable campaign ownership from an update.

        codestra_cc_crm treats campaign_id and cc_source_list_key as immutable,
        so an unchanged binding is dropped and a changed one is a conflict
        rather than an AccessError surfaced as a 500.
        """
        incoming = values.pop("campaign_id", None)
        values.pop("cc_source_list_key", None)
        if incoming is not None and incoming != lead.campaign_id.id:
            return self._json(409, {"error": "campaign_binding_immutable"})
        return None

    def _reject_stale_update(self, lead, payload):
        """Reject a command that carries older consent than the stored record."""
        incoming = payload.get("consent_timestamp")
        existing = lead.codestra_consent_timestamp
        if not incoming or not existing:
            return None
        parsed = fields.Datetime.to_datetime(incoming)
        if parsed and parsed < existing:
            return self._json(409, {
                "error": "stale_command",
                "stored_consent_timestamp": existing.isoformat(),
            })
        return None

    def _crm_lead_list(self):
        auth, _payload, error = self._begin(
            "crm.lead.list", allow_event_replay=True,
            tenant_allowlist_parameter="codestra.crm.tenant_ids",
            service_user_parameter="codestra.crm.service_user_id",
        )
        if error: return error
        unit = self._crm_scope(auth)
        if not unit: return self._json(403, {"error": "crm_service_scope_rejected"})
        try:
            limit = min(int(request.httprequest.args.get("limit", 50)), 200)
            offset = max(int(request.httprequest.args.get("offset", 0)), 0)
        except ValueError:
            return self._json(422, {"error": "invalid_pagination"})
        mappings = request.env["codestra.crm.external.mapping"].with_user(auth["user"]).search(
            [("customer_key", "=", auth["tenant_id"]), ("model", "=", "crm.lead"),
             ("business_unit_id", "=", unit.id)],
            limit=limit, offset=offset, order="id",
        )
        by_record_id = {mapping.record_id: mapping for mapping in mappings}
        leads = request.env["crm.lead"].with_user(auth["user"]).browse(list(by_record_id))
        items = [
            self._crm_lead_value(lead, by_record_id[lead.id])
            for lead in leads if lead.exists() and lead.business_unit_id == unit
        ]
        return self._complete(auth, "crm.lead.list", {
            "items": items, "limit": limit, "offset": offset,
        })

    def _create_crm_lead(self, auth, payload, unit):
        values, error = self._crm_values(
            payload, self.CRM_LEAD_CREATE_FIELDS, unit, auth["user"]
        )
        if error:
            return None, None, error
        if not payload.get("name") or not payload.get("external_id") or not payload.get("middleware_id"):
            return None, None, self._json(422, {"error": "missing_required_fields"})
        # The integration identity creates an unassigned lead; assigning it as
        # a human owner would incorrectly require an operator membership.
        values.update({"type": "lead", "user_id": False})
        # Payload fields and ownership were validated above.  Elevation is
        # limited to the ORM create so delegated campaign constraints can read
        # the already-bound governed workspace without requiring a human
        # campaign membership on the non-interactive service identity.
        lead = request.env["crm.lead"].with_user(auth["user"]).sudo().with_company(
            unit.company_id
        ).create(values)
        mapping = request.env["codestra.crm.external.mapping"].with_user(auth["user"]).create({
            "customer_key": auth["tenant_id"], "external_id": payload["external_id"],
            "middleware_id": payload["middleware_id"], "model": "crm.lead", "record_id": lead.id,
            "company_id": unit.company_id.id, "business_unit_id": unit.id,
            "service_user_id": auth["user"].id,
        })
        self._apply_crm_compliance(auth, lead, payload, unit)
        return lead, mapping, None

    @http.route(
        "/codestra/middleware/v1/klyrow/events",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        readonly=False,
    )
    def klyrow_event_projection(self):
        auth, payload, error = self._begin(
            "klyrow.event.project",
            allow_event_replay=True,
            tenant_allowlist_parameter="codestra.klyrow.tenant_ids",
            service_user_parameter="codestra.klyrow.service_user_id",
        )
        if error:
            return error
        if (
            payload.get("event_id") != auth["event_id"]
            or payload.get("tenant_id") != auth["tenant_id"]
            or payload.get("correlation_id") != auth["correlation_id"]
        ):
            return self._json(409, {"error": "klyrow_projection_binding_conflict"})

        def run():
            try:
                projection, outcome = request.env[
                    "codestra.klyrow.business.projection"
                ].with_user(auth["user"]).apply_event(payload, auth["request_hash"])
            except ValidationError:
                return self._json(422, {"error": "invalid_klyrow_projection"})
            if not projection and outcome == "tenant_projection_missing":
                return self._json(404, {"error": "tenant_projection_missing"})
            return self._complete(
                auth,
                "klyrow.event.project",
                {
                    "operation_id": payload["operation_id"],
                    "status": "APPLIED",
                    "outcome": outcome,
                    "payload_sha256": auth["request_hash"],
                    "projection_id": projection.id,
                    "event_type": projection.event_type,
                },
                status=201,
            )

        return self._serialized(auth, run)

    @http.route(
        "/codestra/middleware/v1/klyrow/events/<string:operation_id>",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        readonly=True,
    )
    def klyrow_event_projection_status(self, operation_id):
        body = request.httprequest.get_data()
        if body:
            return self._json(422, {"error": "status_body_forbidden"})
        auth, error = self._authenticate(
            body,
            tenant_allowlist_parameter="codestra.klyrow.tenant_ids",
            service_user_parameter="codestra.klyrow.service_user_id",
        )
        if error:
            return error
        evidence = request.env["codestra.middleware.request"].with_user(
            auth["user"]
        ).search(
            [
                ("tenant_id", "=", auth["tenant_id"]),
                ("event_id", "=", auth["event_id"]),
                ("operation", "=", "klyrow.event.project"),
            ],
            limit=1,
        )
        if not evidence:
            return self._json(404, {"error": "klyrow_projection_not_found"})
        try:
            result = json.loads(evidence.response_json)
        except ValueError:
            return self._json(409, {"error": "klyrow_projection_evidence_invalid"})
        if result.get("operation_id") != operation_id:
            return self._json(404, {"error": "klyrow_projection_not_found"})
        return self._json(
            200,
            {
                "operation_id": operation_id,
                "status": "APPLIED",
                "payload_sha256": evidence.request_hash,
            },
        )

    @http.route("/codestra/middleware/v1/crm/leads", type="http", auth="none", methods=["GET", "POST"], csrf=False, readonly=False)
    def crm_lead_create(self):
        if request.httprequest.method == "GET":
            return self._crm_lead_list()
        auth, payload, error = self._begin("crm.lead.create", allow_event_replay=True, tenant_allowlist_parameter="codestra.crm.tenant_ids", service_user_parameter="codestra.crm.service_user_id")
        if error: return error
        unit = self._crm_scope(auth)
        if not unit: return self._json(403, {"error": "crm_service_scope_rejected"})
        def run():
            lead, mapping, error = self._create_crm_lead(auth, payload, unit)
            if error:
                return error
            return self._complete(
                auth, "crm.lead.create", self._crm_lead_value(lead, mapping), status=201
            )

        return self._serialized(auth, run)

    @http.route(
        "/codestra/middleware/v1/commands/crm.lead.upsert",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        readonly=False,
    )
    def crm_lead_upsert_command(self):
        auth, command, error = self._begin(
            "crm.lead.upsert",
            allow_event_replay=True,
            tenant_allowlist_parameter="codestra.crm.tenant_ids",
            service_user_parameter="codestra.crm.service_user_id",
        )
        if error:
            return error
        unit = self._crm_scope(auth)
        if not unit:
            return self._json(403, {"error": "crm_service_scope_rejected"})
        payload, error = self._command_to_crm_payload(command, auth)
        if error:
            return error

        def run():
            mapping = self._crm_mapping(auth, payload["external_id"])
            status = 200
            outcome = "updated"
            if mapping:
                lead = request.env["crm.lead"].with_user(auth["user"]).browse(
                    mapping.record_id
                ).exists()
                if not lead:
                    # The mapping outlived its lead. Say so distinctly so the
                    # Middleware can reconcile instead of retrying forever
                    # against a generic scope conflict.
                    return self._json(409, {
                        "error": "mapping_target_missing",
                        "external_id": mapping.external_id,
                        "middleware_id": mapping.middleware_id,
                    })
                if lead.company_id != unit.company_id or lead.business_unit_id != unit:
                    return self._json(409, {"error": "external_mapping_scope_conflict"})
                values, error = self._crm_values(
                    payload, self.CRM_LEAD_CREATE_FIELDS, unit, auth["user"]
                )
                if error:
                    return error
                error = self._reject_stale_update(lead, payload)
                if error:
                    return error
                error = self._prepare_update_values(lead, values)
                if error:
                    return error
                lead.write(values)
                self._apply_crm_compliance(auth, lead, payload, unit)
            else:
                lead, mapping, error = self._create_crm_lead(auth, payload, unit)
                if error:
                    return error
                status = 201
                outcome = "created"
            result = self._crm_lead_value(lead, mapping)
            result.update({"command_id": command["command_id"], "outcome": outcome})
            return self._complete(auth, "crm.lead.upsert", result, status=status)

        return self._serialized(auth, run)

    @http.route(
        "/codestra/middleware/v1/commands/<string:command_id>/status",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        # The status read records an idempotent integration audit event.
        readonly=False,
    )
    def crm_command_status(self, command_id):
        """Report the recorded outcome of an earlier command.

        Reconciliation needs a way to ask what actually happened to a command
        whose response was never observed, rather than replaying a write.
        """
        auth, _payload, error = self._begin(
            "crm.command.status",
            allow_event_replay=True,
            tenant_allowlist_parameter="codestra.crm.tenant_ids",
            service_user_parameter="codestra.crm.service_user_id",
        )
        if error:
            return error
        unit = self._crm_scope(auth)
        if not unit:
            return self._json(403, {"error": "crm_service_scope_rejected"})
        record = request.env["codestra.middleware.request"].with_user(auth["user"]).search([
            ("tenant_id", "=", auth["tenant_id"]),
            ("event_id", "=", command_id),
        ], limit=1)
        if not record:
            return self._json(404, {"error": "command_not_found"})
        return self._complete(auth, "crm.command.status", {
            "command_id": command_id,
            "operation": record.operation,
            "recorded_at": record.created_at.isoformat(),
            "result": json.loads(record.response_json),
        })

    @http.route("/codestra/middleware/v1/crm/leads/<string:external_id>", type="http", auth="none", methods=["GET", "PATCH"], csrf=False)
    def crm_lead(self, external_id):
        operation = "crm.lead.read" if request.httprequest.method == "GET" else "crm.lead.update"
        auth, payload, error = self._begin(operation, allow_event_replay=True, tenant_allowlist_parameter="codestra.crm.tenant_ids", service_user_parameter="codestra.crm.service_user_id")
        if error: return error
        unit = self._crm_scope(auth)
        mapping = unit and self._crm_mapping(auth, external_id)
        if not mapping: return self._json(404, {"error": "lead_not_found"})
        lead = request.env["crm.lead"].with_user(auth["user"]).browse(mapping.record_id).exists()
        if not lead or lead.company_id != unit.company_id or lead.business_unit_id != unit:
            return self._json(404, {"error": "lead_not_found"})
        if operation.endswith("update"):
            values, error = self._crm_values(payload, self.CRM_LEAD_PATCH_FIELDS, unit, auth["user"])
            if error: return error
            error = self._prepare_update_values(lead, values)
            if error: return error
            lead.write(values)
        return self._complete(auth, operation, self._crm_lead_value(lead, mapping))

    @http.route("/codestra/middleware/v1/crm/activities", type="http", auth="none", methods=["POST"], csrf=False)
    def crm_activity_create(self):
        auth, payload, error = self._begin("crm.activity.create", allow_event_replay=True, tenant_allowlist_parameter="codestra.crm.tenant_ids", service_user_parameter="codestra.crm.service_user_id")
        if error: return error
        allowed = {"lead_external_id", "activity_type", "summary", "note", "due_date", "middleware_id"}
        unsupported = sorted(set(payload) - allowed)
        if unsupported: return self._json(422, {"error": "unsupported_fields", "fields": unsupported})
        unit = self._crm_scope(auth); mapping = unit and self._crm_mapping(auth, str(payload.get("lead_external_id", "")))
        if not mapping: return self._json(404, {"error": "lead_not_found"})
        activity_refs = {"todo": "mail.mail_activity_data_todo", "call": "mail.mail_activity_data_call", "email": "mail.mail_activity_data_email"}
        ref = activity_refs.get(str(payload.get("activity_type", "todo")))
        if not ref or not payload.get("summary"): return self._json(422, {"error": "invalid_activity"})
        lead = request.env["crm.lead"].with_user(auth["user"]).browse(mapping.record_id).exists()
        if not lead or lead.business_unit_id != unit: return self._json(404, {"error": "lead_not_found"})
        activity = request.env["mail.activity"].with_user(auth["user"]).create({
            "activity_type_id": request.env.ref(ref).id, "summary": payload["summary"],
            "note": payload.get("note"), "date_deadline": payload.get("due_date") or fields.Date.today(),
            "res_model_id": request.env["ir.model"]._get_id("crm.lead"),
            "res_id": lead.id, "user_id": auth["user"].id,
        })
        return self._complete(auth, "crm.activity.create", {
            "external_id": payload.get("middleware_id"), "lead_external_id": mapping.external_id,
            "status": "scheduled", "created_at": activity.create_date.isoformat(),
        }, status=201)

    @http.route("/codestra/middleware/v1/email-events", type="http", auth="none", methods=["POST"], csrf=False)
    def email_event(self):
        auth, payload, error = self._begin("email.status", allow_event_replay=True, tenant_allowlist_parameter="codestra.middleware.email_tenant_ids")
        if error: return error
        status_by_type = {
            "klyrow.email.accepted": "accepted",
            "klyrow.email.queued": "queued",
            "klyrow.email.submitted": "provider_accepted",
            "klyrow.email.sent": "provider_accepted",
            "klyrow.email.delivered": "delivered",
            "klyrow.email.bounced": "bounced",
            "klyrow.email.deferred": "deferred",
            "klyrow.email.complained": "complained",
            "klyrow.email.unsubscribed": "suppressed",
            "klyrow.email.suppressed": "suppressed",
            "klyrow.email.rejected": "rejected",
            "klyrow.email.failed": "failed",
        }
        event_type = str(payload.get("event_type", ""))
        status = status_by_type.get(event_type)
        detail = payload.get("payload") or {}
        message_id = str(detail.get("message_id", ""))
        occurred_at = payload.get("occurred_at") or detail.get("timestamp")
        if not status or not message_id or not occurred_at:
            return self._json(422, {"error": "invalid_email_status_event"})
        occurred_at = str(occurred_at).replace("T", " ").removesuffix("Z").split(".", 1)[0]
        record = request.env["codestra.email.delivery.status"].with_user(auth["user"]).create({
            "event_id": auth["event_id"], "correlation_id": auth["correlation_id"],
            "message_id": message_id, "tenant_id": auth["tenant_id"],
            "customer_id": payload.get("customer_id"), "provider": "klyrow",
            "event_type": event_type, "status": status, "occurred_at": occurred_at,
        })
        projection = "not_installed"
        if "codestra.email.outbox" in request.env.registry.models:
            job = request.env["codestra.email.outbox"].sudo().search([
                ("tenant_id", "=", auth["tenant_id"]),
                ("correlation_id", "=", auth["correlation_id"]),
                ("message_id", "=", message_id),
            ], limit=1)
            if job:
                changed = job._apply_callback(
                    state=status, tenant=auth["tenant_id"],
                    correlation=auth["correlation_id"], message_id=message_id,
                    provider_reference=detail.get("provider_message_id"),
                )
                projection = "updated" if changed else "ignored_older_state"
            else:
                projection = "unmatched"
        return self._complete(auth, "email.status", {
            "record_id": record.id, "model": record._name, "status": record.status,
            "event_id": record.event_id, "message_id": record.message_id,
            "projection": projection,
        }, status=201)

    @http.route("/codestra/middleware/v1/contacts", type="http", auth="none", methods=["POST"], csrf=False)
    def create_contact(self):
        auth, payload, error = self._begin("contact.create")
        if error: return error
        name = str(payload.get("name", ""))
        external_id = str(payload.get("external_id", ""))
        if not name.startswith(PREFIX) or not external_id.startswith(PREFIX):
            return self._json(422, {"error": "synthetic_tag_required"})
        partner = request.env["res.partner"].with_user(auth["user"]).create({
            "name": name, "email": payload.get("email"), "phone": payload.get("phone"),
            "codestra_integration_external_id": external_id,
            "codestra_integration_status": "test",
            "comment": "Synthetic middleware connectivity record; safe to archive.",
        })
        return self._complete(auth, "contact.create", self._contact_value(partner), partner, 201)

    @http.route("/codestra/middleware/v1/contacts/<int:partner_id>", type="http", auth="none", methods=["GET", "PATCH"], csrf=False)
    def contact(self, partner_id):
        operation = "contact.read" if request.httprequest.method == "GET" else "contact.update"
        auth, payload, error = self._begin(operation)
        if error: return error
        partner = self._partner(auth, partner_id)
        if not partner: return self._json(404, {"error": "synthetic_contact_not_found"})
        if operation == "contact.update":
            values = {key: payload[key] for key in ("email", "phone") if key in payload}
            if "name" in payload:
                if not str(payload["name"]).startswith(PREFIX):
                    return self._json(422, {"error": "synthetic_tag_required"})
                values["name"] = payload["name"]
            partner.write(values)
        return self._complete(auth, operation, self._contact_value(partner), partner)

    @http.route("/codestra/middleware/v1/contacts/<int:partner_id>/activities", type="http", auth="none", methods=["POST"], csrf=False)
    def activity(self, partner_id):
        auth, payload, error = self._begin("activity.create")
        if error: return error
        partner = self._partner(auth, partner_id)
        if not partner: return self._json(404, {"error": "synthetic_contact_not_found"})
        summary = str(payload.get("summary", ""))
        if not summary.startswith(PREFIX):
            return self._json(422, {"error": "synthetic_tag_required"})
        activity = request.env["mail.activity"].with_user(auth["user"]).create({
            "activity_type_id": request.env.ref("mail.mail_activity_data_todo").id,
            "summary": summary, "note": payload.get("note", "Synthetic connectivity activity"),
            "res_model_id": request.env["ir.model"]._get_id("res.partner"),
            "res_id": partner.id, "user_id": auth["user"].id,
        })
        return self._complete(auth, "activity.create", {"activity_id": activity.id, "partner_id": partner.id}, partner, 201)

    @http.route("/codestra/middleware/v1/contacts/<int:partner_id>/status", type="http", auth="none", methods=["POST"], csrf=False)
    def status(self, partner_id):
        auth, payload, error = self._begin("contact.status")
        if error: return error
        partner = self._partner(auth, partner_id)
        if not partner: return self._json(404, {"error": "synthetic_contact_not_found"})
        value = payload.get("status")
        if value not in {"test", "active", "inactive"}:
            return self._json(422, {"error": "invalid_status"})
        partner.write({"codestra_integration_status": value})
        return self._complete(auth, "contact.status", self._contact_value(partner), partner)

    @staticmethod
    def _contact_value(partner):
        return {
            "partner_id": partner.id, "external_id": partner.codestra_integration_external_id,
            "name": partner.name, "email": partner.email, "phone": partner.phone,
            "status": partner.codestra_integration_status,
        }

    # -- Customer profiles (real production contacts; unlike res.partner
    # above, these are not synthetic-tag-gated -- cc.customer.profile is the
    # actual contact-center customer record, scoped to the caller's business
    # unit the same way crm.lead already is. --

    def _canonical_unit(self, auth):
        """cc.customer.profile/cc.helpdesk.* scope by cc.business.unit, the
        _inherits-delegated canonical model -- _crm_scope's business unit is
        the legacy call.center.business.unit it wraps, a different id/model.
        """
        unit = self._crm_scope(auth)
        if not unit:
            return None
        return request.env["cc.business.unit"].with_user(auth["user"]).search(
            [("legacy_business_unit_id", "=", unit.id)], limit=1
        )

    def _customer_profile(self, auth, profile_id):
        unit = self._canonical_unit(auth)
        if not unit:
            return None
        try:
            profile = request.env["cc.customer.profile"].with_user(auth["user"]).browse(profile_id).exists()
            if not profile or profile.business_unit_id != unit:
                return None
            return profile
        except AccessError:
            # Record rules intentionally hide cross-unit records. The HTTP
            # contract presents that boundary as a not-found response.
            return None

    @staticmethod
    def _customer_profile_value(profile):
        return {
            "profile_id": profile.id, "profile_uuid": profile.profile_uuid,
            "name": profile.name, "email_masked": profile.email_masked,
            "phone_masked": profile.phone_masked, "state": profile.state,
            "verification_state": profile.verification_state,
            "assigned_user_id": profile.assigned_user_id.id or None,
            "campaign_id": profile.campaign_id.id,
        }

    @http.route("/codestra/middleware/v1/customer-profiles", type="http", auth="none", methods=["GET", "POST"], csrf=False, readonly=False)
    def customer_profiles(self):
        if request.httprequest.method == "GET":
            auth, _payload, error = self._begin("customer_profile.list", allow_event_replay=True, tenant_allowlist_parameter="codestra.crm.tenant_ids", service_user_parameter="codestra.crm.service_user_id")
            if error: return error
            unit = self._canonical_unit(auth)
            if not unit: return self._json(403, {"error": "crm_service_scope_rejected"})
            try:
                limit = min(int(request.httprequest.args.get("limit", 50)), 200)
                offset = max(int(request.httprequest.args.get("offset", 0)), 0)
            except ValueError:
                return self._json(422, {"error": "invalid_pagination"})
            profiles = request.env["cc.customer.profile"].with_user(auth["user"]).search(
                [("business_unit_id", "=", unit.id)], limit=limit, offset=offset, order="id",
            )
            return self._complete(auth, "customer_profile.list", {
                "items": [self._customer_profile_value(profile) for profile in profiles],
                "limit": limit, "offset": offset,
            })

        auth, payload, error = self._begin("customer_profile.create", tenant_allowlist_parameter="codestra.crm.tenant_ids", service_user_parameter="codestra.crm.service_user_id")
        if error: return error
        unit = self._canonical_unit(auth)
        if not unit: return self._json(403, {"error": "crm_service_scope_rejected"})
        partner_id = payload.get("partner_id")
        campaign_id = payload.get("campaign_id")
        if not partner_id or not campaign_id or not payload.get("integration_key"):
            return self._json(422, {"error": "missing_required_field", "fields": ["partner_id", "campaign_id", "integration_key"]})
        partner = request.env["res.partner"].with_user(auth["user"]).browse(int(partner_id)).exists()
        campaign = request.env["cc.campaign"].with_user(auth["user"]).browse(int(campaign_id)).exists()
        if not partner or not campaign or campaign.cc_business_unit_id != unit:
            return self._json(404, {"error": "referenced_record_not_found"})

        def run():
            profile = request.env["cc.customer.profile"].with_user(auth["user"]).create_from_partner(
                partner, campaign, integration_key=payload["integration_key"],
            )
            # name is service-managed (create_from_partner derives it from
            # partner.display_name) -- the model's write() rejects direct
            # changes to it without a governance capability this bridge
            # doesn't hold, so no caller-supplied override is applied here.
            return self._complete(auth, "customer_profile.create", self._customer_profile_value(profile), status=201)

        return self._serialized(auth, run)

    @http.route("/codestra/middleware/v1/customer-profiles/<int:profile_id>", type="http", auth="none", methods=["GET", "PATCH"], csrf=False, readonly=False)
    def customer_profile(self, profile_id):
        operation = "customer_profile.read" if request.httprequest.method == "GET" else "customer_profile.update"
        auth, payload, error = self._begin(operation, allow_event_replay=True, tenant_allowlist_parameter="codestra.crm.tenant_ids", service_user_parameter="codestra.crm.service_user_id")
        if error: return error
        profile = self._customer_profile(auth, profile_id)
        if not profile: return self._json(404, {"error": "customer_profile_not_found"})
        if operation.endswith("update"):
            allowed = {"name", "assigned_user_id", "state"}
            unsupported = sorted(set(payload) - allowed)
            if unsupported: return self._json(422, {"error": "unsupported_fields", "fields": unsupported})
            profile.write(payload)
        return self._complete(auth, operation, self._customer_profile_value(profile))

    # -- Notes (mail.message chatter entries) and tasks (mail.activity
    # to-do items) on a customer profile. These are two distinct standard
    # Odoo concepts: a note has no due date and is never "completed"; a task
    # (activity) has a deadline and a completion action. --

    @http.route("/codestra/middleware/v1/customer-profiles/<int:profile_id>/notes", type="http", auth="none", methods=["GET", "POST"], csrf=False, readonly=False)
    def customer_profile_notes(self, profile_id):
        operation = "note.list" if request.httprequest.method == "GET" else "note.create"
        auth, payload, error = self._begin(operation, allow_event_replay=(operation == "note.list"), tenant_allowlist_parameter="codestra.crm.tenant_ids", service_user_parameter="codestra.crm.service_user_id")
        if error: return error
        profile = self._customer_profile(auth, profile_id)
        if not profile: return self._json(404, {"error": "customer_profile_not_found"})
        if operation == "note.list":
            messages = profile.message_ids.filtered(lambda message: message.message_type == "comment")
            return self._complete(auth, operation, {
                "items": [{"note_id": message.id, "body": message.body, "author_id": message.author_id.id, "created_at": message.date.isoformat()} for message in messages],
            })
        body = str(payload.get("body", "")).strip()
        if not body: return self._json(422, {"error": "body_required"})

        def run():
            message = profile.with_user(auth["user"]).message_post(body=body, message_type="comment", subtype_xmlid="mail.mt_note")
            return self._complete(auth, operation, {"note_id": message.id, "profile_id": profile.id}, status=201)

        return self._serialized(auth, run)

    @http.route("/codestra/middleware/v1/notes/<int:note_id>", type="http", auth="none", methods=["PATCH"], csrf=False, readonly=False)
    def note_update(self, note_id):
        auth, payload, error = self._begin("note.update", tenant_allowlist_parameter="codestra.crm.tenant_ids", service_user_parameter="codestra.crm.service_user_id")
        if error: return error
        unit = self._canonical_unit(auth)
        if not unit: return self._json(403, {"error": "crm_service_scope_rejected"})
        message = request.env["mail.message"].with_user(auth["user"]).browse(note_id).exists()
        if not message or message.model != "cc.customer.profile":
            return self._json(404, {"error": "note_not_found"})
        profile = request.env["cc.customer.profile"].with_user(auth["user"]).browse(message.res_id).exists()
        if not profile or profile.business_unit_id != unit:
            return self._json(404, {"error": "note_not_found"})
        body = payload.get("body")
        if not body: return self._json(422, {"error": "body_required"})
        message.write({"body": body})
        return self._complete(auth, "note.update", {"note_id": message.id, "body": message.body}, profile.partner_id)

    @http.route("/codestra/middleware/v1/customer-profiles/<int:profile_id>/tasks", type="http", auth="none", methods=["GET", "POST"], csrf=False, readonly=False)
    def customer_profile_tasks(self, profile_id):
        operation = "task.list" if request.httprequest.method == "GET" else "task.create"
        auth, payload, error = self._begin(operation, allow_event_replay=(operation == "task.list"), tenant_allowlist_parameter="codestra.crm.tenant_ids", service_user_parameter="codestra.crm.service_user_id")
        if error: return error
        profile = self._customer_profile(auth, profile_id)
        if not profile: return self._json(404, {"error": "customer_profile_not_found"})
        if operation == "task.list":
            activities = request.env["mail.activity"].with_user(auth["user"]).search([
                ("res_model", "=", "cc.customer.profile"), ("res_id", "=", profile.id),
            ])
            return self._complete(auth, operation, {
                "items": [{"task_id": activity.id, "summary": activity.summary, "due_date": activity.date_deadline.isoformat() if activity.date_deadline else None, "user_id": activity.user_id.id} for activity in activities],
            })
        summary = str(payload.get("summary", "")).strip()
        if not summary: return self._json(422, {"error": "summary_required"})
        activity_refs = {"todo": "mail.mail_activity_data_todo", "call": "mail.mail_activity_data_call", "email": "mail.mail_activity_data_email"}
        ref = activity_refs.get(str(payload.get("activity_type", "todo")))
        if not ref: return self._json(422, {"error": "invalid_activity_type"})

        def run():
            activity = request.env["mail.activity"].with_user(auth["user"]).create({
                "activity_type_id": request.env.ref(ref).id, "summary": summary,
                "note": payload.get("note"), "date_deadline": payload.get("due_date") or fields.Date.today(),
                "res_model_id": request.env["ir.model"]._get_id("cc.customer.profile"),
                "res_id": profile.id, "user_id": payload.get("user_id") or auth["user"].id,
            })
            return self._complete(auth, operation, {"task_id": activity.id, "profile_id": profile.id, "status": "scheduled"}, profile.partner_id, status=201)

        return self._serialized(auth, run)

    def _task(self, auth, task_id):
        unit = self._canonical_unit(auth)
        if not unit:
            return None, None
        try:
            activity = request.env["mail.activity"].with_user(auth["user"]).browse(task_id).exists()
            if not activity or activity.res_model != "cc.customer.profile":
                return None, None
            profile = request.env["cc.customer.profile"].with_user(auth["user"]).browse(activity.res_id).exists()
            if not profile or profile.business_unit_id != unit:
                return None, None
            return activity, profile
        except AccessError:
            return None, None

    @http.route("/codestra/middleware/v1/tasks/<int:task_id>", type="http", auth="none", methods=["PATCH"], csrf=False, readonly=False)
    def task_update(self, task_id):
        auth, payload, error = self._begin("task.update", tenant_allowlist_parameter="codestra.crm.tenant_ids", service_user_parameter="codestra.crm.service_user_id")
        if error: return error
        activity, profile = self._task(auth, task_id)
        if not activity: return self._json(404, {"error": "task_not_found"})
        allowed = {"summary", "note", "due_date"}
        values = {}
        if "summary" in payload: values["summary"] = payload["summary"]
        if "note" in payload: values["note"] = payload["note"]
        if "due_date" in payload: values["date_deadline"] = payload["due_date"]
        unsupported = sorted(set(payload) - allowed)
        if unsupported: return self._json(422, {"error": "unsupported_fields", "fields": unsupported})
        activity.write(values)
        return self._complete(auth, "task.update", {"task_id": activity.id, "profile_id": profile.id, "summary": activity.summary}, profile.partner_id)

    @http.route("/codestra/middleware/v1/tasks/<int:task_id>/complete", type="http", auth="none", methods=["POST"], csrf=False, readonly=False)
    def task_complete(self, task_id):
        auth, payload, error = self._begin("task.complete", tenant_allowlist_parameter="codestra.crm.tenant_ids", service_user_parameter="codestra.crm.service_user_id")
        if error: return error
        activity, profile = self._task(auth, task_id)
        if not activity: return self._json(404, {"error": "task_not_found"})

        def run():
            # action_feedback marks the activity done and unlinks it, per
            # standard Odoo mail.activity behavior -- capture what's needed
            # for the response before it disappears.
            result = {"task_id": activity.id, "profile_id": profile.id, "status": "completed"}
            activity.action_feedback(feedback=payload.get("feedback") or None)
            return self._complete(auth, "task.complete", result, profile.partner_id)

        return self._serialized(auth, run)

    # -- Tickets (cc.helpdesk.ticket). SLA due-dates/ticket_number are
    # derived by the model's own create() from queue_id -- this endpoint
    # supplies only the caller-facing fields and trusts that computation. --

    def _ticket(self, auth, ticket_id):
        unit = self._canonical_unit(auth)
        if not unit:
            return None
        try:
            ticket = request.env["cc.helpdesk.ticket"].with_user(auth["user"]).browse(ticket_id).exists()
            if not ticket or ticket.business_unit_id != unit:
                return None
            return ticket
        except AccessError:
            return None

    @staticmethod
    def _ticket_value(ticket):
        return {
            "ticket_id": ticket.id, "ticket_uuid": ticket.ticket_uuid,
            "ticket_number": ticket.ticket_number, "subject": ticket.subject,
            "category": ticket.category, "priority": ticket.priority,
            "severity": ticket.severity, "state": ticket.state,
            "customer_profile_id": ticket.customer_profile_id.id,
            "queue_id": ticket.queue_id.id, "sla_state": ticket.sla_state,
            "opened_at": ticket.opened_at.isoformat() if ticket.opened_at else None,
            "resolution_due_at": ticket.resolution_due_at.isoformat() if ticket.resolution_due_at else None,
        }

    @http.route("/codestra/middleware/v1/tickets", type="http", auth="none", methods=["GET", "POST"], csrf=False, readonly=False)
    def tickets(self):
        if request.httprequest.method == "GET":
            auth, _payload, error = self._begin("ticket.list", allow_event_replay=True, tenant_allowlist_parameter="codestra.crm.tenant_ids", service_user_parameter="codestra.crm.service_user_id")
            if error: return error
            unit = self._canonical_unit(auth)
            if not unit: return self._json(403, {"error": "crm_service_scope_rejected"})
            try:
                limit = min(int(request.httprequest.args.get("limit", 50)), 200)
                offset = max(int(request.httprequest.args.get("offset", 0)), 0)
            except ValueError:
                return self._json(422, {"error": "invalid_pagination"})
            tickets = request.env["cc.helpdesk.ticket"].with_user(auth["user"]).search(
                [("business_unit_id", "=", unit.id)], limit=limit, offset=offset, order="id desc",
            )
            return self._complete(auth, "ticket.list", {
                "items": [self._ticket_value(ticket) for ticket in tickets], "limit": limit, "offset": offset,
            })

        auth, payload, error = self._begin("ticket.create", tenant_allowlist_parameter="codestra.crm.tenant_ids", service_user_parameter="codestra.crm.service_user_id")
        if error: return error
        unit = self._canonical_unit(auth)
        if not unit: return self._json(403, {"error": "crm_service_scope_rejected"})
        required = {"queue_id", "customer_profile_id", "subject", "category", "priority", "severity"}
        missing = sorted(required - set(payload))
        if missing: return self._json(422, {"error": "missing_required_field", "fields": missing})
        queue = request.env["cc.helpdesk.queue"].with_user(auth["user"]).browse(int(payload["queue_id"])).exists()
        profile = self._customer_profile(auth, int(payload["customer_profile_id"]))
        if not queue or queue.business_unit_id != unit or not profile:
            return self._json(404, {"error": "referenced_record_not_found"})

        def run():
            # ticket_uuid/ticket_number/integration_key/sla_policy_id/due-dates
            # are all server-managed by the model's own create() -- passing
            # them here would raise AccessError; only caller-facing fields go in.
            ticket = request.env["cc.helpdesk.ticket"].with_user(auth["user"]).create({
                "queue_id": queue.id, "customer_profile_id": profile.id,
                "subject": payload["subject"], "description": payload.get("description"),
                "category": payload["category"], "priority": payload["priority"],
                "severity": payload["severity"],
            })
            return self._complete(auth, "ticket.create", self._ticket_value(ticket), profile.partner_id, status=201)

        return self._serialized(auth, run)

    @http.route("/codestra/middleware/v1/tickets/<int:ticket_id>", type="http", auth="none", methods=["GET", "PATCH"], csrf=False, readonly=False)
    def ticket(self, ticket_id):
        operation = "ticket.read" if request.httprequest.method == "GET" else "ticket.update"
        auth, payload, error = self._begin(operation, allow_event_replay=True, tenant_allowlist_parameter="codestra.crm.tenant_ids", service_user_parameter="codestra.crm.service_user_id")
        if error: return error
        ticket = self._ticket(auth, ticket_id)
        if not ticket: return self._json(404, {"error": "ticket_not_found"})
        if operation.endswith("update"):
            allowed = {"subject", "description", "priority", "severity", "state", "resolution", "assigned_user_id"}
            unsupported = sorted(set(payload) - allowed)
            if unsupported: return self._json(422, {"error": "unsupported_fields", "fields": unsupported})
            ticket.write(payload)
        return self._complete(auth, operation, self._ticket_value(ticket))
