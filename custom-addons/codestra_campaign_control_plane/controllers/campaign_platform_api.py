"""Campaign control routes.

Two audiences, two prefixes:

* ``/api/v1/integration/...`` — the Middleware-facing operations pinned by
  ``contracts/odoo/campaign-control.v1.json`` (``campaign.actual_state.write``,
  ``campaigns.read``, ``desired_state.read``). Scope, path, request schema and
  response binding are exact; identifiers are public IDs only.
* ``/platform/v1/campaigns...`` — the separately authorized administrative
  platform client that originates lifecycle commands
  (``odoo.campaign.control.write``). Middleware never holds that scope.

Every route authenticates through the shared integration helpers and
delegates to the ``cc.campaign`` lifecycle service; no lifecycle logic lives
here. The retired ``/api/v1/integration/campaign-actions`` path answers 410
from the base module and is not advertised.
"""

from odoo import http
from odoo.http import request

from odoo.addons.call_center_campaign.controllers.integration_api import (
    CodestraIntegrationApiController,
    IntegrationConflict,
    IntegrationNotFound,
    _assert_organization_scope,
    _assert_scope,
    _authenticate,
    _body,
    _capability_state,
    _handle_errors,
    _json_response,
    register_conflict_constraints,
)

from ..models.campaign_lifecycle import (
    COMMANDS,
    ENVIRONMENT_CODES,
    READBACK_REQUIRED_FIELDS,
    CampaignControlConflict,
)

SCOPE_CONTROL_READ = "odoo.campaign.control.read"
SCOPE_CONTROL_WRITE = "odoo.campaign.control.write"
SCOPE_ACTUAL_STATE_WRITE = "odoo.campaign.actual_state.write"
CANONICAL_PREFIX = "/platform/v1/campaigns"
INTEGRATION_ROUTES = {
    "campaign.actual_state.write": "/api/v1/integration/campaigns/actual-state",
    "campaigns.read": "/api/v1/integration/campaigns/read",
    "desired_state.read": "/api/v1/integration/desired-state/read",
}
SCOPE_TRIPLE = ("organization_public_id", "business_unit_public_id", "campaign_public_id")
CONTRACT_CAPABILITIES = list(INTEGRATION_ROUTES)

register_conflict_constraints(
    "cc_campaign_provisioning_run_idempotency_unique",
    "cc_campaign_provisioning_run_event_uuid_unique",
    "cc_campaign_readback_readback_idempotency_unique",
)


def _domain(operation):
    """Translate deterministic lifecycle conflicts into HTTP 409."""
    try:
        return operation()
    except CampaignControlConflict as exc:
        raise IntegrationConflict(exc.code) from exc


def _scoped_campaign(claims, body):
    """Resolve the contract scope triple inside the caller's claims."""
    for name in SCOPE_TRIPLE:
        if not isinstance(body.get(name), str) or not body[name]:
            raise ValueError(f"FIELD_REQUIRED:{name}")
    _assert_organization_scope(claims, body["organization_public_id"])
    campaign = request.env["cc.campaign"].control_lookup_scoped(
        body["organization_public_id"],
        body["business_unit_public_id"],
        body["campaign_public_id"],
    )
    if not campaign:
        raise IntegrationNotFound("campaign not found")
    _assert_scope(
        claims,
        campaign._control_environment(),
        campaign.cc_business_unit_id.code,
        campaign.code,
    )
    return campaign


def _platform_campaign(public_id, claims):
    campaign = request.env["cc.campaign"].control_lookup(public_id)
    if not campaign:
        raise IntegrationNotFound("campaign not found")
    _assert_scope(
        claims,
        campaign._control_environment(),
        campaign.cc_business_unit_id.code,
        campaign.code,
    )
    return campaign


def _command_response(campaign, run, replayed):
    status = 200 if run.status in {"succeeded", "failed"} else 202
    return _json_response(
        {
            "status": "REPLAYED" if replayed else "ACCEPTED",
            "replayed": replayed,
            "campaign": campaign.control_document(),
            "run": run.control_document(),
            "command_id": run.event_uuid or None,
            "idempotency_key": run.idempotency_key,
        },
        status,
    )


class CampaignIntegrationController(http.Controller):
    """Middleware-facing campaign-control operations (contract revision 0066)."""

    @http.route(INTEGRATION_ROUTES["campaign.actual_state.write"], type="http", auth="none", methods=["POST"], csrf=False, save_session=False)
    def write_actual_state(self):
        def operation():
            claims, body, _ = _body(SCOPE_ACTUAL_STATE_WRITE, set(READBACK_REQUIRED_FIELDS))
            # _body already rejects a body idempotency_key that differs from the
            # header; the contract additionally requires the body value itself.
            if body["idempotency_key"] != request.httprequest.headers.get("Idempotency-Key"):
                raise IntegrationConflict("IDEMPOTENCY_CONFLICT")
            campaign = _scoped_campaign(claims, body)
            receipt = _domain(lambda: campaign.control_readback(body))
            return _json_response(receipt)

        return _handle_errors(operation)

    @http.route(INTEGRATION_ROUTES["campaigns.read"], type="http", auth="none", methods=["POST"], csrf=False, save_session=False)
    def read_campaign(self):
        def operation():
            claims, body, _ = _body(SCOPE_CONTROL_READ, set(SCOPE_TRIPLE))
            if set(body) - set(SCOPE_TRIPLE):
                raise ValueError("UNKNOWN_FIELDS:" + ",".join(sorted(set(body) - set(SCOPE_TRIPLE))))
            campaign = _scoped_campaign(claims, body)
            return _json_response(campaign.contract_read_document())

        return _handle_errors(operation)

    @http.route(INTEGRATION_ROUTES["desired_state.read"], type="http", auth="none", methods=["POST"], csrf=False, save_session=False)
    def read_desired_state(self):
        def operation():
            claims, body, _ = _body(SCOPE_CONTROL_READ, set(SCOPE_TRIPLE))
            if set(body) - set(SCOPE_TRIPLE):
                raise ValueError("UNKNOWN_FIELDS:" + ",".join(sorted(set(body) - set(SCOPE_TRIPLE))))
            campaign = _scoped_campaign(claims, body)
            return _json_response(campaign.contract_desired_state_document())

        return _handle_errors(operation)


class CampaignPlatformController(http.Controller):
    """Administrative platform client: originates lifecycle commands."""

    def _command(self, public_id, command):
        def operation():
            claims, body, _ = _body(SCOPE_CONTROL_WRITE)
            campaign = _platform_campaign(public_id, claims)
            capabilities = _capability_state()
            run, replayed = _domain(
                lambda: campaign.control_command(
                    command,
                    idempotency_key=request.httprequest.headers.get("Idempotency-Key"),
                    audit_reason=body.get("audit_reason"),
                    expected_configuration_version=body.get("expected_configuration_version"),
                    correlation_id=request.httprequest.headers["X-Codestra-Correlation-ID"],
                    causation_id=request.httprequest.headers.get("X-Codestra-Causation-ID"),
                    target_configuration_version=body.get("target_configuration_version"),
                    external_effects_enabled=(
                        capabilities["business_writes_enabled"]
                        and capabilities["external_delivery_enabled"]
                    ),
                )
            )
            return _command_response(campaign, run, replayed)

        return _handle_errors(operation)

    @http.route(CANONICAL_PREFIX, type="http", auth="none", methods=["POST"], csrf=False, save_session=False)
    def create_campaign(self):
        def operation():
            claims, body, _ = _body(SCOPE_CONTROL_WRITE, {"name", "code"})
            Campaign = request.env["cc.campaign"]
            unit, values = Campaign.control_create_values(
                body, idempotency_key=request.httprequest.headers.get("Idempotency-Key")
            )
            _assert_scope(claims, ENVIRONMENT_CODES[values["environment"]], unit.code)
            campaign, created = Campaign.control_create(values)
            return _json_response(
                {
                    "status": "CREATED" if created else "REPLAYED",
                    "replayed": not created,
                    "campaign": campaign.control_document(),
                },
                201 if created else 200,
            )

        return _handle_errors(operation)

    @http.route(f"{CANONICAL_PREFIX}/<string:public_id>", type="http", auth="none", methods=["GET"], csrf=False)
    def read_campaign(self, public_id):
        def operation():
            claims, _ = _authenticate(b"", SCOPE_CONTROL_READ)
            campaign = _platform_campaign(public_id, claims)
            return _json_response({"campaign": campaign.control_document()})

        return _handle_errors(operation)

    @http.route(f"{CANONICAL_PREFIX}/<string:public_id>/validate", type="http", auth="none", methods=["POST"], csrf=False, save_session=False)
    def validate_campaign(self, public_id):
        return self._command(public_id, "validate")

    @http.route(f"{CANONICAL_PREFIX}/<string:public_id>/provision", type="http", auth="none", methods=["POST"], csrf=False, save_session=False)
    def provision_campaign(self, public_id):
        return self._command(public_id, "provision")

    @http.route(f"{CANONICAL_PREFIX}/<string:public_id>/synthetic-test", type="http", auth="none", methods=["POST"], csrf=False, save_session=False)
    def synthetic_test_campaign(self, public_id):
        return self._command(public_id, "synthetic_test")

    @http.route(f"{CANONICAL_PREFIX}/<string:public_id>/activate", type="http", auth="none", methods=["POST"], csrf=False, save_session=False)
    def activate_campaign(self, public_id):
        return self._command(public_id, "activate")

    @http.route(f"{CANONICAL_PREFIX}/<string:public_id>/disable", type="http", auth="none", methods=["POST"], csrf=False, save_session=False)
    def disable_campaign(self, public_id):
        return self._command(public_id, "disable")

    @http.route(f"{CANONICAL_PREFIX}/<string:public_id>/reconcile", type="http", auth="none", methods=["POST"], csrf=False, save_session=False)
    def reconcile_campaign(self, public_id):
        return self._command(public_id, "reconcile")

    @http.route(f"{CANONICAL_PREFIX}/<string:public_id>/rollback", type="http", auth="none", methods=["POST"], csrf=False, save_session=False)
    def rollback_campaign(self, public_id):
        return self._command(public_id, "rollback")

    @http.route(f"{CANONICAL_PREFIX}/<string:public_id>/actual-state", type="http", auth="none", methods=["GET"], csrf=False)
    def read_actual_state(self, public_id):
        def operation():
            claims, _ = _authenticate(b"", SCOPE_CONTROL_READ)
            campaign = _platform_campaign(public_id, claims)
            document = campaign.control_document()
            return _json_response(
                {
                    key: document[key]
                    for key in (
                        "campaign_public_id",
                        "campaign_uuid",
                        "control_status",
                        "desired_state",
                        "effective_state",
                        "configuration_version",
                        "manifest_ref",
                        "manifest_hash",
                        "pending_run_ids",
                        "blocked_reason",
                    )
                }
            )

        return _handle_errors(operation)

    @http.route(f"{CANONICAL_PREFIX}/<string:public_id>/provisioning-runs", type="http", auth="none", methods=["GET"], csrf=False)
    def provisioning_runs(self, public_id):
        def operation():
            claims, _ = _authenticate(b"", SCOPE_CONTROL_READ)
            campaign = _platform_campaign(public_id, claims)
            return _json_response(
                {
                    "campaign_public_id": campaign.code,
                    "campaign_uuid": campaign.workspace_uuid,
                    "runs": [run.control_document() for run in campaign.provisioning_run_ids],
                    "readbacks": [
                        readback.receipt() for readback in campaign.readback_ids
                    ],
                }
            )

        return _handle_errors(operation)


class CampaignPlatformCapabilities(CodestraIntegrationApiController):
    """Advertise the contract operations this module serves."""

    def _capability_keys(self):
        return super()._capability_keys() + CONTRACT_CAPABILITIES


COMMAND_ROUTES = {command: command.replace("_", "-") for command in COMMANDS}
