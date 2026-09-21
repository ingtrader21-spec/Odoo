# Codestra VICIdial CRM

Odoo 18 core call-center addon. Version 18.0.2.0.0 depends on `codestra_base` while preserving the installed technical name, transactional tables, model names, XML IDs, menus, security groups, and record relationships.

The addon owns CRM/user call-center extensions, VICIdial agents, campaigns, phones, calls/events, dispositions, transfers, recordings, queue snapshots, the single callback model, and the installed compatibility sync-event model. Generic integration tables remain compatibility-owned here until a migration-safe Integration Hub extension exists.

All live writes, synchronization, n8n delivery, external AI delivery, call control, and recording access are fail-closed through `codestra_base`. This addon performs no VICIdial/Asterisk request and stores no plaintext credential.

Legacy group XML IDs remain authoritative for current memberships and imply their matching `codestra_base` groups. Future transfer-request, compliance, AI, QA, automation, and notification models are deliberately excluded from active core loading and assigned to later suite addons.

## Canonical command authentication

`originate_command` is a private Python-only transport alongside the legacy
click-to-call route. Configure `codestra.middleware.telephony_command_url` with
the exact HTTPS `/v1/telephony/commands` endpoint. The canonical transport never
uses `codestra.middleware.api_key`.

Configure these values in the Odoo service environment:

- `CODESTRA_TELEPHONY_TOKEN_URL`: the reviewed HTTPS OIDC token endpoint.
- `CODESTRA_TELEPHONY_CLIENT_ID`: the dedicated telephony service client.
- `CODESTRA_TELEPHONY_CLIENT_SECRET_FILE`: an absolute path to its secret file,
  readable only by the service and administrators.
- `CODESTRA_TELEPHONY_CA_FILE`: optional absolute CA bundle for the OIDC endpoint;
  otherwise the system trust store is used.

The client uses `client_credentials` with `scope=telephony.commands.write`. Configure
the identity provider to return that scope explicitly, a Bearer access token,
and `expires_in` longer than the ten-second command timeout. Its issuer,
audience and authorized tenant/campaign/actor claims must match Middleware's
OIDC policy; Middleware verifies those claims. Deploy a separate appropriately
scoped service configuration where tenant isolation requires it. Never put a
client secret or bearer token in Odoo parameters, browser state or logs.

Local envelope validation, including the contract's 16–128 visible-ASCII
idempotency-key bound, completes before token acquisition. Token acquisition
uses verified TLS with redirects and environment proxies disabled. Every auth
failure is a redacted pre-dispatch rejection. Command HTTP 401 is also a
rejection. Commands are never retried automatically or rerouted to the legacy
endpoint. Ambiguous command responses/timeouts still require reconciliation
with the original operation and idempotency identities.

Offline regressions: `python3 tests/security/test_telephony_command_envelope.py`.
These tests do not certify live token provisioning or telephony activation.

## Controlled TEST_SYN transport

The operator-only `action_test_syn_internal_call` action is the sole Odoo
entrypoint for the first transport certification. It requires an active agent
whose reviewed identity is exactly `appolon` / phone login `6901`, a `TEST_SYN`
campaign in `test` mode, a tenant-bound Keycloak subject, and an enabled system
parameter `codestra.telephony.test_syn_enabled=true`. It always sends the named
`internal:TEST_ECHO` alias with `recording_requested=false`; a lead phone number,
PSTN route, trunk, or arbitrary dialplan value is never read.

Configure `codestra.middleware.telephony_test_syn_url` as the Middleware HTTPS
`/v1/calls/originate` compatibility ingress and set
`codestra.middleware.telephony_expected_host` to the exact Middleware hostname.
The client rejects a different host, redirects, credentials, query strings,
Server-B URLs, and non-HTTPS endpoints before acquiring a token. Configure
`codestra.telephony.test_syn_caller_id` only with the reviewed E.164 test caller
ID. The synthetic transport requests the exact Middleware route scope
`telephony.calls.originate`; the issuer must return that scope in the short-lived
Bearer token. It uses the same OIDC client and duplicate-prevention key as the
durable call reservation; timeouts and malformed responses remain
reconciliation-required and are never retried blindly.

Run the exact controlled test once, verify the internal adapter read-back, then
repeat the same correlation/idempotency test. Do not enable PSTN dialing until
both runs have authoritative evidence and independent review.


## Automatic TEST_SYN preflight repair

The browser matcher can repair one legacy CRM row whose stored normalized-phone
projection and TEST_SYN assignment are both missing. This behavior is disabled
by default; enable it with the Odoo system parameter
`codestra.telephony.auto_repair_owned_test_syn_leads=true`.

The repair runs only for `TEST_SYN` in `test` mode, only when the authenticated
agent has exactly one active campaign, and only while the runtime
`codestra.telephony.external_effects_enabled` value is false-like. The immutable
calling-contract pin also keeps external effects disabled. The raw phone,
including a supported `00` international form, must resolve to exactly one
active lead and no contact; malformed raw values fail closed. The lead must be
owned by the caller, belong to an authorized company and business unit, have no
campaign assignment, and not be DNC.

Raw discovery uses stored, B-tree-indexed ASCII digit projections on both
`crm.lead` and `res.partner`; it does not use a leading-wildcard predicate or
a runtime regex table scan. A non-blocking transaction advisory lock serializes
repair attempts by normalized destination. Lock contention returns the normal
`match=none` response, so a worker never waits indefinitely.

When installed global campaign rules hide an otherwise eligible pre-repair row
from an ordinary agent, the repair additionally requires exactly one active,
same-business-unit canonical `TEST_SYN` campaign that explicitly authorizes
that user. A constrained elevated ORM write may then bind only that canonical
campaign and the two legacy TEST_SYN fields; the stored phone projection is
recomputed from the unchanged raw value. A row already visible to the user
stays on the ordinary ORM path.

After the sole candidate row is locked, every ownership, company, business-unit,
DNC, campaign, active-state, raw-phone, contact-collision, and ambiguity guard
is evaluated again from invalidated records. The write runs in a savepoint, and
a fresh ordinary-user read plus the normal campaign-scoped matcher must both
return the same sole lead or the repair rolls back and returns `match=none`.

Matching never creates a call, command, integration event, audit event, or
external effect. Any ambiguity or failed guard preserves the normal
`match=none` result.

