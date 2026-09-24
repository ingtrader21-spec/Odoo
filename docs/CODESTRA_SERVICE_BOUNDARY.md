# Codestra service boundary

Repository: ingtrader21-spec/Odoo
Service ID: business-system
Class: application

This repository is an independent component. It owns its own runtime/configuration, data authority, API/control contract, tests, release evidence, and environment promotion.

## Cross-system rule

Canonical Codestra cross-system flow:

Caddy -> Kong -> Middleware V3 :8095 -> this service API/control surface

No application may use this repository as a shortcut for direct app-to-app writes. No component may write another application's business database. Middleware V3 owns orchestration, command durability, idempotency, policy normalization, retry/reconciliation, and cross-system audit.

Edge and observability components retain their infrastructure responsibilities, but business effects still enter through governed service contracts.

## Environment model

development -> testing -> staging -> production

The contract environment name test maps to the Git branch testing.

Each environment must be independently configurable and must not share mutable production secrets or state with another environment.

## API and users

The component must be prepared for additional APIs, tenants, service callers, and user roles without breaking existing contracts:

- versioned endpoints/contracts
- explicit tenant/user/workload identity
- least-privilege scopes
- no trust in caller-supplied role headers
- OpenBao secret references
- health/readiness/metrics for HTTP runtimes
- private metrics and internal surfaces
- machine-readable API/control contract
- negative authorization and tenant-isolation tests

## Production gates

Production is not a development branch. Promotion requires green testing/staging evidence, immutable artifact identity, rollback/readback evidence, and no weakened security or CI gates.

Effectful commands must be idempotent and fail closed. Ambiguous effects require readback/reconciliation rather than blind retry.
