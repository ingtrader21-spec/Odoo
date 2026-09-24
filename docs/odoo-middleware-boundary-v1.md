# Odoo → Middleware boundary v1

Mission 1 consolidates the existing HTTP clients, outbox/inbox records, callbacks, results,
traces, desired-state/readback and idempotency mechanisms into **one canonical boundary**. Odoo
expresses *command intent*; Middleware (`middleware-integration-api:8095`, behind Caddy → Kong)
executes, authorises, de-duplicates, audits and reconciles. Nothing in Odoo opens a provider
connection (`config/odoo-provider-paths.v1.json`, enforced by
`scripts/validate_mission1_foundation.py`). This document complements
`docs/INTEGRATION-BOUNDARY.md` (inbound writes: only Middleware may write) with the outbound
contract.

## Canonical objects

| Concept | Canonical object | Module | Notes |
| --- | --- | --- | --- |
| command request | `codestra.runtime.integration.outbox` row (campaign/control commands) — event types `campaign.<operation>.requested.v1`; per-channel intent queues `codestra.sms.outbox`, `codestra.email.outbox`, `cc.identity.outbox`, `cc.operation.outbox`, `codestra.lead.import.outbox` | `call_center_campaign` (+ channel modules) | one row per business intent; pull-only: Middleware claims work through `/api/v1/integration/outbox/*` (claim/releases/renewals) |
| command ID | `command_id` on control-plane runs and readbacks; `event_uuid` on the outbox row | `codestra_campaign_control_plane`, `call_center_campaign` | assigned by Odoo at intent time, echoed by every result/readback |
| idempotency key | `idempotency_key` (required, indexed, immutable) on every outbox row, provisioning run and readback; Middleware persists it and answers a replay with the original receipt | all queues | reuse across different commands is a conflict (`test_idempotency_key_reuse_across_commands_conflicts`) |
| correlation ID | `correlation_id` (required, indexed) on outbox rows, traces (`codestra.integration.trace`), readbacks and Middleware requests; `causation_id` links a result to the command it answers | all queues | propagated as `X-Correlation-ID` on every HTTP exchange; never personal data |
| timeout | transport level: bounded `urlopen(..., timeout=…)` in every client (`codestra_middleware_bridge`, `codestra_email_middleware`, `codestra_sms_middleware`, `codestra_identity_provisioning`, `codestra_vicidial_crm.middleware_client`); queue level: `next_attempt_at` and claim renewals (`/renewals`) with a lease that expires back to `pending` | clients / outbox | a timed-out claim is released, never silently completed |
| retry classification | `retry_count`, `next_attempt_at`, `delivery_state ∈ {pending, processing, delivered, failed, dead_letter, superseded}`; transient failures re-schedule (`+60 s` backoff), terminal failures `dead_letter`; a newer configuration version `supersedes` an unsent command | `codestra.runtime.integration.outbox` | local configuration failures are deferred without consuming delivery retries |
| result | `codestra.integration.result.inbox` (results by delivery, `GET /api/v1/integration/results/by-delivery/{id}`), `codestra.automation.action.receipt` for automation results, `cc.campaign.readback` for control commands | `call_center_campaign`, `codestra_campaign_control_plane` | results are written by the verified Middleware service identity only (`MIDDLEWARE_ONLY` routes) |
| failure | run `status=failed` with `control_blocked_reason_json`; outbox `failed`/`dead_letter`; dead-letter before intake fails the run and blocks further state commands until reconciled | control plane / outbox | a failure never leaves a partially applied business state (`test_a_failing_action_rolls_back_the_whole_result`) |
| callback | Middleware → Odoo callbacks are `MIDDLEWARE_ONLY` controller routes (`/api/v1/integration/*`, `/codestra/middleware/v1/*`, calling readback) with controller-verified JWT/HMAC identity, nonce replay guard (`codestra.integration.callback.nonce`, bounded purge cron) and `Idempotency-Key`; Odoo → Middleware callbacks for appointments use the same client boundary | controllers / `codestra_appointments` | no `auth="user"` route accepts machine callbacks |
| reconciliation | `reconcile` control command (concurrent-safe, no state change), `GET /api/v1/integration/campaigns/actual-state` write-back, telephony reconciliation runs/drifts in `codestra_telephony_bridge`, identity readback matching in `cc.identity.outbox` (`readback_matched`/`mismatch`) | control plane / bridges | "pending lag is not drift"; divergence observed by a readback is `drift`, never auto-corrected without a new command |

## Request envelope

Every command carries: `event_uuid`/`command_id`, `idempotency_key`, `correlation_id`,
`causation_id`, `organization_public_id`, `business_unit_public_id`, `campaign_public_id`,
`configuration_version`, `operation`, `manifest_ref`/`manifest_hash`, `observed_at` (readback),
`attempt`. The readback contract (`READBACK_REQUIRED_FIELDS` in
`codestra_campaign_control_plane`) rejects any receipt missing one of them.

## What Odoo does not do

- It does not authorise commands cross-system (Middleware `POST /v2/automation/commands`,
  `platform/v1/*` routes hold the authorization and audit).
- It does not call VICIdial, Klyrow, Telnexa, SMTP or any PBX (no such transport in source).
- It does not retry a command by re-issuing a new `idempotency_key`.
- It does not mark a run succeeded from an HTTP 202: only a readback with the required fields
  advances `effective_state`.

## Enforcement

- `scripts/validate_integration_boundary.py` — inbound: only Middleware writes through the
  approved service API / ORM bridge.
- `scripts/validate_mission1_foundation.py` — outbound: every outbound HTTP site classified as
  `MIDDLEWARE_CLIENT`, `IDENTITY_PROVIDER` (Keycloak) or `READ_ONLY_PROJECTION`; forbidden
  transports rejected; controller inventory fresh and fully governed.
- `tests/security/test_mission1_foundation.py` — negative attempts (direct provider transport,
  unclassified endpoint, duplicate authority, invalid transition) must fail.
