# Codestra Application Integration Plane

**Repository:** `appolon1908-hue/Odoo`  
**Status:** Accepted repository design specification  
**Scope:** Odoo-owned implementation only  
**Release posture:** Staging-only until the certification gates pass

**Superseding authority note:** the locked specification
`docs/superpowers/specs/2026-09-15-odoo-unified-campaign-control-plane-design.md`
(committed to `design/odoo-unified-campaign-control-plane-20260915`) is binding
where it conflicts with this document. In particular, that specification
prohibits an Odoo-owned `cc.manifest.version` (Middleware alone owns the
provisioning manifest); Odoo owns only `cc.campaign.configuration.version`
(desired configuration snapshot/hash) and links to the existing immutable
`call.center.campaign.design.revision` evidence. `codestra_campaign_control_plane`
implements that: `cc.campaign` carries `desired_state`, `effective_state`
and `configuration_version_id`; `call.center.campaign` remains only the
delegated base record and owns no second lifecycle or manifest model.

## 1. Odoo responsibility

Odoo is the campaign control plane and CRM business system of record. It owns:

- campaigns, campaign templates, immutable template and configuration versions;
- campaign membership, agent and supervisor authorization scope;
- customer profiles, CRM tasks, activities, callbacks and appointments;
- agent, supervisor and administrator workspace projections;
- desired provisioning state and verified effective-state/read-back evidence;
- operational communication timelines and read-safe external projections;
- transactional outbox records and reconciliation-visible audit metadata.

Odoo does not own provider execution or another application's business truth.
Klyrow owns email delivery state, Telnexa owns SMS delivery state, VICIdial
owns active dialer/call state, and each business application owns its domain
records. Odoo keeps projections and correlation references only.

## 2. Canonical model decisions

Existing owners remain authoritative and are extended in place:

| Capability | Odoo owner |
| --- | --- |
| Campaign | `cc.campaign` |
| Membership and campaign scope | `cc.campaign.membership` |
| Customer projection | `cc.customer.profile` |
| Campaign workflow | `codestra.campaign.workflow` (compatibility-bound to the canonical campaign) |
| Agent channel | `codestra.agent.channel` |
| Integration delivery | existing transactional outbox/provisioning models |

The `codestra_campaign_control_plane` addon owns only missing metadata:
`cc.application`, `cc.campaign.template`, immutable template versions,
`cc.field.definition`, typed profile field values, workspace layout metadata,
allowlisted integration bindings, immutable campaign configuration versions,
and provisioning runs (`cc.campaign.provisioning.run`).
It deliberately does not create a second customer, campaign, workflow, or
workspace business model.

## 3. Governed integration path

All cross-system commands follow:

```text
Browser/Odoo action -> Odoo transactional outbox -> Middleware
-> versioned application/provider adapter -> authoritative system
-> verified read-back/event -> Odoo projection
```

No Odoo module may connect directly to an application database, provider API,
or monitoring system. Middleware is the only cross-system writer and owns
service registration, adapter routing, retries, inbox processing,
reconciliation, drift and kill switches. n8n is asynchronous automation, not
an event bus or authority.

Every command/event must preserve tenant, business unit, campaign,
application/entity identity, source version, correlation/causation IDs,
idempotency identity and payload hash. Odoo mutations are resource-specific;
there is no generic model, method, domain, SQL, or unrestricted write API.

## 4. Odoo API surface

Existing compatible controllers remain the source of truth. New routes are
added only when no equivalent exists:

- Middleware-facing campaign-control operations exactly as pinned by the
  shared contract `contracts/odoo/campaign-control.v1.json`
  (`POST /api/v1/integration/campaigns/actual-state`,
  `POST /api/v1/integration/campaigns/read`,
  `POST /api/v1/integration/desired-state/read`), plus the administrative
  platform client routes under `/platform/v1/campaigns`;
  `/api/v1/integration/campaign-actions` is retired (`410 Gone`);
- calculated workspace reads under `/cc/api/v1/me/workspace`;
- calculated application/template reads under `/cc/api/v1/applications` and
  `/cc/api/v1/templates`;
- existing CRM, callback, calendar, click-to-call, outbox and read-back
  routes.

Workspace reads are session-authenticated and campaign-scoped by canonical
membership record rules. Administrative metadata is global-administrator
controlled. Cross-system writes remain authenticated Middleware commands and
are durably recorded before asynchronous delivery.

Delivery is pull-only: Middleware claims, acknowledges or fails outbox events
through `/api/v1/integration/outbox/*`; Odoo never pushes control commands
and runs no database worker. Acknowledgement records intake only. Campaign
`effective_state` (contract enum `unknown`, `absent`, `provisioned_disabled`,
`synthetic_tested`, `active`, `disabled`) changes solely through the
authenticated `campaign.actual_state.write` read-back
(`POST /api/v1/integration/campaigns/actual-state`, 16 required fields,
`Idempotency-Key` equal to the body key, `409 STALE_CONFIGURATION_VERSION`
on stale or future versions). Odoo configuration progress lives in
`control_status` (`draft`, `validated`, `blocked`), never in an effective
state. Commands are optimistic on `expected_configuration_version` under a
campaign row lock and idempotent on the caller `Idempotency-Key`.

## 5. Required safety state

The following capabilities remain disabled by default in staging:

`LIVE_EMAIL_DELIVERY=false`, `LIVE_SMS_DELIVERY=false`,
`LIVE_PSTN_DIALING=false`, `ODOO_WRITE=false`, and inactive n8n workflows.
External resources are provisioned disabled first. Activation requires
validated desired state, verified effective state, synthetic testing, matching
configuration version, no unresolved drift, and an auditable approval.

## 6. Contradiction resolutions

1. **Delivery ownership:** older Odoo documents used “delivery results” as if
   Odoo were authoritative. This specification resolves that wording to
   provider-owned delivery truth plus Odoo read-safe projections.
2. **Workflow ownership:** `codestra.campaign.workflow` currently references
   the legacy campaign model for compatibility. No second workflow is created;
   a controlled compatibility migration is required before removing the
   legacy reference.
3. **Workspace ownership:** the workspace is a calculated projection, not a
   duplicate customer/campaign database. Layout metadata is configuration;
   assignments and records remain canonical.
4. **API ownership:** Odoo exposes its own narrow business projections and
   command intake. Middleware owns platform service catalog, adapter commands,
   retries, inboxes, drift and reconciliation APIs.
5. **Monitoring:** telemetry is operational evidence only and never transports
   or stores business authority.

## 7. Repository-by-repository implementation plan

| Repository/service | Required work | Exit evidence |
| --- | --- | --- |
| **Odoo** | Harden canonical models, control-plane addon, ACLs/rules, campaign lifecycle, workspace/callback/click-to-call surfaces, outbox/read-back projections, migrations and runtime tests. | Clean install/upgrade, ORM security tests, API contract tests, campaign isolation tests, staging smoke evidence. |
| **Middleware** | Add the typed Odoo campaign-control adapter/router using existing Odoo delivery client and allowlisted operations; implement durable command/read-back/retry/reconciliation behavior. | OpenAPI contract, auth/idempotency/concurrency tests, inbox/DLQ/reconciliation evidence, no duplicate adapter. |
| **Keycloak** | Publish service identities, audiences, roles/scopes and MFA policy for human/admin and Middleware callers. | Token claim and negative authorization evidence. |
| **Kong** | Route only approved public paths, enforce authentication, limits and request policy; keep private Odoo/Middleware interfaces private. | Route, mTLS/rate-limit and denial tests. |
| **Caddy** | Terminate TLS and route approved upstreams only; no business logic. | TLS/upstream and ungoverned-route denial evidence. |
| **Codestra-OpenBao** | Hold provider/service credentials and signing-key references; support rotation without secrets in manifests. | Secret-reference and rotation evidence. |
| **VICIdial-Codestra** | Own dialer execution/read-back adapter and call events; never accept direct browser/Odoo writes. | Disabled-first provisioning, event replay and call-state reconciliation tests. |
| **Klyrow** | Own email senders/templates/delivery/suppression state and signed events. | Provider event signature, idempotency and suppression tests. |
| **telnexa** | Own SMS sender/message/conversation/delivery state and signed events. | Provider event signature, idempotency and suppression tests. |
| **Business applications** | Expose private, versioned projections/commands/events for each domain; do not accept direct Odoo database access. | Contract and source-version/reconciliation evidence per application. |
| **N8N** | Run only approved asynchronous workflows using signed Middleware commands/events; remain inactive until certified. | Workflow version, identity, signature and downstream idempotency evidence. |
| **Monitoring repositories** | Collect telemetry through Alloy/OpenTelemetry/Prometheus/Loki/Tempo/Alertmanager/Grafana without becoming a business transport. | SLO/alert/trace evidence and separation checks. |
| **SDK-repository** | Generate clients from canonical contracts; do not become an alternate contract authority. | Generated-client compatibility and version pin evidence. |

## 8. Execution order and certification gate

1. Land Odoo model/security/API changes and tests.
2. Land Middleware adapter and contract tests against the Odoo interface.
3. Publish identity, gateway, TLS and secret references.
4. Deploy the exact protected SHA/artifacts to isolated staging with all live
   flags disabled.
5. Upgrade affected Odoo modules against a sanitized restore.
6. Run replay, out-of-order, retry, dead-letter, drift, rollback,
   cross-tenant/campaign denial, browser workspace and callback tests.
7. Approve activation only after paired database/filestore recovery evidence,
   exact SHA verification, smoke tests and soak completion.

Any unavailable external repository, runtime, staging deployment, database
restore, or provider evidence is a blocker and must be reported as
`PARTIAL` or `BLOCKED`, never inferred as `PASS`.
