# Mission 1 — current-state audit

Audited at source head `7a209c310f1940084ff9b6b4cdc4d83f1b1afe4e` (branch `mission/odoo-mission1-foundation-20260918` created from PR #142's certified candidate).
Method: AST scan of every module (manifests, `_name`/`_inherit` declarations, selection fields, `@http.route`
decorators, `sudo()` sites, outbound HTTP), the regenerated controller inventory, the security XML/CSV, and the
reviewed classification tables in `config/odoo-*.v1.json`.

## Counts

| Measure | Value |
| --- | --- |
| installable custom modules | 81 |
| declared models (`_name`) | 346 |
| controller routes (inventory rows) | 135 |
| routes by class | LEGACY 3, MIDDLEWARE_ONLY 80, PUBLIC_INTENTIONAL 16, USER_AUTHENTICATED 36 |
| routes by caller | BROWSER_USER 39, MIDDLEWARE_SERVICE 88, NONE_RETIRED 3, PUBLIC 5 |
| sudo governance | GOVERNED_PARAMETER_READ 11, NONE 50, SERVICE_IDENTITY_EXECUTION 66, USER_SCOPE_THEN_SYSTEM 8 |
| sudo() call sites in models/controllers | 302 |
| outbound HTTP sites | 11 (all Middleware, Keycloak or read-only projection) |
| direct provider transports (smtplib/imaplib/twilio/telnyx/MySQL/xmlrpc) | 0 |
| state machines bound to code | 15 |
| canonical domains | 7 |
| canonical roles | 5 (+5 justified additional roles) |

## Findings and their resolution

| # | Finding | Resolution |
| --- | --- | --- |
| M1-A | The committed controller inventory had 72 rows while the controllers declare 135 routes (13 with constant-built paths shown as DYNAMIC_ROUTE); one retired route raising `Gone` was classified as an unauthenticated mutation | generator resolves module/class constants, `list(dict)` and f-strings, recognises `raise Gone(...)` as 410, adds `csrf`/`caller`/`downstream`, and the gate regenerates offline and fails on staleness |
| M1-B | Overlapping campaign concepts (`call.center.campaign`, `cc.campaign`, `codestra.campaign.*`) | ownership resolved: `cc.campaign` canonical identity/lifecycle, `call.center.campaign` storage record, control plane sole runtime authority; legacy design cron proven inactive and unloaded |
| M1-C | Overlapping agent/identity concepts (`codestra.agent.profile`, `codestra.vicidial.agent`, `vicidial.identity.map`) | classified compatibility/projection; `codestra.agent.onboarding` + `codestra.identity.link` canonical |
| M1-D | `codestra_integration_hub` recorded as a duplicate `codestra.integration.event` authority | verified as an `_inherit` extension; module kept as RETIRE_CANDIDATE, no new callers allowed |
| M1-E | 85 routes reach `sudo()` | every route carries a sudo class and justification; no route left in `REVIEW_REQUIRED`/`INTERNAL`; per-site proofs remain a Mission-2 item for the 302 call sites |
| M1-F | `auth="none"` on 88 routes | 80 are `MIDDLEWARE_ONLY` with controller-verified service identity, 8 are intentional public liveness/attestation/SSO surfaces, 3 are retired 410 routes |
| M1-G | Provider-named source (VICIdial, Klyrow, Asterisk/SIP) | no transport exists; all outbound sites classified `MIDDLEWARE_CLIENT`, `IDENTITY_PROVIDER` or `READ_ONLY_PROJECTION` |
| M1-H | Legacy `call.center.provisioning.request` beside `codestra.provisioning.request` | compatibility only; canonical request/step machines bound to code with the CREATE→VALIDATE→DISPATCH→STATUS→RECONCILE→DISABLE mapping |

## Unresolved (recorded, not hidden)

- Per-call-site `sudo()` proofs (302 sites) — governance is at route level in this mission.
- `codestra_campaign_crm_os`, `codestra_integration_hub`, the VICIdial projection modules and `call_center_orchestration`
  remain TRANSITIONAL/COMPATIBILITY/RETIRE_CANDIDATE until live-data migration proof exists.
- Runtime negative authorization (cross-campaign/cross-company attempts against a running Odoo) is asserted at
  source level (record rules, ACLs, authority matrix); the Odoo 19 runtime suite exercises the control-plane and
  campaign guards but Mission 1 adds no new runtime tests.
- `.github/workflows/codestra-deploy-readiness.yml` still references the pre-transfer reusable workflow owner.
