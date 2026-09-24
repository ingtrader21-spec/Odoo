# Mission 1 — evidence matrix

Branch `mission/odoo-mission1-foundation-20260918`, base parent `7a209c310f1940084ff9b6b4cdc4d83f1b1afe4e` (PR #142 certified candidate), PR https://github.com/ingtrader21-spec/Odoo/pull/143 (#143, base codex/cross-repo-authority-20260916, MERGEABLE/CLEAN, review pending).
Every row names the command that produced it; complete outputs are preserved under `artifacts/`.

| Gate | Requirement | Command | Local result (Windows) | Authority |
| --- | --- | --- | --- | --- |
| Module authority | 81 modules classified, no duplicate `_name` authority | `validate_mission1_foundation.py` | MODULE_AUTHORITY=PASS modules=81 (canonical 16, supporting 54, transitional 7, compatibility 3, retire_candidate 1) | GREEN |
| Domain ownership | 7 domains, one canonical owner each, owner models exist | same | DOMAIN_OWNERSHIP=PASS domains=7 | GREEN |
| Campaign authority | `cc.campaign` canonical; control plane sole runtime authority; legacy design cron inactive+unloaded | same + `test_only_the_control_plane_controls_campaign_runtime_provisioning` | PASS | GREEN |
| Agent authority | `codestra.agent.onboarding` + `codestra.identity.link`; Odoo not an identity DB | `test_odoo_never_becomes_the_identity_database` | PASS | GREEN |
| Roles | 5 canonical roles → existing groups; membership model; authority matrix | ROLE_FOUNDATION=PASS; `validate_campaign_authority_matrix.py` PASS (10 roles) | PASS | GREEN |
| Campaign/company isolation | scoped record rules; bypass only by service/admin/break-glass/matrix-justified roles; auditor read-only | `test_campaign_and_company_isolation_rules_are_membership_bound` | PASS | GREEN |
| Controller governance | 135/135 routes classified; 0 INTERNAL; 0 REVIEW_REQUIRED sudo; 0 unauthenticated mutations; MIDDLEWARE_ONLY routes carry service identity | CONTROLLER_GOVERNANCE=PASS routes=135 middleware_only=80 | PASS | GREEN |
| Middleware boundary | inbound: only Middleware writes; outbound: 11 sites all MIDDLEWARE_CLIENT / IDENTITY_PROVIDER / READ_ONLY_PROJECTION | `validate_integration_boundary.py` PASS; PROVIDER_PATHS=PASS | PASS | GREEN |
| Provider bypass governance | 0 direct provider transports; forbidden imports rejected | `test_direct_provider_transport_is_rejected`, `test_no_forbidden_transport_in_source` | PASS | GREEN |
| Provisioning lifecycle | `codestra.provisioning.request` 14 states mapped CREATE→VALIDATE→DISPATCH→STATUS→RECONCILE→DISABLE; steps; legacy request compatibility | STATE_MODELS=PASS machines=15 | PASS | GREEN |
| Endpoint catalog | inventory regenerated offline, deterministic, 135 rows, 0 DYNAMIC_ROUTE, 0 REJECT, 3 RETIRED, csrf/caller/downstream present; catalog parity | `generate_reconciliation_ledgers.py --endpoints-only` ×2 byte-identical (sha256 efa4ca06…); `generate_odoo_endpoint_catalog.py --check` PASS; `validate_api_contracts.py` source PASS | PASS | GREEN |
| State models | 15 machines' states equal the model selections; campaign transition table and control commands equal code | STATE_MODELS=PASS; `test_state_drift_is_rejected` | PASS | GREEN |
| Migration safety | additive only; migration contracts source gate | `validate_migration_contracts.py` MISSION_MIGRATION_SOURCE_GATE=PASS (restore rehearsal BLOCKED_RUNTIME_EVIDENCE by design) | PASS | GREEN |
| Static validators | all 23 run_ci.sh validators + Mission-1 gate | see `artifacts/mission1-local-regression.txt` | 29/29 exit 0 | GREEN |
| Source tests | isolated source harness | `run_isolated_source_tests.py` | Windows: 223 run, 30 errors — all pre-existing Windows-only (`docs/mission1-isolated-source-root-cause.md`, baseline differential identical) | Linux CI authoritative |
| Mission-1 focused tests | 25 tests incl. negatives | `python -m unittest tests.security.test_mission1_foundation` | 25 OK | GREEN |
| Frontend tests | node test files | 2 files | OK | GREEN |
| Security gates | integration boundary, mission security, authority matrix, API contracts, shared contract, foundation | `artifacts/mission1-security-recheck.txt` | all exit 0 | GREEN |
| Odoo/PostgreSQL runtime | Odoo 19 module suite | Linux CI only | — | see CI section |

## Linux CI on the exact Mission-1 SHA `623ee8046ac76397e758f302b0fe32962e798008`

| Workflow | Conclusion | Run |
| --- | --- | --- |
| Odoo Addons CI (source head, merge result, Odoo 19 + PostgreSQL runtime, isolated source tests, Mission-1 gate) | success | 35392281512 |
| Security gates (secret-and-source-security, dependency-review) | success | 35392281440 |
| Production container CI (digest-pinned image build and scan) | success | 35392281478 |
| calling-contract-pin | success | 35392281531 |
| Codestra deploy readiness (pre-transfer reusable-workflow owner; fails on main too; not a required context) | failure | 35392281492 |

Odoo 19 + PostgreSQL runtime suite: **892 tests, 0 failed, 0 errors** (Odoo Addons CI run 35392281512: Validate Odoo source head, Validate Odoo merge result, Test Odoo 19 and PostgreSQL runtime all success). Isolated source tests (Linux): **223 run, 0 failures, 0 errors (source head and merge result)**.

## Counts

| Item | Value |
| --- | --- |
| module count | 81 |
| canonical domains | 7 |
| canonical roles | 5 (+5 justified) |
| endpoint count | 135 |
| focused Mission-1 tests | 25 |
| isolated source tests | 223 (198 baseline + 25) |
| state machines | 15 |
| production effects | 0 |
