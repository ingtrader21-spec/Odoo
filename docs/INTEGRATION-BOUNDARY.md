# Odoo 19 integration boundary

**Status:** Accepted and binding for Codestra Odoo customizations.

## System-of-record responsibility

Odoo 19 is the business system of record for:

- customers and contacts;
- leads and opportunities;
- activities and campaigns;
- call history;
- post-call forms and notes;
- callbacks and appointments;
- consent and communication preferences;
- operational SMS and email history and campaign communication timeline;
- normalized delivery-result projections received from the authoritative
  communication providers;
- agent and supervisor business views;
- business reporting.

This repository contains the reviewed custom modules, tests, migrations, and deployment controls that implement those business capabilities. It does not contain the PostgreSQL database, filestore, credentials, certificates, runtime sessions, logs, backups, or edits copied from a running container.

Provider delivery truth remains outside Odoo: Klyrow owns email delivery
state, Telnexa owns SMS delivery state, and VICIdial owns active dialer/call
state. Odoo stores read-safe projections, campaign associations, audit
references, and operational timeline entries; it does not become a provider
ledger or a second delivery system.

## Authorized cross-system writer

Codestra Middleware is the only authorized cross-system writer.

External portals, n8n, telephony systems, SMS/email systems, social systems, crawlers, provider services, and reporting tools must not connect directly to Odoo PostgreSQL or receive Odoo database write credentials.

Middleware may write through only one of these approved interfaces:

1. a narrow Odoo service API implemented by a reviewed custom module; or
2. a reviewed ORM bridge that executes resource-specific commands inside Odoo.

Both interfaces are Odoo application interfaces. Neither is a generic database proxy or unrestricted RPC-to-model adapter.

## Bridge requirements

The reviewed bridge module is `codestra_middleware_bridge`. Its canonical CRM
command endpoint is
`POST /codestra/middleware/v1/commands/crm.lead.upsert`. Before activation it
must:

- authenticate a dedicated Middleware service identity;
- enforce least-privilege access controls and record rules;
- resolve the authoritative tenant, company, and business scope;
- accept versioned resource-specific commands;
- reject caller-selected arbitrary model names, method names, domains, SQL, or field sets;
- validate required business fields and state transitions;
- use the Odoo ORM for business writes;
- use raw SQL only in reviewed migration code when the ORM cannot perform the migration safely;
- store the Middleware command ID and idempotency key in the same transaction as the business change;
- preserve stable mappings between Middleware identifiers and Odoo records;
- use optimistic concurrency or an explicit expected version where overwrite risk exists;
- return the original result for an already-applied command;
- preserve correlation IDs and a safe audit trail;
- provide reconciliation queries that do not expose unrestricted ORM access.

## Database rule

No external service may write directly to Odoo PostgreSQL.

The Odoo application role remains the business-write database identity. Backup tooling, monitoring, migrations, and approved maintenance use separate least-privilege identities and procedures. A read-only reporting role does not become a write path.

The custom-addons repository must not introduce separate PostgreSQL client connections, embedded database passwords, `DATABASE_URL` values, or shell calls to `psql` as an integration mechanism. Reviewed Odoo migration scripts may use the Odoo-managed cursor inside the controlled module-upgrade transaction.

## Idempotency and reconciliation

Every Middleware-originated mutation must carry:

- a stable Middleware command ID;
- a stable idempotency key;
- tenant and company context;
- correlation and causation identifiers;
- the command schema version;
- the expected resource version when applicable.

The Odoo bridge stores the applied command identity atomically with the business change. Concurrent duplicates must result in one applied mutation. A retry returns the existing result rather than creating another lead, activity, appointment, call record, message-history entry, or delivery result.

Unknown outcomes are reconciled by command ID, external mapping, or provider reference. Operators do not fix delivery state by editing Odoo tables manually.

## Recommended resource-specific commands

The bridge may expose narrow commands such as:

- upsert customer/contact by stable external identity;
- create or update a lead/opportunity;
- create an activity, callback, or appointment;
- record a call and post-call disposition;
- record an SMS/email attempt and delivery result;
- update consent or communication preferences;
- attach a normalized provider result;
- query command application and mapping state for reconciliation.

It must not expose `model`, `method`, `domain`, `fields`, `values`, or raw SQL as caller-controlled generic execution parameters.

## Testing gates

A module that implements or changes the integration boundary must prove:

1. unauthenticated and unauthorized service identities fail;
2. cross-company and cross-tenant access fails;
3. record rules and access-control lists are effective;
4. malformed and unsupported command versions fail;
5. duplicate and concurrent commands apply once;
6. the command ledger and business change commit or roll back together;
7. state-transition and optimistic-concurrency conflicts are deterministic;
8. audit and correlation data is preserved;
9. no generic model-write endpoint exists;
10. no external Odoo PostgreSQL write credential is required;
11. clean install, upgrade, migration rollback, and paired database/filestore recovery are tested;
12. staging starts with external delivery and live integration writes disabled.

## Deployment boundary

Only a reviewed protected merged SHA is deployed. The affected modules are upgraded in staging first. Production uses the identical accepted artifact after a matching PostgreSQL and filestore recovery point, explicit approval, smoke testing, and a rehearsed rollback procedure.

Git rollback by itself is not a safe rollback after a data-changing Odoo module upgrade. Database and filestore recovery must match the module revision when data reversal is required.
