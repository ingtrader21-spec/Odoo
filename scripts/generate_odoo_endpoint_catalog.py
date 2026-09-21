#!/usr/bin/env python3
"""Generate custom-addons/call_center_campaign/docs/ODOO-ENDPOINT-CATALOG.yaml
from the shared Middleware contract contracts/odoo/campaign-control.v1.json.

    python scripts/generate_odoo_endpoint_catalog.py          # rewrite
    python scripts/generate_odoo_endpoint_catalog.py --check  # CI: fail on drift
"""
import json
import pathlib
import sys

root = pathlib.Path(__file__).resolve().parents[1]
contract = json.loads((root / "contracts/odoo/campaign-control.v1.json").read_text(encoding="utf-8"))
pin = json.loads((root / "contracts/odoo/campaign-control.v1.sha256.json").read_text(encoding="utf-8"))
catalog_path = root / "custom-addons/call_center_campaign/docs/ODOO-ENDPOINT-CATALOG.yaml"
operations_section = """operations:
  - key: odoo.health.live
    method: GET
    path: /health/live
    authentication: private_network
  - key: odoo.health.ready
    method: GET
    path: /health/ready
    scope: monitor.read
  - key: odoo.service.attest
    method: GET
    path: /.well-known/codestra-service
    scope: service.attest
  - key: odoo.metrics.read
    method: GET
    path: /metrics
    scope: metrics.read
"""

SERVED_BY = {
    "outbox.claim": "call_center_campaign",
    "outbox.read": "call_center_campaign",
    "outbox.renew": "call_center_campaign",
    "outbox.acknowledge": "call_center_campaign",
    "outbox.fail": "call_center_campaign",
    "outbox.release": "call_center_campaign",
    "results.create": "call_center_campaign",
    "results.read": "call_center_campaign",
    "results.by_delivery": "call_center_campaign",
    "automation_results.apply": "call_center_campaign",
    "provider_activities.create": "call_center_campaign",
    "campaign.actual_state.write": "codestra_campaign_control_plane",
    "campaigns.read": "codestra_campaign_control_plane",
    "desired_state.read": "codestra_campaign_control_plane",
    "agents.read": "call_center_campaign",
    "leads.read": "call_center_campaign",
    "traces.read": "call_center_campaign",
    "telephony.projections.read": "codestra_telephony_bridge",
    "telephony.mappings.read": "codestra_telephony_bridge",
    "observability.kpis.create": "codestra_observability_integration",
    "observability.incidents.upsert": "codestra_observability_integration",
    "observability.kpis.read": "codestra_observability_integration",
    "observability.incidents.read": "codestra_observability_integration",
    "observability.sync.read": "codestra_observability_integration",
}
UNSERVED = {
    "reconciliation.runs.read": "Odoo serves per-record reconciliation reads (codestra_telephony_bridge /reconciliation/runs/{run_public_id}); the contract collection path has no Odoo implementation.",
    "reconciliation.drifts.read": "Odoo serves per-record reconciliation reads (codestra_telephony_bridge /reconciliation/drifts/{drift_public_id}); the contract collection path has no Odoo implementation.",
    "sales.lookup": "No Odoo addon implements the sales lookup projection.",
    "sales.verification.read": "No Odoo addon implements the sales verification projection.",
}
REQUEST_SCHEMAS = {
    "internal://odoo/automation-results/v1": "contracts/odoo/schemas/automation-results.v1.json",
    "internal://odoo/provider-activities/v1": "contracts/odoo/schemas/provider-activities.v1.json",
    "internal://odoo/campaigns/actual-state/v1": "contracts/odoo/schemas/campaigns-actual-state.v1.json",
    "internal://odoo/campaigns/read/v1": "contracts/odoo/schemas/campaigns-read.v1.json",
    "internal://odoo/desired-state/read/v1": "contracts/odoo/schemas/desired-state-read.v1.json",
}

lines = [
    'schema_version: "1.0"',
    "service_key: odoo",
    "api_version: v1",
    "base_url_key: odoo.internal",
    "production_activation: false",
    "",
    "# Middleware-facing operations. This section is generated from the shared",
    "# contract contracts/odoo/campaign-control.v1.json (Middleware owns the file;",
    "# Odoo vendors a byte-identical copy). Key, method, path and scope must equal",
    "# the contract; tests in codestra_campaign_control_plane enforce parity.",
    "shared_contract:",
    f"  file: {pin['contract']}",
    f"  canonical_sha256: {pin['canonical_sha256']}",
    f"  file_sha256: {pin['file_sha256']}",
    f"  hash_rule: \"{contract['hash_rule']}\"",
    "  middleware_pin: Middleware- migrations/versions/0066_reconcile_odoo_campaign_scope.py CATALOG_SHA256",
    "  request_schemas:",
]
for schema_id, path in REQUEST_SCHEMAS.items():
    lines.append(f"    {schema_id}: {path}")
lines.append("  unserved_operations:")
for key, reason in UNSERVED.items():
    lines.append(f"    - key: {key}")
    lines.append(f"      endpoint_key: {contract['operations'][key]['endpoint_key']}")
    lines.append(f"      reason: \"{reason}\"")
lines.append("")
lines.append("endpoints:")
for key, op in contract["operations"].items():
    if key in UNSERVED:
        continue
    assert key in SERVED_BY, key
    lines.append(f"  - key: {op['endpoint_key']}")
    lines.append(f"    operation: {key}")
    lines.append(f"    method: {op['method']}")
    lines.append(f"    path: {op['path']}")
    lines.append(f"    scope: {op['scope']}")
    lines.append(f"    mutation: {'true' if op['kind'] == 'write' else 'false'}")
    lines.append(f"    served_by: {SERVED_BY[key]}")
    if op.get("request_schema"):
        lines.append(f"    request_schema: {op['request_schema']}")
    if op.get("response_binding"):
        lines.append("    response_binding:")
        for name in op["response_binding"]:
            lines.append(f"      - {name}")
    if op.get("receipt_field"):
        lines.append(f"    receipt_field: {op['receipt_field']}")
    if op.get("response_fields"):
        lines.append("    response_fields:")
        for name in op["response_fields"]:
            lines.append(f"      - {name}")
    if op.get("conflict_error_class"):
        lines.append(f"    conflict_status: {op['conflict_status']}")
        lines.append(f"    conflict_error_class: {op['conflict_error_class']}")
lines.append("")
lines.append("# Administrative platform client only (odoo.campaign.control.write is never")
lines.append("# issued to Middleware). These originate lifecycle commands; provider")
lines.append("# execution still travels Odoo outbox -> Middleware claim -> read-back.")
lines.append("platform_admin_endpoints:")
admin = [
    ("odoo.platform.campaigns.create", "POST", "/platform/v1/campaigns", "odoo.campaign.control.write", True, ["name", "code", "business_unit_code"]),
    ("odoo.platform.campaigns.read", "GET", "/platform/v1/campaigns/{campaign_public_id}", "odoo.campaign.control.read", False, None),
]
for command in ("validate", "provision", "synthetic_test", "activate", "disable", "reconcile", "rollback"):
    required = ["audit_reason", "expected_configuration_version"]
    if command == "rollback":
        required.append("target_configuration_version")
    admin.append((f"odoo.platform.campaigns.{command}", "POST", f"/platform/v1/campaigns/{{campaign_public_id}}/{command.replace('_', '-')}", "odoo.campaign.control.write", True, required))
admin += [
    ("odoo.platform.campaigns.actual_state.read", "GET", "/platform/v1/campaigns/{campaign_public_id}/actual-state", "odoo.campaign.control.read", False, None),
    ("odoo.platform.campaigns.provisioning_runs.read", "GET", "/platform/v1/campaigns/{campaign_public_id}/provisioning-runs", "odoo.campaign.control.read", False, None),
]
for key, method, path, scope, mutation, required in admin:
    lines.append(f"  - key: {key}")
    lines.append(f"    method: {method}")
    lines.append(f"    path: {path}")
    lines.append(f"    scope: {scope}")
    lines.append(f"    mutation: {'true' if mutation else 'false'}")
    lines.append("    served_by: codestra_campaign_control_plane")
    if required:
        lines.append("    required_fields:")
        for name in required:
            lines.append(f"      - {name}")
lines.append("")
lines.append("# Odoo-only routes outside the shared contract. Middleware calls the")
lines.append("# capabilities document at its fixed path; the rest are operator, audit and")
lines.append("# compatibility reads and are not registry-routed operations.")
lines.append("odoo_only_endpoints:")
odoo_only = [
    ("odoo.capabilities.read", "GET", "/api/v1/integration/capabilities", "service.attest", False, "call_center_campaign"),
    ("odoo.results.reconcile", "POST", "/api/v1/integration/results/{result_public_id}/reconcile", "odoo.integration.results.reconcile", True, "call_center_campaign"),
    ("odoo.traces.by_record", "GET", "/api/v1/integration/traces?model={model}&res_id={id}", "odoo.integration.read", False, "call_center_campaign"),
    ("odoo.audit.read", "GET", "/api/v1/integration/audit/{audit_id}", "odoo.integration.audit.read", False, "call_center_campaign"),
    ("odoo.desired_state.aggregate.read", "GET", "/api/v1/integration/desired-state/{aggregate_type}/{public_id}", "odoo.integration.read", False, "call_center_campaign"),
    ("odoo.agents.read.by_path", "GET", "/api/v1/integration/agents/{agent_public_id}", "odoo.integration.read", False, "call_center_campaign"),
    ("odoo.leads.read.by_path", "GET", "/api/v1/integration/leads/{lead_public_id}", "odoo.integration.read", False, "call_center_campaign"),
    ("odoo.campaigns.legacy.read", "GET", "/api/v1/integration/campaigns/{campaign_public_id}", "odoo.integration.read", False, "call_center_campaign"),
    ("odoo.outbox.renew.legacy_path", "POST", "/api/v1/integration/outbox/{event_id}/lease/renew", "odoo.integration.outbox.renew", True, "call_center_campaign"),
    ("odoo.outbox.release.legacy_path", "POST", "/api/v1/integration/outbox/{event_id}/release", "odoo.integration.outbox.release", True, "call_center_campaign"),
    ("odoo.results.by_delivery.query", "GET", "/api/v1/integration/results?delivery_id={delivery_id}", "odoo.integration.results.read", False, "call_center_campaign"),
]
for key, method, path, scope, mutation, served_by in odoo_only:
    lines.append(f"  - key: {key}")
    lines.append(f"    method: {method}")
    lines.append(f"    path: {path}")
    lines.append(f"    scope: {scope}")
    lines.append(f"    mutation: {'true' if mutation else 'false'}")
    lines.append(f"    served_by: {served_by}")
lines.append("")
lines.append(operations_section.rstrip("\n"))
lines.append("")
lines.append("campaign_control:")
lines.append("  delivery: middleware_pull")
lines.append("  outbox_command_id: event_uuid")
lines.append("  outbox_event_allowlist:")
for event_type in contract["outbox_events"]["allowlist"]:
    lines.append(f"    - {event_type}")
lines.append("  outbox_payload_required_fields:")
for name in contract["outbox_events"]["payload_required_fields"]:
    lines.append(f"    - {name}")
lines.append("  effective_states:")
for state in contract["effective_states"]:
    lines.append(f"    - {state}")
lines.append("  control_statuses:")
for status in ("draft", "validated", "blocked"):
    lines.append(f"    - {status}")
lines.append("  run_statuses:")
for status in ("pending", "succeeded", "failed"):
    lines.append(f"    - {status}")
lines.append("  run_delivery_states:")
for status in ("none", "queued", "accepted", "dead_letter"):
    lines.append(f"    - {status}")
lines.append("  concurrency_field: expected_configuration_version")
lines.append("  readback_required_fields:")
for name in contract["actual_state_readback"]["required_fields"]:
    lines.append(f"    - {name}")
lines.append("  conflict_codes:")
for code in (
    "STALE_CONFIGURATION_VERSION",
    "IDEMPOTENCY_CONFLICT",
    "CONFIGURATION_VERSION_CONFLICT",
    "INVALID_STATE_TRANSITION",
    "PROVISIONING_RUN_PENDING",
    "IDEMPOTENCY_KEY_REUSED",
    "EXTERNAL_EFFECTS_DISABLED",
    "CONFIGURATION_NOT_VALIDATED",
    "ROLLBACK_TARGET_IS_CURRENT",
    "ROLLBACK_TARGET_BLOCKED",
):
    lines.append(f"    - {code}")
lines.append("  scopes:")
for scope in ("odoo.campaign.control.read", "odoo.campaign.actual_state.write", "odoo.campaign.control.write"):
    lines.append(f"    - {scope}")
lines.append("  middleware_scopes:")
for scope in ("odoo.campaign.control.read", "odoo.campaign.actual_state.write"):
    lines.append(f"    - {scope}")
lines.append("")
lines.append("webhook_boundary:")
lines.append("  provider_webhooks_accepted: false")
lines.append("  normalized_result_ingress: odoo.results.create")
lines.append("  retired_routes:")
lines.append("    - path: /codestra/integration/v1/results")
lines.append("      response_status: 410")
lines.append("    - path: /api/v1/integration/campaign-actions")
lines.append("      operation: campaign_actions.apply")
lines.append("      response_status: 410")
lines.append("      replaced_by: automation_results.apply")
lines.append("      advertised: false")
rendered = "\n".join(lines) + "\n"
if "--check" in sys.argv[1:]:
    current = catalog_path.read_text(encoding="utf-8") if catalog_path.exists() else ""
    if current != rendered:
        print("ODOO_ENDPOINT_CATALOG=DRIFT (rerun scripts/generate_odoo_endpoint_catalog.py)")
        raise SystemExit(1)
    print("ODOO_ENDPOINT_CATALOG=PASS")
else:
    catalog_path.write_text(rendered, encoding="utf-8", newline="\n")
    print("catalog regenerated:", len(lines), "lines")
