#!/usr/bin/env python3
"""Generate deterministic source-reconciliation ledgers from Git and addon metadata."""

from __future__ import annotations

import ast
import sys
import csv
import hashlib
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ADDONS = ROOT / "custom-addons"
OUT = ROOT / "docs" / "reconciliation"
REPO = "appolon1908-hue/Odoo"
MAIN = "origin/main"
SOURCE_BRANCH = "feat/cc-compliance-audit"
SOURCE_REF = f"origin/{SOURCE_BRANCH}"


def run(*args: str, check: bool = True) -> str:
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"command failed: {' '.join(args)}\n{result.stderr}")
    return result.stdout.strip()


def git(*args: str, check: bool = True) -> str:
    return run("git", *args, check=check)


def gh_json(*args: str) -> Any:
    output = run("gh", *args)
    return json.loads(output) if output else None


def write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def module_dirs_at(ref: str) -> list[str]:
    output = git("ls-tree", "-d", "--name-only", f"{ref}:custom-addons", check=False)
    return sorted(output.splitlines()) if output else []


def changed_files(ref: str) -> list[str]:
    output = git("diff", "--name-only", f"{MAIN}..{ref}", check=False)
    return output.splitlines() if output else []


def classify_branch(name: str, sha: str, unique_tip_count: int) -> str:
    if name == "reconcile/odoo-canonical-source-v1":
        return "CANONICAL_CANDIDATE"
    if name == "import/server-odoo-20260828":
        return "SERVER_CAPTURE"
    if name == SOURCE_BRANCH:
        return "SUPERSEDED"
    if name == "main":
        return "DOCUMENTATION_ONLY"
    if unique_tip_count > 1:
        return "DUPLICATE"
    if name == "feat/cc-wfm-reporting" or git(
        "merge-base", "--is-ancestor", f"origin/{name}", SOURCE_REF, check=False
    ) == "":
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", f"origin/{name}", SOURCE_REF],
            cwd=ROOT,
        )
        if result.returncode == 0 and name != SOURCE_BRANCH:
            return "SUPERSEDED"
    if name.startswith("test/"):
        return "TEST_ONLY"
    if name.startswith("release/") or name == "docs/cc-production-gates":
        return "RELEASE_EVIDENCE_ONLY"
    if name.startswith("docs/") or name.startswith("architecture/") or name.startswith("integration/n8n"):
        return "DOCUMENTATION_ONLY"
    if name == "chore/odoo-gitops-bootstrap":
        return "SUPERSEDED"
    if name.startswith(("feat/", "feature/", "fix/", "crm/", "integration/")):
        return "NEEDS_SELECTIVE_PORT"
    return "ABANDON"


def generate_branch_ledger() -> None:
    prs = gh_json(
        "pr", "list", "--repo", REPO, "--state", "all", "--limit", "200", "--json",
        "number,state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid,title,statusCheckRollup",
    )
    pr_by_head = {item["headRefName"]: item for item in prs}
    branches = [
        value.removeprefix("origin/")
        for value in git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin").splitlines()
        if value != "origin/HEAD"
    ]
    sha_by_branch = {name: git("rev-parse", f"origin/{name}") for name in branches}
    names_by_sha: dict[str, list[str]] = defaultdict(list)
    for name, sha in sha_by_branch.items():
        names_by_sha[sha].append(name)

    ahead = {
        name: int(git("rev-list", "--count", f"{MAIN}..origin/{name}"))
        for name in branches
    }
    rows: list[dict[str, Any]] = []
    for name in sorted(branches):
        sha = sha_by_branch[name]
        pr = pr_by_head.get(name)
        files = changed_files(f"origin/{name}")
        checks = []
        if pr:
            for check in pr.get("statusCheckRollup") or []:
                checks.append(f"{check.get('name')}:{check.get('conclusion') or check.get('status')}")
        if not checks:
            check_data = gh_json("api", f"repos/{REPO}/commits/{sha}/check-runs")
            checks = [
                f"{check.get('name')}:{check.get('conclusion') or check.get('status')}"
                for check in check_data.get("check_runs", [])
            ]
        latest = git("log", "-1", "--format=%s", f"origin/{name}")
        classification = classify_branch(name, sha, len(names_by_sha[sha]))
        rows.append({
            "branch": name,
            "head_sha": sha,
            "base_branch": pr.get("baseRefName", "main") if pr else "main",
            "base_sha": pr.get("baseRefOid", git("merge-base", MAIN, f"origin/{name}")) if pr else git("merge-base", MAIN, f"origin/{name}"),
            "pr_number": pr.get("number", "") if pr else "",
            "pr_state": pr.get("state", "NO_PR") if pr else "NO_PR",
            "draft": str(pr.get("isDraft", "")).upper() if pr else "",
            "commits_ahead_main": ahead[name],
            "latest_ci": ";".join(checks) if checks else "NO_CHECKS",
            "module_count": len(module_dirs_at(f"origin/{name}")),
            "purpose": pr.get("title", latest) if pr else latest,
            "contains_application_code": "YES" if any(path.startswith("custom-addons/") for path in files) else "NO",
            "contains_migrations": "YES" if any("/migrations/" in path for path in files) else "NO",
            "contains_security_changes": "YES" if any("/security/" in path or "security" in path.lower() for path in files) else "NO",
            "contains_api_changes": "YES" if any("/controllers/" in path or path.startswith("api/") for path in files) else "NO",
            "contains_business_data": "YES" if any("/data/" in path for path in files) else "NO",
            "classification": classification,
            "canonical_source_candidate": "YES" if classification == "CANONICAL_CANDIDATE" else "NO",
            "notes": "same tip: " + ";".join(sorted(names_by_sha[sha])) if len(names_by_sha[sha]) > 1 else "",
        })
    columns = list(rows[0])
    write_csv(OUT / "BRANCH-PR-LEDGER.csv", columns, rows)


def manifest(module: Path) -> dict[str, Any]:
    value = ast.literal_eval((module / "__manifest__.py").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"invalid manifest: {module}")
    return value


def ownership(name: str) -> str:
    rules = (
        ("COMPATIBILITY", ("certification",)),
        ("AI_ADVISORY", ("_ai_", "ai_")),
        ("TELEPHONY_PROJECTION", ("vicidial", "telephony", "ivr", "recording")),
        ("EMAIL_PROJECTION", ("mail",)),
        ("PORTAL", ("portal",)),
        ("REPORTING", ("report", "analytics", "revenue", "data_quality")),
        ("WFM", ("wfm", "workforce", "training", "onboarding")),
        ("COMPLIANCE", ("compliance", "audit", "quality")),
        ("CALLBACK_APPOINTMENT", ("appointment", "_calls")),
        ("IDENTITY", ("identity", "login")),
        ("CAMPAIGN", ("campaign", "disposition", "staging")),
        ("CRM", ("crm", "lead", "customer", "case", "client", "interaction")),
        ("INTEGRATION_BOUNDARY", ("integration", "middleware", "orchestration", "automation", "social")),
    )
    for label, needles in rules:
        if any(needle in name for needle in needles):
            return label
    return "CORE_BUSINESS"


def model_names(module: Path) -> list[str]:
    names: set[str] = set()
    for path in module.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            declared_name: str | None = None
            inherited: set[str] = set()
            for statement in node.body:
                if not isinstance(statement, ast.Assign):
                    continue
                targets = [target.id for target in statement.targets if isinstance(target, ast.Name)]
                if "_name" in targets and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                    declared_name = statement.value.value
                if "_inherit" in targets:
                    value = literal(statement.value)
                    if isinstance(value, str):
                        inherited.add(value)
                    elif isinstance(value, list):
                        inherited.update(item for item in value if isinstance(item, str))
            # Odoo modules sometimes repeat _name while inheriting the same
            # model. Treat that as an extension, not a second owner.
            if declared_name and declared_name not in inherited:
                names.add(declared_name)
    return sorted(names)


def generate_module_inventory() -> None:
    rows: list[dict[str, Any]] = []
    for module in sorted(path for path in ADDONS.iterdir() if path.is_dir() and not path.name.startswith(".")):
        data = manifest(module)
        files = sorted(
            (path for path in module.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(module).as_posix(),
        )
        relative_paths = {
            path: path.relative_to(module).as_posix() for path in files
        }
        text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in files if path.suffix in {".py", ".xml", ".csv", ".json", ".yaml", ".yml"}
        )
        source_commit = git("log", "-1", "--format=%H", SOURCE_REF, "--", f"custom-addons/{module.name}")
        rows.append({
            "module": module.name,
            "technical_name": module.name,
            "manifest_version": data.get("version", ""),
            "tree_sha": git("rev-parse", f"HEAD:custom-addons/{module.name}"),
            "source_branch": SOURCE_BRANCH,
            "source_commit": source_commit,
            "dependencies": ";".join(data.get("depends", [])),
            "model_owner": ";".join(model_names(module)),
            "views": ";".join(relative_paths[path] for path in files if "/views/" in f"/{relative_paths[path]}"),
            "security_files": ";".join(relative_paths[path] for path in files if "/security/" in f"/{relative_paths[path]}"),
            "migrations": sum("/migrations/" in f"/{relative_paths[path]}" and path.suffix == ".py" for path in files),
            "tests": sum("/tests/test_" in f"/{relative_paths[path]}" and path.suffix == ".py" for path in files),
            "external_api": "YES" if any("/controllers/" in f"/{relative_paths[path]}" and path.suffix == ".py" for path in files) else "NO",
            "scheduled_jobs": "YES" if "ir.cron" in text else "NO",
            "outbox": "YES" if re.search(r"\boutbox\b", text, re.I) else "NO",
            "inbox": "YES" if re.search(r"\binbox\b", text, re.I) else "NO",
            "provider_effect": "YES" if re.search(r"requests\.|httpx|urlopen|provider[_ ]write|dial", text, re.I) else "NO",
            "live_effect_flag": "PRESENT" if re.search(r"feature.flag|effect.*enabled|live.*enabled", text, re.I) else "NOT_DETECTED",
            "status": "APPROVED_CANDIDATE",
            "ownership": ownership(module.name),
        })
    write_csv(OUT / "ODOO-MODULE-INVENTORY.csv", list(rows[0]), rows)


def literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def module_constants(tree: ast.Module) -> dict[str, Any]:
    """Module-level literal assignments (``PREFIX = "/api"``, ``ROUTES = {...}``)."""
    constants: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = literal(node.value)
            if value is not None:
                constants[node.targets[0].id] = value
    return constants


def resolve_route(node: ast.AST, constants: dict[str, Any]) -> Any:
    """Resolve a route expression built from module constants (name, dict lookup, f-string)."""
    value = literal(node)
    if value is not None:
        return value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        container = constants.get(node.value.id)
        key = literal(node.slice)
        if isinstance(container, dict) and key in container:
            return container[key]
        return None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for piece in node.values:
            if isinstance(piece, ast.Constant):
                parts.append(str(piece.value))
            elif isinstance(piece, ast.FormattedValue):
                inner = resolve_route(piece.value, constants)
                if not isinstance(inner, str):
                    return None
                parts.append(inner)
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.List):
        items = [resolve_route(item, constants) for item in node.elts]
        return items if all(isinstance(item, str) for item in items) else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"list", "sorted", "tuple"} and len(node.args) == 1:
        container = resolve_route(node.args[0], constants)
        if isinstance(container, dict):
            return sorted(container)
        return container
    return None


def reachable_function_source(
    source: str,
    tree: ast.Module,
    cls: ast.ClassDef,
    entrypoint: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str:
    """Return an entrypoint plus local helpers it can call.

    Odoo service controllers intentionally use ``auth="none"`` because their
    JWT/HMAC service identity is verified by the controller itself.  Looking at
    only the decorated function misclassifies handlers that delegate that
    verification to a private helper.
    """
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    functions.update({
        node.name: node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    })
    pending = [entrypoint]
    visited: set[str] = set()
    segments: list[str] = []
    while pending:
        node = pending.pop()
        if node.name in visited:
            continue
        visited.add(node.name)
        segments.append(ast.get_source_segment(source, node) or "")
        for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
            name = None
            if isinstance(call.func, ast.Name):
                name = call.func.id
            elif isinstance(call.func, ast.Attribute):
                name = call.func.attr
            if name in functions and name not in visited:
                pending.append(functions[name])
    return "\n".join(segments)


def service_authentication(reachable_source: str, module_source: str) -> bool:
    """Identify the reviewed JWT/HMAC gateways used by service routes."""
    direct_gateway = re.search(
        r"\b(?:_authenticate|_body|_begin)\s*\(|hmac\.compare_digest\s*\(",
        reachable_source,
    )
    imported_gateway = re.search(r"\bverify\s*\(", reachable_source) and re.search(
        r"from\s+\.service_auth\s+import\s+", module_source
    )
    return bool(direct_gateway or imported_gateway)


def generate_endpoint_inventory() -> None:
    rows: list[dict[str, Any]] = []
    for path in sorted(ADDONS.glob("*/controllers/*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_level = module_constants(tree)
        for cls in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
            constants = dict(module_level)
            for stmt in cls.body:  # class-body constants such as PATH = "/codestra/..."
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    value = literal(stmt.value)
                    if value is not None:
                        constants[stmt.targets[0].id] = value
            for fn in [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for decorator in fn.decorator_list:
                    if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute) or decorator.func.attr != "route":
                        continue
                    if decorator.args:
                        route_value = resolve_route(decorator.args[0], constants)
                    else:
                        # A route-less override inherits its paths from the parent controller.
                        bases = [getattr(base, "id", getattr(base, "attr", "")) for base in cls.bases]
                        route_value = "INHERITED_ROUTE:" + ",".join(base for base in bases if base)
                    paths = route_value if isinstance(route_value, list) else [route_value]
                    kwargs = {kw.arg: literal(kw.value) for kw in decorator.keywords if kw.arg}
                    methods = kwargs.get("methods") or ["ANY"]
                    framework_auth = kwargs.get("auth", "user")
                    csrf = kwargs.get("csrf")
                    fn_source = ast.get_source_segment(source, fn) or ""
                    reachable_source = reachable_function_source(source, tree, cls, fn)
                    for route in paths:
                        if not isinstance(route, str):
                            route = "DYNAMIC_ROUTE"
                        generic = bool(re.search(r"<.*model|<.*method|execute", route, re.I))
                        mutation = any(method in {"POST", "PUT", "PATCH", "DELETE", "ANY"} for method in methods)
                        service_authenticated = service_authentication(
                            reachable_source, source
                        )
                        public_mutation = (
                            framework_auth in {"none", "public"}
                            and mutation
                            and not service_authenticated
                        )
                        sudo = ".sudo(" in reachable_source
                        # A retired route answers 410 either explicitly or by raising Gone.
                        retired = ("retired" in fn.name.lower() and "410" in fn_source) or bool(
                            re.search(r"\braise\s+(?:werkzeug\.exceptions\.)?Gone\s*\(", fn_source)
                        )
                        if generic:
                            status = "REJECT_GENERIC_PROXY"
                        elif retired:
                            status = "RETIRED"
                        elif public_mutation:
                            status = "REJECT_UNAUTHENTICATED_MUTATION"
                        elif sudo:
                            status = "REVIEW_SUDO"
                        else:
                            status = "CANDIDATE"
                        middleware_audience = service_authenticated or bool(
                            re.search(r"middleware|service", reachable_source, re.I)
                        )
                        effective_auth = (
                            f"{framework_auth}+controller_service_identity"
                            if service_authenticated
                            else framework_auth
                        )
                        if framework_auth == "user":
                            caller = "BROWSER_USER"
                        elif service_authenticated:
                            caller = "MIDDLEWARE_SERVICE"
                        elif retired:
                            caller = "NONE_RETIRED"
                        else:
                            caller = "PUBLIC"
                        if re.search(r"urllib\.request|requests\.(post|get|put|patch)|\.middleware\.|middleware_client|_transport", reachable_source):
                            downstream = "MIDDLEWARE_CLIENT"
                        elif re.search(r"request\.env\[|self\.env\[|\.sudo\(|\.search\(|\.create\(|\.write\(", reachable_source):
                            downstream = "ODOO_ORM"
                        else:
                            downstream = "NONE"
                        rows.append({
                            "method": ";".join(methods), "path": route, "module": path.parents[1].name,
                            "controller": f"{cls.name}.{fn.name}", "auth": effective_auth, "csrf": "NO" if csrf is False else ("YES" if csrf else "DEFAULT"),
                            "caller": caller, "downstream": downstream,
                            "audience": "MIDDLEWARE" if middleware_audience else "INTERNAL_OR_BROWSER",
                            "scope": "DECLARED" if "scope" in reachable_source else "NOT_DETECTED", "request_model": "INLINE", "response_model": "INLINE",
                            "tenant_company_binding": "YES" if re.search(r"tenant|company", reachable_source, re.I) else "NO",
                            "campaign_binding": "YES" if "campaign" in reachable_source.lower() else "NO",
                            "idempotency": "YES" if re.search(r"idempot|request_id|event_id", reachable_source, re.I) else "NO",
                            "rate_limit_expectation": "KONG/MIDDLEWARE", "middleware_only": "YES" if middleware_audience else "NO",
                            "browser_allowed": "YES" if framework_auth == "user" else "NO", "provider_callback": "YES" if "callback" in route.lower() or "webhook" in route.lower() else "NO", "status": status,
                        })
    write_csv(OUT / "ODOO-ENDPOINT-INVENTORY.csv", list(rows[0]) if rows else ["method", "path"], rows)


def generate_duplicate_audit() -> None:
    owners: dict[str, list[str]] = defaultdict(list)
    for module in sorted(path for path in ADDONS.iterdir() if path.is_dir()):
        for model in model_names(module):
            owners[model].append(module.name)
    duplicates = {model: modules for model, modules in owners.items() if len(modules) > 1}
    lines = ["# Duplicate model and facade audit", "", f"Source: `{SOURCE_BRANCH}` at `{git('rev-parse', SOURCE_REF)}`.", "", "A duplicate `_name` is a blocker; ordinary `_inherit` extensions are not duplicate model ownership.", ""]
    if duplicates:
        lines.extend(["## Duplicate `_name` declarations", ""])
        for model, modules in sorted(duplicates.items()):
            lines.append(f"- `{model}`: {', '.join(f'`{module}`' for module in modules)} — `DUPLICATE_BLOCKER`")
    else:
        lines.append("No duplicate `_name` declarations were detected.")
    lines.extend(["", "## Named facade pairs requiring ownership decisions", "", "| Pair | Classification |", "|---|---|", "| `call_center_core` / `codestra_cc_core` | `call_center_core=CANONICAL_OWNER`; `codestra_cc_core=FACADE_ONLY` |", "| `call_center_campaign` / `codestra_cc_campaign` | `call_center_campaign=CANONICAL_OWNER`; `codestra_cc_campaign=FACADE_ONLY` |", "| `call_center_compliance` / `codestra_cc_compliance` | `call_center_compliance=CANONICAL_OWNER`; `codestra_cc_compliance=COMPATIBILITY_LAYER` with separately named evidence/policy models |", "| `codestra_vicidial_crm` / `codestra_appointments` / `codestra_cc_calls` | `codestra_vicidial_crm=CANONICAL_OWNER` for `codestra.callback`; the others are `COMPATIBILITY_LAYER` extensions/new scoped operations |", "| `codestra_vicidial_crm` / `codestra_integration_hub` | `codestra_vicidial_crm=CANONICAL_OWNER` for the legacy event model; `codestra_integration_hub=MIGRATE_AND_REMOVE` unless live-state reconciliation proves it remains required |", "| `codestra_campaign_crm_os` / canonical campaign modules | `MIGRATE_AND_REMOVE` pending live data and migration proof |", ""])
    (OUT / "DUPLICATE-MODEL-FACADE-AUDIT.md").write_text("\n".join(lines), encoding="utf-8")


def generate_boundary_report() -> None:
    patterns = {
        "embedded FastAPI": re.compile(r"\bFastAPI\s*\("),
        "Celery platform": re.compile(r"\bCelery\s*\("),
        "RabbitMQ client": re.compile(r"\b(?:pika|aio_pika)\b"),
        "external PostgreSQL connection": re.compile(
            r"(?i)\b(?:psycopg2?|asyncpg|pg8000)\s*\.\s*(?:connect|create_pool)\s*\(|"
            r"from\s+(?:psycopg2?|asyncpg|pg8000)\s+import\s+[^\n]*\b(?:connect|create_pool)\b|"
            r"postgres(?:ql)?://"
        ),
        "direct VICIdial database write": re.compile(
            r"(?is)\b(?:insert\s+into|update|delete\s+from)\s+"
            r"(?:vicidial|vicidial_[a-z0-9_]+)\b"
        ),
        "direct provider HTTP write": re.compile(
            r"(?is)(?:\b(?:postal|telnexa|scraper)\b.{0,240}"
            r"(?:requests|httpx)\s*\.\s*(?:post|put|patch|delete)\s*\(|"
            r"(?:requests|httpx)\s*\.\s*(?:post|put|patch|delete)\s*\("
            r".{0,240}\b(?:postal|telnexa|scraper)\b)"
        ),
    }
    findings: list[tuple[str, str, int]] = []
    for path in ADDONS.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".xml", ".json", ".yaml", ".yml", ".md"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in patterns.items():
            for match in pattern.finditer(text):
                findings.append((label, str(path.relative_to(ROOT)), text.count("\n", 0, match.start()) + 1))
    lines = ["# Middleware boundary report", "", f"Candidate: `{SOURCE_BRANCH}` / `{git('rev-parse', SOURCE_REF)}`", "", "Odoo may own transactional outbox/inbox and resource-specific service operations. Cross-system connector execution remains in Codestra Middleware.", "", "## Automated scan", ""]
    if findings:
        for label, path, line in findings:
            lines.append(f"- `REVIEW`: {label} at `{path}:{line}`")
    else:
        lines.extend([
            "No competing Middleware platform, external PostgreSQL connection, direct VICIdial database write, or named provider HTTP write was detected.",
            "",
            "- `EMBEDDED_MIDDLEWARE_PLATFORMS=0`",
            "- `DIRECT_EXTERNAL_POSTGRESQL_CONNECTIONS=0`",
            "- `DIRECT_VICIDIAL_DATABASE_WRITES=0`",
            "- `DIRECT_NAMED_PROVIDER_HTTP_WRITES=0`",
        ])
    lines.extend(["", "This inventory is a deterministic source scan. Certification additionally requires the strict integration-boundary and mission-security CI gates.", ""])
    (OUT / "MIDDLEWARE-BOUNDARY-REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if "--endpoints-only" in sys.argv[1:]:
        # Offline regeneration of the controller inventory (no GitHub access).
        generate_endpoint_inventory()
        path = OUT / "ODOO-ENDPOINT-INVENTORY.csv"
        print(f"GENERATED={path.relative_to(ROOT)} SHA256={hashlib.sha256(path.read_bytes()).hexdigest()}")
        return 0
    generate_branch_ledger()
    generate_module_inventory()
    generate_endpoint_inventory()
    generate_duplicate_audit()
    generate_boundary_report()
    for path in sorted(OUT.iterdir()):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            print(f"GENERATED={path.relative_to(ROOT)} SHA256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
