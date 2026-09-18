#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -z "${PYTHONPYCACHEPREFIX:-}" ]]; then
  CI_PYCACHE_DIR="$(mktemp -d)"
  export PYTHONPYCACHEPREFIX="$CI_PYCACHE_DIR"
  trap 'rm -rf -- "$CI_PYCACHE_DIR"' EXIT
fi

UPSTREAM_SYNC_INITIALIZED=NO
if [[ -e config/upstream-sync-state.json || -e upstream/codestra-odoo-addons ]]; then
  UPSTREAM_SYNC_INITIALIZED=YES
fi
for treeish in HEAD^ origin/main; do
  if git cat-file -e "$treeish:config/upstream-sync-state.json" 2>/dev/null; then
    UPSTREAM_SYNC_INITIALIZED=YES
  fi
done
if [[ "$UPSTREAM_SYNC_INITIALIZED" == YES ]]; then
  [[ -f config/upstream-sync-state.json && -d upstream/codestra-odoo-addons ]] || {
    printf '%s\n' 'initialized upstream synchronization evidence is missing' >&2
    exit 1
  }
  python3 -I scripts/sync_codestra_odoo_addons.py --verify-state
fi

printf '==> Checking Git whitespace errors\n'
git diff --check

printf '==> Checking shell syntax\n'
while IFS= read -r -d '' script; do
  bash -n "$script"
done < <(find scripts -type f -name '*.sh' -print0 | sort -z)

printf '==> Validating the canonical calling-contract pin\n'
python3 -I scripts/validate_calling_contract_pin.py
python3 -I scripts/validate_calling_contract_pin.py --self-test
python3 -I scripts/generate_calling_client_schema.py --check

printf '==> Validating the declared calling-contract usage surface\n'
python3 -I scripts/validate_calling_contract_usage.py
python3 -I scripts/validate_calling_contract_usage.py --self-test

printf '==> Compiling Python files\n'
python3 -I -X pycache_prefix="$PYTHONPYCACHEPREFIX" -m compileall -q custom-addons scripts tests/security

printf '==> Validating campaign authority matrix\n'
python3 -I scripts/validate_campaign_authority_matrix.py

printf '==> Verifying the immutable canonical addon baseline\n'
python3 -I scripts/validate_legacy_addon_baseline.py

printf '==> Validating Odoo manifests\n'
python3 -I scripts/validate_manifests.py

printf '==> Validating every custom browser asset and stylesheet\n'
python3 -I scripts/validate_asset_integrity.py

printf '==> Checking Odoo 19 call popup startup compatibility\n'
node --experimental-vm-modules --test tests/frontend/test_call_popup_rpc.mjs

printf '==> Checking the call popup two-way screen-pop error handling\n'
node --experimental-vm-modules --test tests/frontend/test_call_popup_handle_call.mjs

printf '==> Validating the Middleware and Odoo write boundary\n'
python3 -I scripts/validate_integration_boundary.py

printf '==> Validating the Mission 1 business operations foundation\n'
python3 -I scripts/validate_mission1_foundation.py

printf '==> Validating the four-repository platform control plane\n'
python3 -I scripts/validate_platform_control_plane.py

printf '==> Validating the observability control plane\n'
python3 -I scripts/validate_observability_control_plane.py

printf '==> Reviewing every custom Odoo module\n'
python3 -I scripts/review_modules.py --strict

printf '==> Validating Codestra login, administrator, and database controls\n'
python3 -I scripts/validate_codestra_readiness.py

printf '==> Validating the corporate call-center workstream contract\n'
python3 -I scripts/validate_call_center_workstreams.py

printf '==> Validating complete mission module coverage\n'
python3 -I scripts/validate_mission_coverage.py

printf '==> Validating mission security and closed capabilities\n'
python3 -I scripts/validate_mission_security.py

printf '==> Validating fail-closed Klyrow SMTP routing\n'
python3 -I scripts/validate_klyrow_smtp_policy.py

printf '==> Validating canonical API inventory\n'
python3 -I scripts/validate_api_contracts.py

printf '==> Validating the shared Middleware->Odoo campaign-control contract\n'
python3 -I scripts/validate_odoo_shared_contract.py
python3 -I scripts/generate_odoo_endpoint_catalog.py --check

printf '==> Validating migration policies\n'
python3 -I scripts/validate_migration_contracts.py

printf '==> Validating browser, load, security, and migration evidence contracts\n'
python3 -I scripts/validate_test_evidence_contracts.py

printf '==> Running source-level mission contract tests\n'
python3 -I -X pycache_prefix="$PYTHONPYCACHEPREFIX" scripts/run_isolated_source_tests.py

printf '==> Validating release-candidate policy\n'
python3 -I scripts/validate_release_policy.py

printf '==> Validating blocked-by-default production evidence schema\n'
python3 -I scripts/validate_production_evidence.py \
  --file release/production-evidence-template.json \
  --allow-blocked-template
