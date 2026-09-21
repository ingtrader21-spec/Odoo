#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Frozen production-compatible image identities captured read-only on 2026-08-28.
ODOO_IMAGE="${ODOO_CI_IMAGE:-docker.io/library/odoo@sha256:f54272f31d5f77e4146b887efb3761c98480317daf687e4b4b5e76ed8bcc08c5}"
POSTGRES_IMAGE="${POSTGRES_CI_IMAGE:-docker.io/library/postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94}"
CHROME_VERSION="152.0.7977.64"
CHROME_SHA256="8b592f066af71f054aab2cc80fc26f73c775c6d44ebb99d16ade924b24756c2e"
CHROME_URL="https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chrome-linux64.zip"

DATABASE="odoo_ci"
RESTORE_DATABASE="odoo_ci_restore"
DB_USER="odoo_ci"
SAFE_RUN_ID="$(
  printf '%s' "${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-$$" \
    | tr -cd 'A-Za-z0-9_.-'
)"
NETWORK="codestra-odoo-ci-${SAFE_RUN_ID}"
DB_CONTAINER="codestra-postgres-ci-${SAFE_RUN_ID}"
ODOO_DATA_VOLUME="codestra-odoo-data-${SAFE_RUN_ID}"
ODOO_TEST_IMAGE="codestra-odoo-ci-browser:${SAFE_RUN_ID}"
SECRET_DIR=""
CI_PYTHON_DIR=""
CI_BROWSER_DIR=""
TEST_LOG=""
UPGRADE_LOG=""
BACKUP_FILE=""
FILESTORE_BACKUP_FILE=""

remove_ci_temp_dir() {
  local candidate="$1"
  local resolved_candidate
  resolved_candidate="$(realpath -- "$candidate" 2>/dev/null || true)"
  if [[ "$resolved_candidate" == /tmp/tmp.* ]]; then
    sudo rm -rf -- "$resolved_candidate" >/dev/null 2>&1 || true
  fi
}

cleanup() {
  docker rm -f "$DB_CONTAINER" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
  docker volume rm "$ODOO_DATA_VOLUME" >/dev/null 2>&1 || true
  docker image rm -f "$ODOO_TEST_IMAGE" >/dev/null 2>&1 || true
  if [[ -n "$SECRET_DIR" && -d "$SECRET_DIR" ]]; then
    remove_ci_temp_dir "$SECRET_DIR"
  fi
  if [[ -n "$TEST_LOG" && -f "$TEST_LOG" ]]; then
    rm -f "$TEST_LOG" >/dev/null 2>&1 || true
  fi
  if [[ -n "$UPGRADE_LOG" && -f "$UPGRADE_LOG" ]]; then
    rm -f "$UPGRADE_LOG" >/dev/null 2>&1 || true
  fi
  if [[ -n "$BACKUP_FILE" && -f "$BACKUP_FILE" ]]; then
    rm -f "$BACKUP_FILE" >/dev/null 2>&1 || true
  fi
  if [[ -n "$FILESTORE_BACKUP_FILE" && -f "$FILESTORE_BACKUP_FILE" ]]; then
    rm -f "$FILESTORE_BACKUP_FILE" >/dev/null 2>&1 || true
  fi
  if [[ -n "$CI_PYTHON_DIR" && -d "$CI_PYTHON_DIR" ]]; then
    remove_ci_temp_dir "$CI_PYTHON_DIR"
  fi
  if [[ -n "$CI_BROWSER_DIR" && -d "$CI_BROWSER_DIR" ]]; then
    remove_ci_temp_dir "$CI_BROWSER_DIR"
  fi
}
trap cleanup EXIT

command -v docker >/dev/null 2>&1 || {
  printf 'ERROR=DOCKER_NOT_AVAILABLE\n' >&2
  exit 1
}
command -v curl >/dev/null 2>&1 || {
  printf 'ERROR=CURL_NOT_AVAILABLE\n' >&2
  exit 1
}

for image in "$ODOO_IMAGE" "$POSTGRES_IMAGE"; do
  [[ "$image" == *@sha256:* ]] || {
    printf 'ERROR=CI_IMAGE_NOT_DIGEST_PINNED:%s\n' "$image" >&2
    exit 1
  }
done

DB_PASSWORD="$(
  python3 -I - <<'PY'
import secrets
# Odoo forwards this value to argparse as a separate --db_password argument.
# A leading dash can be mistaken for an option; retain all 288 random bits.
print("ci_" + secrets.token_urlsafe(36))
PY
)"
ADMIN_PASSWORD="$(
  python3 -I - <<'PY'
import secrets
print(secrets.token_urlsafe(40))
PY
)"

printf '==> Pulling immutable Odoo and PostgreSQL images\n'
docker image inspect "$ODOO_IMAGE" >/dev/null 2>&1 || docker pull "$ODOO_IMAGE"
docker image inspect "$POSTGRES_IMAGE" >/dev/null 2>&1 || docker pull "$POSTGRES_IMAGE"

printf 'ODOO_IMAGE=%s\n' "$ODOO_IMAGE"
printf 'POSTGRES_IMAGE=%s\n' "$POSTGRES_IMAGE"
docker image inspect \
  --format 'ODOO_IMAGE_ID={{.Id}}' \
  "$ODOO_IMAGE"
docker image inspect \
  --format 'POSTGRES_IMAGE_ID={{.Id}}' \
  "$POSTGRES_IMAGE"

printf '==> Installing the hash-pinned Odoo browser-test dependency\n'
CI_PYTHON_DIR="$(mktemp -d)"
python3 -I -m pip install \
  --disable-pip-version-check \
  --no-deps \
  --require-hashes \
  --target "$CI_PYTHON_DIR" \
  -r scripts/requirements-odoo-ci.txt
chmod -R a+rX "$CI_PYTHON_DIR"
docker run --rm \
  --entrypoint python3 \
  -e PYTHONPATH=/opt/codestra-ci-python \
  -v "$CI_PYTHON_DIR:/opt/codestra-ci-python:ro" \
  "$ODOO_IMAGE" \
  -c 'import websocket; assert websocket.__version__ == "1.8.0"; print("ODOO_BROWSER_DEPENDENCY=PASS")'

printf '==> Building the checksum-verified Odoo browser-test image\n'
CI_BROWSER_DIR="$(mktemp -d)"
curl \
  --fail \
  --location \
  --retry 3 \
  --show-error \
  --silent \
  --output "$CI_BROWSER_DIR/chrome-linux64.zip" \
  "$CHROME_URL"
printf '%s  %s\n' \
  "$CHROME_SHA256" \
  "$CI_BROWSER_DIR/chrome-linux64.zip" \
  | sha256sum --check --strict
docker build \
  --pull=false \
  --build-arg "ODOO_IMAGE=$ODOO_IMAGE" \
  --file scripts/odoo-ci-browser.Dockerfile \
  --tag "$ODOO_TEST_IMAGE" \
  "$CI_BROWSER_DIR"
docker run --rm \
  --entrypoint sh \
  "$ODOO_TEST_IMAGE" \
  -c 'test -x "$ODOO_BROWSER_BIN"; "$ODOO_BROWSER_BIN" --version; printf "ODOO_BROWSER_BINARY=PASS\n"'
printf 'CHROME_FOR_TESTING_VERSION=%s\n' "$CHROME_VERSION"
printf 'CHROME_FOR_TESTING_SHA256=%s\n' "$CHROME_SHA256"

docker network create --subnet="${ODOO_CI_SUBNET:-10.254.250.0/28}" "$NETWORK" >/dev/null
docker volume create "$ODOO_DATA_VOLUME" >/dev/null

printf '==> Starting isolated PostgreSQL\n'
docker run -d \
  --name "$DB_CONTAINER" \
  --network "$NETWORK" \
  --network-alias db \
  -e POSTGRES_USER="$DB_USER" \
  -e POSTGRES_PASSWORD="$DB_PASSWORD" \
  -e POSTGRES_DB="$DATABASE" \
  --health-cmd="pg_isready -U $DB_USER -d $DATABASE" \
  --health-interval=2s \
  --health-timeout=3s \
  --health-retries=45 \
  "$POSTGRES_IMAGE" \
  >/dev/null

for _ in $(seq 1 60); do
  db_health="$(
    docker inspect \
      --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "$DB_CONTAINER"
  )"
  if [[ "$db_health" == "healthy" ]]; then
    break
  fi
  if [[ "$db_health" == "unhealthy" || "$db_health" == "exited" ]]; then
    docker logs "$DB_CONTAINER" >&2 || true
    printf 'ERROR=POSTGRESQL_CI_STARTUP_FAILED:%s\n' "$db_health" >&2
    exit 1
  fi
  sleep 2
done

if [[ "$(docker inspect --format '{{.State.Health.Status}}' "$DB_CONTAINER")" != "healthy" ]]; then
  docker logs "$DB_CONTAINER" >&2 || true
  printf 'ERROR=POSTGRESQL_CI_READINESS_TIMEOUT\n' >&2
  exit 1
fi

printf 'POSTGRESQL_CONTAINER_HEALTH=PASS\n'
docker exec "$DB_CONTAINER" postgres --version

printf '==> Installing and testing every reviewed custom module on Odoo 19\n'
mapfile -t module_names < <(
  find custom-addons \
    -mindepth 2 \
    -maxdepth 2 \
    -type f \
    -name '__manifest__.py' \
    -printf '%h\n' \
    | xargs -r -n1 basename \
    | sort -u
)
if ((${#module_names[@]} == 0)); then
  printf 'ERROR=NO_CUSTOM_MODULES_FOR_RUNTIME_TEST\n' >&2
  exit 1
fi
module_csv="$(IFS=,; printf '%s' "${module_names[*]}")"
ODOO_CI_ADDONS_PATH="${ODOO_CI_ADDONS_PATH:-/usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons}"
test_tags=""
for module_name in "${module_names[@]}"; do
  if [[ -n "$test_tags" ]]; then
    test_tags+=","
  fi
  test_tags+="/${module_name}"
done
if [[ -n "${ODOO_CI_TEST_TAGS:-}" ]]; then
  test_tags="$ODOO_CI_TEST_TAGS"
fi

TEST_LOG="$(mktemp)"
if ! docker run --rm \
  --network "$NETWORK" \
  -e PYTHONPATH=/opt/codestra-ci-python \
  -e HOST=db \
  -e PORT=5432 \
  -e USER="$DB_USER" \
  -e PASSWORD="$DB_PASSWORD" \
  -v "$CI_PYTHON_DIR:/opt/codestra-ci-python:ro" \
  -v "$ROOT_DIR/custom-addons:/mnt/extra-addons:ro" \
  -v "$ROOT_DIR/contracts:/mnt/contracts:ro" \
  -v "$ODOO_DATA_VOLUME:/var/lib/odoo" \
  "$ODOO_TEST_IMAGE" \
  -- \
  --addons-path="$ODOO_CI_ADDONS_PATH" \
  -d "$DATABASE" \
  --db-filter="^${DATABASE}$" \
  --init="$module_csv" \
  --without-demo \
  --test-enable \
  --test-tags="$test_tags" \
  --stop-after-init \
  --workers=0 \
  --http-interface=127.0.0.1 \
  --log-level=test \
  2>&1 | tee "$TEST_LOG"; then
  printf 'ERROR=ODOO_MODULE_INSTALL_OR_TEST_FAILED\n' >&2
  exit 1
fi

if grep -Fq "Internal Error:" "$TEST_LOG"; then
  printf 'ERROR=ODOO_ASSET_COMPILATION_INTERNAL_ERROR\n' >&2
  exit 1
fi
if grep -Fq "invalid boolean value" "$TEST_LOG"; then
  printf 'ERROR=ODOO_INVALID_CLI_BOOLEAN\n' >&2
  exit 1
fi
if ! grep -Eq "0 failed, 0 error\(s\)" "$TEST_LOG"; then
  printf 'ERROR=ODOO_TEST_SUCCESS_MARKER_MISSING\n' >&2
  exit 1
fi
if grep -Eq '[1-9][0-9]* skipped|skipped Test' "$TEST_LOG"; then
  printf 'ERROR=ODOO_TEST_SKIP_DETECTED\n' >&2
  exit 1
fi
if ! grep -Fq "APPOINTMENT_POPOUT_BROWSER=PASS" "$TEST_LOG"; then
  printf 'ERROR=ODOO_POPOUT_BROWSER_SUCCESS_MARKER_MISSING\n' >&2
  exit 1
fi

printf 'ODOO_ASSET_COMPILATION=PASS\n'
printf 'ODOO_HEADLESS_BROWSER=PASS\n'
printf 'ODOO_BROWSER_SKIPS=0\n'
printf 'ODOO_MODULE_INSTALL_AND_TEST=PASS\n'
printf 'CUSTOM_MODULES_TESTED=%s\n' "${#module_names[@]}"

printf '==> Verifying Kyqra delivery with independent committed cursors\n'
docker run --rm -i \
  --network "$NETWORK" \
  -e HOST=db \
  -e PORT=5432 \
  -e USER="$DB_USER" \
  -e PASSWORD="$DB_PASSWORD" \
  -v "$ROOT_DIR/custom-addons:/mnt/extra-addons:ro" \
  -v "$ODOO_DATA_VOLUME:/var/lib/odoo" \
  "$ODOO_TEST_IMAGE" \
  odoo shell -d "$DATABASE" --no-http \
  < scripts/test_kyqra_concurrency.py

# Use a normal registry after TransactionCase releases its process-wide lock.
docker run --rm -i \
  --network "$NETWORK" \
  -e HOST=db -e PORT=5432 -e USER="$DB_USER" -e PASSWORD="$DB_PASSWORD" \
  -v "$ROOT_DIR/custom-addons:/mnt/extra-addons:ro" \
  -v "$ODOO_DATA_VOLUME:/var/lib/odoo" \
  "$ODOO_IMAGE" odoo shell \
  --addons-path="$ODOO_CI_ADDONS_PATH" -d "$DATABASE" \
  --no-http --workers=0 < scripts/test_observability_concurrency.py

printf '==> Updating every custom module on the disposable database\n'
UPGRADE_LOG="$(mktemp)"
if ! docker run --rm \
  --network "$NETWORK" \
  -e HOST=db \
  -e PORT=5432 \
  -e USER="$DB_USER" \
  -e PASSWORD="$DB_PASSWORD" \
  -v "$ROOT_DIR/custom-addons:/mnt/extra-addons:ro" \
  -v "$ODOO_DATA_VOLUME:/var/lib/odoo" \
  "$ODOO_IMAGE" \
  -- \
  --addons-path="$ODOO_CI_ADDONS_PATH" \
  -d "$DATABASE" \
  --db-filter="^${DATABASE}$" \
  --update="$module_csv" \
  --without-demo \
  --stop-after-init \
  --workers=0 \
  --http-interface=127.0.0.1 \
  --log-level=test \
  2>&1 | tee "$UPGRADE_LOG"; then
  printf 'ERROR=ODOO_MODULE_UPGRADE_FAILED\n' >&2
  exit 1
fi
if grep -Fq "Internal Error:" "$UPGRADE_LOG"; then
  printf 'ERROR=ODOO_UPGRADE_ASSET_COMPILATION_INTERNAL_ERROR\n' >&2
  exit 1
fi
rm -f "$UPGRADE_LOG"
UPGRADE_LOG=""
printf 'ODOO_MODULE_UPGRADE=PASS\n'

printf '==> Exercising fail-closed administrator provisioning\n'
SECRET_DIR="$(mktemp -d)"
ADMIN_SECRET="$SECRET_DIR/codestra_odoo_admin_password"
umask 077
printf '%s' "$ADMIN_PASSWORD" > "$ADMIN_SECRET"
chmod 0400 "$ADMIN_SECRET"

mapfile -t odoo_ids < <(
  docker run --rm \
    --entrypoint sh \
    "$ODOO_IMAGE" \
    -c 'id -u; id -g'
)
if ((${#odoo_ids[@]} != 2)); then
  printf 'ERROR=UNABLE_TO_RESOLVE_ODOO_CONTAINER_IDENTITY\n' >&2
  exit 1
fi
sudo chown "${odoo_ids[0]}:${odoo_ids[1]}" "$ADMIN_SECRET"

docker run --rm -i \
  --network "$NETWORK" \
  -e HOST=db \
  -e PORT=5432 \
  -e USER="$DB_USER" \
  -e PASSWORD="$DB_PASSWORD" \
  -e ODOO_ADMIN_LOGIN=appolon1908@gmail.com \
  -e ODOO_ADMIN_BOOTSTRAP_APPLY=YES \
  -e ODOO_ADMIN_PASSWORD_FILE=/run/secrets/codestra_odoo_admin_password \
  -v "$ROOT_DIR/custom-addons:/mnt/extra-addons:ro" \
  -v "$ROOT_DIR/scripts/ensure_codestra_admin.py:/opt/codestra/ensure_codestra_admin.py:ro" \
  -v "$ADMIN_SECRET:/run/secrets/codestra_odoo_admin_password:ro" \
  -v "$ODOO_DATA_VOLUME:/var/lib/odoo" \
  "$ODOO_IMAGE" \
  -- \
  --addons-path="$ODOO_CI_ADDONS_PATH" \
  shell -d "$DATABASE" --no-http \
  < "$ROOT_DIR/scripts/ensure_codestra_admin.py"

printf 'ADMINISTRATOR_PROVISIONING_RUNTIME_TEST=PASS\n'

run_database_audits() {
  local target_database="$1"
  local schema_ok

  printf '==> Auditing database, administrator, and module state in %s\n' "$target_database"
  docker run --rm -i \
    --network "$NETWORK" \
    -e HOST=db \
    -e PORT=5432 \
    -e USER="$DB_USER" \
    -e PASSWORD="$DB_PASSWORD" \
    -e EXPECTED_ADMIN_LOGIN=appolon1908@gmail.com \
    -e EXPECTED_ODOO_MODULES="$module_csv" \
    -v "$ROOT_DIR/custom-addons:/mnt/extra-addons:ro" \
    -v "$ODOO_DATA_VOLUME:/var/lib/odoo" \
    "$ODOO_IMAGE" \
    -- \
  --addons-path="$ODOO_CI_ADDONS_PATH" \
    shell -d "$target_database" --no-http \
    < "$ROOT_DIR/scripts/audit_odoo_state.py"

  schema_ok="$(
    docker exec \
      -e PGPASSWORD="$DB_PASSWORD" \
      "$DB_CONTAINER" \
      psql -X -v ON_ERROR_STOP=1 \
        -U "$DB_USER" \
        -d "$target_database" \
        -Atqc "
          SELECT CASE
            WHEN to_regclass('public.ir_module_module') IS NOT NULL
             AND to_regclass('public.res_users') IS NOT NULL
             AND to_regclass('public.ir_ui_view') IS NOT NULL
            THEN 1 ELSE 0
          END
        "
  )"
  if [[ "$schema_ok" != "1" ]]; then
    printf 'ERROR=ODOO_CI_SCHEMA_AUDIT_FAILED:%s\n' "$target_database" >&2
    exit 1
  fi
}

run_database_audits "$DATABASE"

printf '==> Creating a synthetic filestore object for paired restore verification\n'
sentinel_output="$(docker run --rm -i \
  --network "$NETWORK" \
  -e HOST=db \
  -e PORT=5432 \
  -e USER="$DB_USER" \
  -e PASSWORD="$DB_PASSWORD" \
  -v "$ROOT_DIR/custom-addons:/mnt/extra-addons:ro" \
  -v "$ROOT_DIR/scripts/create_filestore_restore_sentinel.py:/opt/codestra/create_filestore_restore_sentinel.py:ro" \
  -v "$ODOO_DATA_VOLUME:/var/lib/odoo" \
  "$ODOO_IMAGE" \
  -- \
  --addons-path="$ODOO_CI_ADDONS_PATH" \
  shell -d "$DATABASE" --no-http \
  < "$ROOT_DIR/scripts/create_filestore_restore_sentinel.py" 2>&1)"
printf '%s\n' "$sentinel_output"
FILESTORE_DATABASE_DIR="$(
  printf '%s\n' "$sentinel_output" \
    | sed -n 's/^FILESTORE_DATABASE_DIR=//p'
)"
case "$FILESTORE_DATABASE_DIR" in
  /var/lib/odoo/*/filestore/"$DATABASE"|/var/lib/odoo/filestore/"$DATABASE") ;;
  *)
    printf 'ERROR=UNSAFE_OR_MISSING_FILESTORE_DATABASE_DIR:%s\n' \
      "$FILESTORE_DATABASE_DIR" >&2
    exit 1
    ;;
esac
RESTORE_FILESTORE_DATABASE_DIR="${FILESTORE_DATABASE_DIR%/$DATABASE}/$RESTORE_DATABASE"

printf '==> Capturing a paired database and filestore recovery point\n'
BACKUP_FILE="$(mktemp --suffix=.dump)"
FILESTORE_BACKUP_FILE="$(mktemp --suffix=.tar)"
docker exec \
  -e PGPASSWORD="$DB_PASSWORD" \
  "$DB_CONTAINER" \
  pg_dump --format=custom --no-owner -U "$DB_USER" -d "$DATABASE" \
  > "$BACKUP_FILE"
if [[ ! -s "$BACKUP_FILE" ]]; then
  printf 'ERROR=ODOO_CI_BACKUP_IS_EMPTY\n' >&2
  exit 1
fi
docker run --rm \
  --entrypoint sh \
  -e SOURCE_FILESTORE_DATABASE_DIR="$FILESTORE_DATABASE_DIR" \
  -v "$ODOO_DATA_VOLUME:/var/lib/odoo:ro" \
  "$ODOO_IMAGE" \
  -c 'test -d "$SOURCE_FILESTORE_DATABASE_DIR" && tar -C "$SOURCE_FILESTORE_DATABASE_DIR" -cf - .' \
  > "$FILESTORE_BACKUP_FILE"
if [[ ! -s "$FILESTORE_BACKUP_FILE" ]]; then
  printf 'ERROR=ODOO_CI_FILESTORE_BACKUP_IS_EMPTY\n' >&2
  exit 1
fi

printf '==> Restoring the paired recovery point under an isolated database name\n'
docker exec \
  -e PGPASSWORD="$DB_PASSWORD" \
  "$DB_CONTAINER" \
  createdb -U "$DB_USER" "$RESTORE_DATABASE"
docker exec -i \
  -e PGPASSWORD="$DB_PASSWORD" \
  "$DB_CONTAINER" \
  pg_restore --exit-on-error --no-owner -U "$DB_USER" -d "$RESTORE_DATABASE" \
  < "$BACKUP_FILE"
docker run --rm \
  --entrypoint sh \
  --user 0:0 \
  -e ODOO_UID="${odoo_ids[0]}" \
  -e ODOO_GID="${odoo_ids[1]}" \
  -e RESTORE_FILESTORE_DATABASE_DIR="$RESTORE_FILESTORE_DATABASE_DIR" \
  -v "$FILESTORE_BACKUP_FILE:/tmp/codestra-filestore-backup.tar:ro" \
  -v "$ODOO_DATA_VOLUME:/var/lib/odoo" \
  "$ODOO_IMAGE" \
  -c 'set -eu; test ! -e "$RESTORE_FILESTORE_DATABASE_DIR"; mkdir -p "$RESTORE_FILESTORE_DATABASE_DIR"; tar -C "$RESTORE_FILESTORE_DATABASE_DIR" -xf /tmp/codestra-filestore-backup.tar; chown -R "$ODOO_UID:$ODOO_GID" "$RESTORE_FILESTORE_DATABASE_DIR"'

run_database_audits "$RESTORE_DATABASE"
docker run --rm -i \
  --network "$NETWORK" \
  -e HOST=db \
  -e PORT=5432 \
  -e USER="$DB_USER" \
  -e PASSWORD="$DB_PASSWORD" \
  -v "$ROOT_DIR/custom-addons:/mnt/extra-addons:ro" \
  -v "$ROOT_DIR/scripts/audit_filestore_restore.py:/opt/codestra/audit_filestore_restore.py:ro" \
  -v "$ODOO_DATA_VOLUME:/var/lib/odoo" \
  "$ODOO_IMAGE" \
  -- \
  --addons-path="$ODOO_CI_ADDONS_PATH" \
  shell -d "$RESTORE_DATABASE" --no-http \
  < "$ROOT_DIR/scripts/audit_filestore_restore.py"

printf 'POSTGRESQL_SCHEMA_AUDIT=PASS\n'
printf 'ADMINISTRATOR_STATE_AUDIT=PASS\n'
printf 'MODULE_STATE_AUDIT=PASS\n'
printf 'DISPOSABLE_BACKUP_RESTORE=PASS\n'
printf 'DATABASE_BACKUP_RESTORE=PASS\n'
printf 'FILESTORE_BACKUP_RESTORE=PASS\n'
printf 'PAIRED_DATABASE_FILESTORE_RESTORE=PASS\n'
printf 'ODOO_POSTGRESQL_RUNTIME_CI=PASS\n'
