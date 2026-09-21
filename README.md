# Appolon Odoo 19 Custom Addons

`appolon1908-hue/Odoo` is the primary repository and source of truth for
Codestra's self-hosted Odoo 19 custom modules. New development, pull requests,
reviewed releases, and deployment source references belong here.

`Codestra-SRL/codestra-odoo-addons` is the legacy source for the controlled
migration. After its complete source has been imported and accepted, retain
that repository as a historical backup. Develop and release from Appolon.
The repository-specific integration-plane ownership and implementation plan is
in [`docs/CODESTRA-INTEGRATION-PLANE-DESIGN.md`](docs/CODESTRA-INTEGRATION-PLANE-DESIGN.md).

The import places runnable modules in `custom-addons/` and preserves the
complete source snapshot in `upstream/codestra-odoo-addons/`. Keep existing
addon technical names and XML IDs so the repository move does not break
installed modules or their dependencies. See
[`docs/UPSTREAM-SYNC.md`](docs/UPSTREAM-SYNC.md) for the import and provenance
requirements. The private-source copy is still pending; declaring Appolon
primary does not establish that the repositories are synchronized.

> The repository is currently public. Keep it limited to non-secret code and
> bootstrap controls until its visibility is changed to private. Never commit
> credentials, customer data, databases, filestore content, or runtime
> configuration containing secrets.

## Current modules

### `codestra_login_branding`

A responsive dark Codestra authentication surface for Odoo 19. It inherits the
official `web.login_layout` and `web.login` templates, preserves Odoo
authentication behavior, removes the default vendor/database-manager footer,
uses local assets only, and includes Odoo view and HTTP tests.

Administrator provisioning is intentionally outside module installation. The
reviewed `scripts/ensure_codestra_admin.py` script makes
`appolon1908@gmail.com` the human Odoo Administrator only when the explicit
apply gate and external password secret file are supplied. It never repurposes
Odoo's technical superuser and never grants PostgreSQL superuser privileges.

## Validation

```bash
bash scripts/run_ci.sh
```

Static CI reviews every custom module and validates manifests, XML, tests,
assets, login contracts, administrator policy, and the Middleware-only write
boundary.

GitHub-hosted runtime CI also starts isolated PostgreSQL and Odoo 19 containers
from digest-pinned official images. It installs and tests every custom module,
exercises the administrator bootstrap, and audits database, administrator, and
module state.

The hosted runtime test proves the repository works on its isolated CI
database. It does not prove that a live server database is healthy. Run the
read-only host audit in
[`docs/LOGIN-ADMIN-DATABASE-RUNBOOK.md`](docs/LOGIN-ADMIN-DATABASE-RUNBOOK.md)
against staging and production before activation.

## Operating model

1. Create a feature branch.
2. Change or add modules under `custom-addons/`.
3. Open a pull request and pass exact-head, merge-result, and runtime CI.
4. Deploy the reviewed, merged commit SHA to staging.
5. Upgrade only the affected modules and run smoke tests.
6. Run the live database, administrator, and module audit.
7. Deploy the exact same commit SHA to production after backup and approval.

## Repository scope

Commit:

- custom Odoo modules;
- module tests and migration scripts;
- non-secret configuration templates;
- validation and deployment scripts;
- operational documentation.

Never commit:

- PostgreSQL databases or dumps;
- Odoo filestore or attachments;
- `.env` files, passwords, tokens, private keys, or certificates;
- live `odoo.conf` files containing credentials;
- runtime volumes, sessions, logs, or backups;
- edits made inside a running Odoo container.

## Server connection

Follow [`docs/SERVER-CONNECTION.md`](docs/SERVER-CONNECTION.md) to inventory the
Docker deployment, create a read-only deploy key, mount the repository checkout,
and deploy exact reviewed commit SHAs.

The production server must consume this repository through a read-only deploy
credential and deploy an exact reviewed commit SHA. Git rollback alone is not
sufficient after a database-changing module upgrade; restore the matching
database and filestore recovery point when rollback requires data reversal.
