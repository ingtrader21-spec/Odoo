# Domain ownership v1

Source of truth: `config/odoo-domain-ownership.v1.json`. Ownership is architectural, not physical: compatibility
models keep their tables and external identifiers, but exactly one model per domain is the authority, and a
compatibility/transitional/retire-candidate module can never hold a canonical owner or a runtime-control entry point.

Odoo owns business state and command intent; Middleware owns cross-system command execution, authorization, idempotency, execution audit and reconciliation; Keycloak owns identity; Kong owns gateway prerequisites; Caddy owns ingress/TLS; n8n owns approved automation execution.

## campaign

- **canonical owner**: `cc.campaign` in `codestra_cc_core` — campaign identity, business unit/company scope, lifecycle_state, channel configuration, identifier status
- **storage record**: `call.center.campaign` in `call_center_campaign` — physical campaign row delegated through _inherits; legacy state (draft/approved/active/paused/closed) is derived, not a competing lifecycle
- **runtime control**: `cc.campaign` in `codestra_campaign_control_plane` — the only object that may request provisioning, synthetic test, activation, disable, reconcile or rollback; delivered pull-only through the Middleware contract
- **compatibility models** (may remain, never competing authorities): `codestra.campaign.workflow`, `codestra.campaign.status`, `codestra.campaign.automation`, `codestra.campaign.action.service`, `codestra.campaign.playbook`
- **compatibility modules**: `codestra_campaign_crm_os`, `codestra_cc_campaign`, `codestra_staging_campaign_design`, `codestra_campaign_publishing`
- **rules**:
  - No model other than cc.campaign may hold a second campaign lifecycle authority.
  - call.center.campaign automatic design provisioning stays a compatibility path: its outbox cron ships inactive and is not loaded by the manifest.
  - codestra.campaign.* objects in codestra_campaign_crm_os describe workflow/knowledge/KPI configuration only; they never call Middleware directly.

## agent

- **canonical owner**: `codestra.agent.onboarding` in `codestra_agent_onboarding` — agent business lifecycle: Odoo user, company/business unit, campaign, supervisor, role, WebRTC session, channel entitlements, Middleware provisioning identity
- **identity binding**: `codestra.identity.link` in `codestra_identity_provisioning` — binding of the Odoo user to the Keycloak subject; Keycloak remains the identity database
- **compatibility models** (may remain, never competing authorities): `codestra.agent.profile`, `codestra.agent.performance`, `codestra.agent.state.event`, `codestra.vicidial.agent`
- **compatibility modules**: `codestra_campaign_crm_os`, `codestra_vicidial_crm`
- **rules**:
  - codestra.vicidial.agent is a telephony presence projection and never provisions or authenticates an agent.
  - codestra.agent.profile is a CRM-OS performance profile; membership and roles resolve through cc.campaign.membership.

## identity

- **canonical owner**: `codestra.identity.link` in `codestra_identity_provisioning` — Keycloak subject <-> Odoo user link, platform user, tenant membership and credential references (references only, never secrets)
- **propagation**: `cc.identity.outbox` in `codestra_cc_identity` — identity intent propagated to Middleware with readback matching
- **compatibility models** (may remain, never competing authorities): `vicidial.identity.map`, `codestra.intake.identity`, `cc.mail.sender.identity`
- **compatibility modules**: `codestra_vicidial_crm`, `codestra_intake_leads`
- **rules**:
  - Odoo never stores Keycloak credentials or acts as an identity provider; SSO routes in codestra_orbit_theme only redirect to and consume Keycloak.

## call

- **canonical owner**: `cc.operation.outbox` in `codestra_cc_calls` — call operations (callbacks, appointments, transfers, referrals) expressed as Middleware-bound operations
- **projection**: `codestra.vicidial.call` in `codestra_vicidial_crm` — read-only telephony projection of calls, events and recordings received from Middleware
- **compatibility models** (may remain, never competing authorities): `codestra.call.event`, `call.center.callback.task`, `call.center.callback.notification`, `codestra.callback`
- **compatibility modules**: `codestra_campaign_crm_os`, `call_center_orchestration`, `codestra_appointments`
- **rules**:
  - A call is never placed, transferred or recorded by Odoo directly; the calling contract pin governs every telephony readback route.

## channel

- **canonical owner**: `codestra.agent.channel` in `codestra_identity_provisioning` — agent channel entitlements (phone/webrtc/email/sms) with a deterministic state
- **campaign side**: `cc.campaign.channel` in `codestra_cc_core` — campaign channel configuration
- **compatibility models** (may remain, never competing authorities): `codestra.lead.channel.eligibility`, `codestra.social.channel`
- **compatibility modules**: `codestra_lead_automation`, `codestra_social_orchestration`
- **rules**:
  - Channel entitlements are granted through provisioning steps only.

## inbox_outbox

- **canonical owner**: `codestra.runtime.integration.outbox` in `call_center_campaign` — the Middleware command outbox (claim/release/renew/dead-letter) and codestra.integration.result.inbox for results; traces and nonces live beside it
- **channel queues**: `codestra.sms.outbox`, `codestra.email.outbox`, `cc.identity.outbox`, `cc.operation.outbox`, `codestra.lead.import.outbox`
- **compatibility models** (may remain, never competing authorities): `codestra.crm.outbox`, `codestra.integration.delivery`, `codestra.integration.event`, `codestra.integration.idempotency`
- **compatibility modules**: `codestra_campaign_crm_os`, `codestra_integration_hub`
- **rules**:
  - Every channel queue delivers through the Middleware boundary; none opens a provider connection.
  - codestra_integration_hub delivery/idempotency is a retire candidate and may not gain new callers.

## provisioning

- **canonical owner**: `codestra.provisioning.request` in `codestra_identity_provisioning` — agent/user provisioning state machine with steps, reservations, verification, activation, suspension and termination
- **campaign side**: `cc.campaign.provisioning.run` in `codestra_campaign_control_plane` — campaign provisioning runs and readback
- **compatibility models** (may remain, never competing authorities): `call.center.provisioning.request`, `codestra.agent.provisioning.middleware.client`, `codestra.middleware.agent.provisioning.transport`
- **compatibility modules**: `call_center_orchestration`
- **rules**:
  - call.center.provisioning.request is a compatibility record and never dispatches to Middleware on its own.

## Canonical campaign model

| Aspect | Where it lives |
| --- | --- |
| campaign identity | `cc.campaign` (`public_id`, `code`, `identifier_status` ∈ canonical/legacy_exception) |
| business unit / company | `cc.campaign.cc_business_unit_id` → `cc.business.unit` → `call.center.business.unit` (company) |
| supervisor / agent memberships | `cc.campaign.membership` (`codestra_cc_security`), exactly one active operational membership per agent |
| CRM ownership | `codestra_cc_crm` service group and lead record rules |
| channel configuration | `cc.campaign.channel`, `cc.campaign.policy` |
| provisioning state | `cc.campaign.provisioning.run` (control plane) |
| integration state | `codestra.runtime.integration.outbox` / `cc.campaign.readback` |
| activation state | `cc.campaign.effective_state` ∈ unknown, absent, provisioned_disabled, synthetic_tested, active, disabled, advanced only by readback |
| lifecycle | `cc.campaign.lifecycle_state` ∈ draft, design_pending, design_ready, approval_pending, approved, provisioning, provisioned_disabled, testing, staging_ready, activation_pending, active, blocked, failed, rollback_pending, rolled_back, archived with `SAFE_LIFECYCLE_TRANSITIONS` |
| compatibility identifiers | `legacy_campaign_id` → `call.center.campaign`, `call.center.campaign.mapping`, external ids preserved |

No duplicate campaign object independently controls runtime provisioning: the legacy design outbox cron ships inactive
and unloaded, `codestra.campaign.*` objects never call Middleware, and the gate enforces both.

## Canonical agent model

| Link | Where it lives |
| --- | --- |
| Odoo user | `codestra.agent.onboarding.user_id` / `codestra.platform.user` |
| agent | `codestra.agent.onboarding` (state ∈ draft, in_review, approved, provisioning, active, offboarding, closed, failed, cancelled) |
| company / business unit | business-unit mixin on the onboarding record |
| campaign | `cc.campaign.membership` |
| supervisor | membership role `supervisor` |
| role | canonical role → Odoo group mapping (`config/odoo-role-foundation.v1.json`) |
| extension | `codestra.extension.assignment` (reserved/committed/released/expired) from `codestra.extension.pool` |
| WebRTC state | `cc.webrtc.session` |
| channel entitlements | `codestra.agent.channel` (state ∈ requested, provisioning, provisioned, effective, revoked, failed) |
| Keycloak subject | `codestra.identity.link` (state ∈ pending, active, suspended, terminated); Keycloak is the identity database |
| Middleware provisioning identity | `codestra.provisioning.request` and its steps, delivered through the canonical Middleware boundary |
