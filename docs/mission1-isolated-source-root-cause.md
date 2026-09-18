# Mission 1 — isolated source test errors: root cause and baseline differential

Command: `python -I scripts/run_isolated_source_tests.py` (Windows 10 host, Python 3.12.10, git with global
`core.autocrlf=true`). Complete outputs are preserved in `artifacts/mission1-isolated-source-failure.txt`
(Mission 1 worktree) and `artifacts/baseline-7a209c31-isolated-source.txt` (untouched baseline worktree).

| Run | Tree | Tests run | Failures | Errors | Skipped |
| --- | --- | --- | --- | --- | --- |
| Mission 1 | `mission/odoo-mission1-foundation-20260918` (uncommitted work over `7a209c31`) | 223 | 0 | 30 | 0 |
| Baseline | `7a209c310f1940084ff9b6b4cdc4d83f1b1afe4e` (detached, read-only worktree) | 198 | 0 | 30 | 0 |
| Baseline, Linux CI | `7a209c31`, `Odoo Addons CI` run 35373867911 (`Validate Odoo source head`) | 198 | 0 | 0 | 0 |

The 30 error identities (test names, exception classes, first repository frames) are **identical** in both local
runs; the only textual differences are temporary directory names inside `WinError 1314` messages.
Mission 1 introduced **0** errors and its 25 new tests all pass (223 − 198 = 25). Every one of the 30 errors passes
in Linux CI on the same baseline SHA.

## Root-cause clusters

| Cluster | Count | Signature | First repository frame | Classification | Evidence |
| --- | --- | --- | --- | --- | --- |
| A | 4 | `AttributeError: module 'os' has no attribute 'geteuid'` | `custom-addons/codestra_klyrow_smtp/scripts/provision_klyrow_smtp.py:47` (`_parse_protected_env` checks the secret file's owner uid) | WINDOWS_ENVIRONMENT | `os.geteuid` does not exist on Windows; the script is a Linux provisioning tool that refuses secrets not owned by root/the service user — the check is correct on the target platform. Tests: `ProvisionScriptTest.{test_imports_exact_export_and_keeps_delivery_disabled, test_rejects_incomplete_export, test_rejects_unrelated_key, test_rejects_wrong_transport_binding}` |
| B | 9 | `OSError: [WinError 1314] A required privilege is not held by the client` | `tests/security/test_upstream_sync.py` fixture lines 252, 276, 298, 535, 560, 582, 852, 948, 975 (`os.symlink` / `Path.symlink_to`) | WINDOWS_ENVIRONMENT | creating symbolic links requires the SeCreateSymbolicLink privilege (Developer Mode or elevation), which this session does not hold; the tests are the symlink-rejection security cases and cannot even build their fixtures |
| C | 17 | `scripts.sync_codestra_odoo_addons.SyncError: snapshot Git tree drift` | `scripts/sync_codestra_odoo_addons.py:740` (`verify_state`: pure-Python Git tree digest of the snapshot ≠ `git rev-parse HEAD^{tree}` recorded from the fixture repository) | WINDOWS_ENVIRONMENT | reproduced in isolation: a fixture file written with `write_text("hello\n")` lands on disk as `b'hello\r\n'` (Python text-mode newline translation on Windows); git hashes it through the `autocrlf` clean filter as LF (`git hash-object` → `ce013625…`) while `git_tree_digest` hashes the raw disk bytes (`git hash-object --no-filters` → `ef0493b2…`). The two digests therefore differ on this host only; on Linux both are LF and agree (CI green) |

No cluster is `MISSION1_REGRESSION`, `REAL_APPLICATION_DEFECT`, `TEST_HARNESS`, `MISSING_DEPENDENCY`,
`IMPORT_ISOLATION`, `PATH_HANDLING` or `GENERATED_ARTIFACT_STALENESS` (the regenerated endpoint inventory is
verified fresh by `scripts/validate_mission1_foundation.py` and `tests/security/test_mission1_foundation.py`).

## Repair decision

Nothing to repair in Mission 1: all 30 errors are `PREEXISTING_BASELINE` + `WINDOWS_ENVIRONMENT`, proven by the
mandatory baseline differential rather than assumed. They were not converted to skips, the security checks
(symlink rejection, protected secret ownership, snapshot tree binding) were not weakened, and no Windows-only
production behaviour was introduced. Linux CI (`Validate Odoo source head` / `Validate Odoo merge result`, which
run `scripts/run_ci.sh` including this harness) is the authoritative gate for the isolated source tests.
