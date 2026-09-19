#!/usr/bin/env python3
"""Fail CI if the prepared Odoo → Middleware V3 command mapping drifts from source.

The mapping (contracts/middleware-v3-command-mapping.v1.json) is preparation
only: it records, for every Middleware path the custom addons call today, how
that call maps onto the V3 command kernel. It stays dark until the Middleware
V3 route contract is frozen. This validator proves three things from source:

* the mapping is still marked pending and never authorises a runtime apply;
* every ``current_path`` is really referenced by a non-test addon file named in
  the mapping, and no non-test addon references a Middleware path the mapping
  does not know (the mapping cannot go stale silently);
* no addon calls the V3 kernel routes yet, and no addon targets the retired
  Middleware host alias.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "middleware-v3-command-mapping.v1.json"
ADDONS = ROOT / "custom-addons"
KERNEL_PATHS = (
    "/platform/v1/commands",
    "/platform/v1/operations/",
    "/platform/v1/kernel/describe",
)
RETIRED_UPSTREAM_ALIASES = ("appolon-middleware-integration-api",)
# Middleware paths a governed addon may call. Anything else under these prefixes
# that is not in the mapping is drift.
MIDDLEWARE_PREFIXES = ("/api/v1/", "/platform/v1/", "/v1/", "/v2/")
PATH_TOKEN = re.compile(r"['\"](/(?:api/v1|platform/v1|v1|v2)/[A-Za-z0-9_{}/.-]*)['\"]")
# Odoo's own inbound controllers and Odoo-served routes are not Middleware targets.
NOT_MIDDLEWARE = ("/api/v1/integration/", "/api/v1/agent/", "/v1/identities", "/codestra/")


def fail(message: str) -> None:
    raise SystemExit(f"MIDDLEWARE_V3_COMMAND_MAPPING=FAIL {message}")


def template(path: str) -> str:
    return re.sub(r"\{[^{}]*\}", "{}", path)


def addon_sources() -> list[Path]:
    return sorted(
        p
        for p in ADDONS.rglob("*.py")
        if "/tests/" not in p.as_posix() and "/controllers/" not in p.as_posix()
    )


def main() -> int:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    if contract.get("status") != "V3_PENDING_FINAL_MIDDLEWARE_CONTRACT":
        fail("mapping is not pending the final Middleware V3 contract")
    if contract.get("V3_PENDING_FINAL_MIDDLEWARE_CONTRACT") is not True:
        fail("pending flag cleared")
    if contract.get("runtime_apply_authorized") is not False:
        fail("runtime apply must stay unauthorised")
    if contract["flow"]["canonical_upstream"] != "middleware-integration-api:8095":
        fail("canonical upstream drift")
    if contract["flow"]["browser_privileged_direct_bridge"] is not False:
        fail("browser privileged direct bridge must stay forbidden")
    if contract["middleware_to_odoo"]["n8n_direct_business_effect_bypass"] is not False:
        fail("n8n business-effect bypass must stay forbidden")
    routes = contract["v3_kernel_routes"]
    if routes["submit"] != {"method": "POST", "path": "/platform/v1/commands", "response": "202 + Location: /platform/v1/operations/{operation_id}"}:
        fail("V3 submit route drift")

    mapped: dict[str, dict] = {}
    for row in contract["odoo_to_middleware"]:
        key = template(row["current_path"])
        if key in mapped:
            fail(f"duplicate current_path {row['current_path']}")
        mapped[key] = row
        if row["v3_state"] == "MAPS_TO_V3_COMMAND":
            if not row.get("v3_command_type") or not row.get("v3_adapter"):
                fail(f"{row['current_path']} maps to V3 without a command type or adapter")
            if row["effectful"] is not True:
                fail(f"{row['current_path']} maps to a command but is not effectful")
        elif row.get("v3_command_type"):
            fail(f"{row['current_path']} carries a command type but does not map to V3")
        if row["effectful"] and row["current_auth"] not in ("keycloak-client-credentials", "unknown"):
            fail(f"{row['current_path']} is effectful without Keycloak client credentials")
        for source in row["source"]:
            source_path = ROOT / source
            if not source_path.is_file():
                fail(f"{row['current_path']} names a missing source file {source}")
            text = source_path.read_text(encoding="utf-8", errors="replace")
            literal = row.get("source_literal") or row["current_path"].split("{", 1)[0]
            if literal not in text:
                fail(f"{source} no longer references {literal}")

    seen: dict[str, set[str]] = {}
    for source in addon_sources():
        text = source.read_text(encoding="utf-8", errors="replace")
        for hit in PATH_TOKEN.findall(text):
            if hit.startswith(NOT_MIDDLEWARE) or "codestra/" in hit:
                continue
            for kernel in KERNEL_PATHS:
                if hit.startswith(kernel):
                    fail(f"{source.relative_to(ROOT).as_posix()} already calls the V3 kernel route {hit}")
            seen.setdefault(template(hit), set()).add(source.relative_to(ROOT).as_posix())
        for retired in RETIRED_UPSTREAM_ALIASES:
            if retired in text:
                fail(f"{source.relative_to(ROOT).as_posix()} targets the retired upstream alias {retired}")

    unknown = sorted(k for k in seen if k not in mapped and k.startswith(MIDDLEWARE_PREFIXES) and not k.startswith("/v1/identities"))
    # Paths that are route fragments composed at runtime (no method call) show up as
    # prefixes of mapped rows; treat a seen path as known when a mapped row starts with it.
    unknown = [k for k in unknown if not any(m.startswith(k.rstrip("/")) for m in mapped)]
    if unknown:
        fail("addons reference Middleware paths the mapping does not know: " + ", ".join(unknown))

    print("MIDDLEWARE_V3_COMMAND_MAPPING=PASS")
    print(f"MAPPED_PATHS={len(mapped)} MAPS_TO_V3={sum(1 for r in mapped.values() if r['v3_state'] == 'MAPS_TO_V3_COMMAND')} V3_KERNEL_CALLS_IN_ADDONS=0 RETIRED_ALIAS_REFERENCES=0")
    print("RUNTIME_APPLY_AUTHORIZED=NO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
