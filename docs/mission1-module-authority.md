# Mission 1 — module authority

Source of truth: `config/odoo-module-authority.v1.json` (enforced by `scripts/validate_mission1_foundation.py`,
tested by `tests/security/test_mission1_foundation.py`). Every one of the **81** installable
custom modules is classified exactly once; a module that is missing from, or invented in, the config fails the gate.

| Classification | Count | Meaning |
| --- | --- | --- |
| CANONICAL | 16 | owns a business domain or a platform boundary; the only place a domain's authority may live |
| SUPPORTING | 54 | implements features on top of a canonical owner without competing for authority |
| TRANSITIONAL | 7 | still holds models or routes that a canonical owner will absorb; may not control runtime provisioning |
| COMPATIBILITY | 3 | facade or legacy record kept so installations and external identifiers stay stable |
| RETIRE_CANDIDATE | 1 | MIGRATE_AND_REMOVE pending live-data proof; not deleted by this mission |
| LEGACY / DEPRECATED | 0 | none at this head |

No module was deleted: overlap is resolved by ownership (see `docs/domain-ownership-v1.md`), not by removal.

## Duplicate model declarations

None. `codestra_integration_hub` extends `codestra.integration.event` with `_inherit` (owner `codestra_vicidial_crm`),
so the blocker recorded in `docs/reconciliation/DUPLICATE-MODEL-FACADE-AUDIT.md` is resolved in source; the gate
rejects any future second `_name` declaration.

## Inventory

| Module | Class | Domain | Models | Routes | sudo sites | Tests | Rationale |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `call_center_campaign` | CANONICAL | campaign | 14 | 22 | 40 | yes | Physical campaign record (call.center.campaign), Middleware integration API for the shared contract, runtime integration outbox/inbox, traces and nonces. Automatic design provisioning is present but its outbox cron ships inactive and unloaded; runtime provisioning authority belongs to the control plane. |
| `call_center_compliance` | CANONICAL | compliance | 3 | 0 | 1 | yes | Compliance rules, consent and suppression (owner per DUPLICATE-MODEL-FACADE-AUDIT). |
| `call_center_core` | CANONICAL | foundation | 18 | 0 | 1 | yes | Business units, branches, audit events, calling-hours policy, journey/status vocabularies and the business-unit mixin every other module scopes on. |
| `codestra_agent_onboarding` | CANONICAL | agent | 3 | 0 | 0 | yes | codestra.agent.onboarding is the canonical agent business lifecycle; cc.webrtc.session holds WebRTC state; the Middleware provisioning client model is the onboarding-side intent. |
| `codestra_campaign_control_plane` | CANONICAL | campaign_control | 11 | 18 | 20 | yes | Sole runtime provisioning/activation authority for campaigns: validated configuration versions, control commands, provisioning runs, readback receipts, pull-only Middleware delivery. |
| `codestra_cc_calls` | CANONICAL | call | 12 | 0 | 0 | yes | Call operations: operation outbox, callbacks, appointments, transfers, referrals and their policies. |
| `codestra_cc_core` | CANONICAL | campaign | 5 | 0 | 0 | yes | cc.campaign is the canonical campaign identity and lifecycle (delegating storage to call.center.campaign), with cc.business.unit, campaign channels and policies. |
| `codestra_cc_crm` | CANONICAL | crm | 1 | 0 | 0 | yes | CRM service group and lead ownership rules for the cc line. |
| `codestra_cc_identity` | CANONICAL | identity | 3 | 2 | 0 | yes | Identity outbox, session scope and reassignment records that propagate Odoo-side identity intent to Middleware; Keycloak stays the identity database. |
| `codestra_cc_mail` | CANONICAL | email | 7 | 0 | 0 | yes | Mail sender identity, mailboxes and mail ingestion service group. |
| `codestra_cc_security` | CANONICAL | roles | 2 | 0 | 0 | yes | Canonical group set (group_cc_*), campaign membership and break-glass grants that every cc record rule resolves through. |
| `codestra_email_middleware` | CANONICAL | email | 1 | 0 | 11 | yes | Email outbox and Middleware email transport. |
| `codestra_identity_provisioning` | CANONICAL | provisioning | 19 | 2 | 11 | yes | Agent/user provisioning requests and steps, identity links to Keycloak subjects, extension pools/assignments, channel entitlements, tenants and credential references. |
| `codestra_middleware_bridge` | CANONICAL | middleware_boundary | 6 | 21 | 7 | yes | Canonical Odoo -> Middleware client boundary: middleware request/outbound records, agent provisioning transport, CRM/customer/ticket/Klyrow projection routes. |
| `codestra_sms_middleware` | CANONICAL | sms | 1 | 0 | 15 | yes | SMS outbox and Middleware SMS transport; live delivery disabled by default. |
| `codestra_telephony_bridge` | CANONICAL | telephony_readback | 6 | 7 | 9 | yes | Telephony readback and reconciliation boundary bound to the pinned calling contract. |
| `call_center_lead_validation` | SUPPORTING | leads | 1 | 0 | 1 | yes | Lead validation results. |
| `codestra` | SUPPORTING | meta | 0 | 0 | 0 | no | Umbrella module. |
| `codestra_admin_agent_workspace` | SUPPORTING | workspace | 1 | 0 | 5 | yes | Native admin/agent workspace screens. |
| `codestra_ai_agent_assistant` | SUPPORTING | ai | 1 | 0 | 0 | yes | AI agent assistant (advisory). |
| `codestra_ai_call_audit` | SUPPORTING | ai | 0 | 0 | 0 | no | AI call audit (advisory, no models). |
| `codestra_ai_core` | SUPPORTING | ai | 4 | 0 | 0 | no | AI core models (advisory). |
| `codestra_ai_qualification` | SUPPORTING | ai | 0 | 0 | 0 | no | AI qualification (advisory, no models). |
| `codestra_ai_realtime_assistant` | SUPPORTING | ai | 0 | 0 | 0 | no | AI realtime assistant (advisory, no models). |
| `codestra_ai_review` | SUPPORTING | ai | 0 | 0 | 0 | no | AI review (advisory, no models). |
| `codestra_analytics_reporting` | SUPPORTING | reporting | 2 | 0 | 0 | no | Analytics reporting. |
| `codestra_appointments` | SUPPORTING | call | 7 | 0 | 5 | yes | Appointments and callback synchronisation extending codestra.callback; outbound calls go to the Middleware callback API only. |
| `codestra_base` | SUPPORTING | foundation | 5 | 0 | 3 | yes | Shared base models and helpers. |
| `codestra_case_management` | SUPPORTING | crm | 1 | 0 | 0 | yes | Case management. |
| `codestra_cc_agent_desktop` | SUPPORTING | workspace | 0 | 0 | 0 | yes | Agent desktop composition. |
| `codestra_cc_analytics` | SUPPORTING | reporting | 0 | 0 | 0 | yes | Analytics composition. |
| `codestra_cc_audit` | SUPPORTING | compliance | 1 | 0 | 0 | yes | Append-only audit ledger and audit service group. |
| `codestra_cc_automation` | SUPPORTING | automation | 0 | 0 | 0 | yes | Automation composition (no live execution). |
| `codestra_cc_compliance` | SUPPORTING | compliance | 8 | 0 | 0 | yes | Separately named compliance evidence/policy models extending the canonical compliance owner (COMPATIBILITY_LAYER in the facade audit). |
| `codestra_cc_customer_360` | SUPPORTING | crm | 0 | 0 | 0 | yes | Customer 360 composition. |
| `codestra_cc_disposition` | SUPPORTING | call | 5 | 0 | 0 | yes | Dispositions and sub-dispositions. |
| `codestra_cc_helpdesk` | SUPPORTING | crm | 3 | 0 | 0 | yes | Helpdesk tickets. |
| `codestra_cc_mailbox` | SUPPORTING | email | 0 | 0 | 0 | yes | Mailbox composition. |
| `codestra_cc_omnichannel` | SUPPORTING | crm | 0 | 0 | 0 | yes | Omnichannel composition. |
| `codestra_cc_quality` | SUPPORTING | quality | 9 | 0 | 0 | yes | QA scorecards and reviews. |
| `codestra_cc_recordings` | SUPPORTING | call | 4 | 0 | 0 | yes | Recording read-side and recording service group. |
| `codestra_cc_reliability` | SUPPORTING | observability | 0 | 0 | 0 | yes | Reliability composition. |
| `codestra_cc_reporting` | SUPPORTING | reporting | 4 | 0 | 0 | yes | Reporting projections. |
| `codestra_cc_supervisor` | SUPPORTING | workspace | 0 | 0 | 0 | yes | Supervisor landing composition. |
| `codestra_cc_wfm` | SUPPORTING | workforce | 8 | 0 | 0 | yes | Workforce management and workforce event service group. |
| `codestra_cc_workforce` | SUPPORTING | workforce | 1 | 0 | 0 | yes | Workforce projections. |
| `codestra_client_operations` | SUPPORTING | crm | 2 | 0 | 0 | yes | Client operations. |
| `codestra_client_portal` | SUPPORTING | portal | 0 | 0 | 0 | yes | Client portal. |
| `codestra_daily_reporting` | SUPPORTING | reporting | 8 | 0 | 0 | no | Daily operations reports. |
| `codestra_data_quality` | SUPPORTING | leads | 1 | 0 | 0 | yes | Data-quality checks. |
| `codestra_intake_leads` | SUPPORTING | leads | 2 | 0 | 4 | yes | Intake lead identity and projection via Middleware. |
| `codestra_interaction_workflow` | SUPPORTING | crm | 3 | 0 | 0 | yes | Interaction workflow read-only operations. |
| `codestra_ivr_control` | SUPPORTING | telephony_projection | 6 | 0 | 0 | no | IVR control models (no runtime dial path). |
| `codestra_klyrow_smtp` | SUPPORTING | email | 0 | 0 | 14 | yes | Klyrow SMTP policy projected from Middleware; no direct SMTP transport. |
| `codestra_kyqra_review_hub` | SUPPORTING | business_line | 3 | 0 | 5 | yes | Kyqra review hub (read-only projections). |
| `codestra_lead_automation` | SUPPORTING | leads | 8 | 1 | 5 | yes | Lead automation and channel eligibility. |
| `codestra_lead_ingestion` | SUPPORTING | leads | 6 | 0 | 13 | yes | Lead import outbox and ingestion. |
| `codestra_login_branding` | SUPPORTING | identity_ui | 0 | 0 | 0 | yes | Login branding. |
| `codestra_mail_inbox` | SUPPORTING | email | 10 | 0 | 7 | yes | Mail inbox records. |
| `codestra_marketing_crm` | SUPPORTING | business_line | 0 | 0 | 0 | yes | Marketing CRM system-of-record contract. |
| `codestra_metrics` | SUPPORTING | observability | 0 | 1 | 6 | yes | Prometheus metrics route. |
| `codestra_moneybee_crm` | SUPPORTING | business_line | 1 | 0 | 1 | yes | MoneyBee CRM projection over the Middleware bridge. |
| `codestra_observability_integration` | SUPPORTING | observability | 3 | 5 | 3 | yes | Observability incidents/KPIs projection API for Middleware. |
| `codestra_orbit_theme` | SUPPORTING | identity_ui | 0 | 4 | 6 | yes | Orbit theme and Keycloak SSO login/callback/logout routes (identity lives in Keycloak). |
| `codestra_revenue_assurance` | SUPPORTING | reporting | 2 | 0 | 0 | yes | Revenue assurance. |
| `codestra_scrapper_projection` | SUPPORTING | business_line | 2 | 0 | 9 | yes | Scrapper projection (read-only). |
| `codestra_social_orchestration` | SUPPORTING | business_line | 8 | 0 | 0 | yes | Social orchestration models. |
| `codestra_training_academy` | SUPPORTING | workforce | 2 | 0 | 0 | yes | Training academy. |
| `codestra_transcription` | SUPPORTING | ai | 3 | 0 | 0 | no | Transcription records. |
| `codestra_workspace_theme` | SUPPORTING | ui | 0 | 0 | 0 | yes | Workspace theme. |
| `codestra_campaign_crm_os` | TRANSITIONAL | campaign | 37 | 1 | 35 | yes | 37 models including codestra.agent.profile, codestra.crm.outbox and codestra.campaign.* workflow objects; MIGRATE_AND_REMOVE pending live data and migration proof. It may not control runtime provisioning. |
| `codestra_campaign_publishing` | TRANSITIONAL | campaign | 0 | 0 | 0 | yes | Campaign publishing tests over the staging design path. |
| `codestra_cc_vicidial` | TRANSITIONAL | telephony_projection | 3 | 0 | 0 | yes | Telephony readback service group and VICIdial projections; commands never originate here. |
| `codestra_staging_campaign_design` | TRANSITIONAL | campaign | 1 | 0 | 0 | yes | Staging campaign design previews; revision-bound and pull-only. |
| `codestra_vicidial_connector` | TRANSITIONAL | telephony_projection | 4 | 0 | 0 | yes | Connector projections over codestra_integration_hub; retires with it. |
| `codestra_vicidial_crm` | TRANSITIONAL | telephony_projection | 33 | 30 | 52 | yes | Owner of codestra.callback, codestra.vicidial.* projections, calling realtime read-side, call control routes and the Middleware calling client. Canonical for the legacy event model per the facade audit; command execution stays with Middleware. |
| `codestra_vicidial_recording` | TRANSITIONAL | telephony_projection | 5 | 3 | 12 | yes | Recording projection and playback via the recording middleware API. |
| `call_center_orchestration` | COMPATIBILITY | provisioning | 8 | 0 | 0 | yes | Legacy call.center.provisioning.request, callback tasks and lead import batches; still a dependency of umbrella modules. It is not a provisioning authority. |
| `codestra_cc_campaign` | COMPATIBILITY | campaign | 0 | 0 | 0 | yes | Facade only (no models); keeps installation paths stable. |
| `codestra_odoo_certification` | COMPATIBILITY | certification | 1 | 0 | 0 | yes | Certification fixtures and readiness projections. |
| `codestra_integration_hub` | RETIRE_CANDIDATE | inbox_outbox | 7 | 0 | 1 | yes | Duplicate codestra.integration.event and a second delivery/idempotency implementation; MIGRATE_AND_REMOVE per the facade audit unless live reconciliation proves it required. |
