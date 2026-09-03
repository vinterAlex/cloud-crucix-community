"""
cloud_crucix/bridge.py
----------------------
Cloud Crucix Community Edition dashboard backend: a read-only BigQuery activity / IAM dashboard.

Authenticates as a **service account**, and only as a service account: there is
deliberately no gcloud, no Application Default Credentials and no interactive
login, so it behaves identically on every machine and can never quietly borrow
whoever happens to be logged in locally.

Drop a service-account JSON key into `secrets/` and start it; everything else
(projects, regions, prices) is discovered from the live Google APIs, with a sane
default and a config override for anything discovery can't answer.

Where the key is found, first match wins:
  1. --sa-key PATH  /  CRUCIX_SA_KEY_FILE
  2. auth.credentials_file in config.yaml
  3. any *.json in ./secrets/            ← the normal way
  4. GOOGLE_APPLICATION_CREDENTIALS
  5. ./service_account.json

Optionally that key then impersonates another service account
(auth.impersonate_service_account) or a Workspace user (auth.subject, for the
group-membership lookup only).

Usage:
    pip install -r requirements.txt
    python bridge.py                      # then open http://localhost:5006

See RUN-ME.txt for the Docker route.
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from google.auth import impersonated_credentials
from google.oauth2 import service_account
import google.auth.transport.requests
from google.cloud import bigquery
from google.cloud import resourcemanager_v3
from google.iam.v1 import iam_policy_pb2
import requests as http_requests
import copy
import json
import os
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    import yaml  # optional: config.yaml support
except ImportError:  # pragma: no cover - config.yaml is a convenience, not a requirement
    yaml = None

COMMUNITY = True

app = Flask(__name__)
CORS(app)
# Keep insertion order in responses: the diagnostics checks are ordered
# most-important-first and the UI renders them in the order it receives them.
app.json.sort_keys = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Beside the code, and beside wherever the user launched from. Deduped, since in
# the container both are /app/secrets and the startup message lists them.
SECRETS_DIRS = list(dict.fromkeys([
    os.path.join(BASE_DIR, "secrets"),
    os.path.join(os.path.abspath(os.getcwd()), "secrets"),
]))

# CRUCIX_CONFIG_DIR lets a container keep its config on a mounted volume, so
# settings saved from the UI survive `docker run --rm`.
CONFIG_DIR = os.environ.get("CRUCIX_CONFIG_DIR") or BASE_DIR
CONFIG_FILE = next(
    (p for p in (os.path.join(CONFIG_DIR, "config.yaml"), os.path.join(BASE_DIR, "config.yaml"))
     if os.path.isfile(p)),
    os.path.join(BASE_DIR, "config.yaml"),
)
CONFIG_OVERRIDE_FILE = os.path.join(CONFIG_DIR, "config.local.json")

# Fallback when no region can be detected and none is configured.
DEFAULT_REGION = "region-eu"

# Stamped on every query this tool runs, so /self_cost can find its own jobs.
TOOL_LABEL = "cloud-crucix-community"

# run_query refuses anything that is not a plain read.
SELECT_ONLY = re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*(SELECT|WITH)\b",
                         re.IGNORECASE | re.DOTALL)

# What each part of the UI costs in queries. Kept in one place so /self_cost can
# price everything honestly rather than guessing, and so the numbers in the
# Setup panel cannot drift away from what the code actually does.
#
# The dashboard is split into tabs precisely so this stays small: only the tab
# you are looking at loads, and a refresh reloads only that tab.
TAB_QUERIES = {
    "activity": {
        "label": "Activity tab",
        "endpoints": {"overview": 1, "users": 1, "tables": 1,
                      "permission_errors": 1, "failed_reasons": 1, "activity": 1},
        "note": "loads on open and on every refresh - this is the default tab",
    },
    "workload": {
        "label": "Workload tab (community placeholder)",
        "endpoints": {},
        "note": "community edition only - upgrade for workload analytics",
    },
    "cost": {
        "label": "Cost & storage tab (community placeholder)",
        "endpoints": {},
        "note": "community edition only - upgrade for cost analytics",
    },
    "findings": {
        "label": "Findings tab (community placeholder)",
        "endpoints": {},
        "note": "community edition only - upgrade for deep analysis",
    },
}
for _tab in TAB_QUERIES.values():
    _tab["queries"] = sum(_tab["endpoints"].values())

# Everything else is on demand, and none of it is part of a refresh.
LAZY_QUERIES = {
    "stat_tooltips": {"queries": 7, "label": "Stat-card tooltips",
                      "note": "first hover only, per time range"},
    "search": {"queries": 3, "label": "One search",
               "note": "per search you actually run"},
    "self_cost": {"queries": 1, "label": "This cost panel",
                  "note": "when you open Setup"},
}

# The default tab is what an auto-refresh actually reloads.
QUERIES_PER_REFRESH = TAB_QUERIES["activity"]["queries"]
QUERIES_PER_ENDPOINT = dict(TAB_QUERIES["activity"]["endpoints"])

# ─── Configuration ───────────────────────────────────────────────────────────────
# Everything here can be discovered or defaulted; config.yaml only exists so a
# user who knows better can pin a value. The UI edits the same settings live
# through GET/POST /settings.

DEFAULTS: dict = {
    # "" means: discover it. A list pins the picker instead of discovering.
    "projects": [],
    "region": "",
    "regions": [],
    "days": 7,
    "auth": {
        "credentials_file": "",
        "impersonate_service_account": "",
        # Workspace user to impersonate via domain-wide delegation. Only
        # /user_groups (Cloud Identity) needs it.
        "subject": "",
    },
    "pricing": {
        # Try the Cloud Billing Catalog API for live rates before defaulting.
        "auto": True,
        "currency": "USD",
        # Google's published flat on-demand analysis rate. Same in the US, the
        # EU and every single region, so it is a safe default; override with a
        # negotiated rate. Used only when auto-discovery is off or unavailable.
        "on_demand_per_tib": 6.25,
        "active_storage_per_gib_month": 0.02,
        "long_term_storage_per_gib_month": 0.01,
        # Physical (compressed) rates, used only to compare billing models.
        "active_physical_per_gib_month": 0.04,
        "long_term_physical_per_gib_month": 0.02,
        # Editions/capacity pricing bills slot-hours, not bytes — set this to
        # "capacity" and the cost view stops quoting a per-TiB estimate.
        "model": "on_demand",
    },
    "discovery": {
        # get_dataset() is one API call per dataset; bound it on huge projects.
        "max_datasets": 300,
        "cache_seconds": 900,
        "pricing_cache_seconds": 21600,
    },
    "safety": {
        # Hard ceiling per query, enforced by BigQuery itself. INFORMATION_SCHEMA
        # scans are small, so anything near this means the window is too wide for
        # the project's job volume — better to fail than to bill for it. 0 = off.
        "max_bytes_billed_gb": 50,
    },
    "ui": {
        # Auto-refresh interval in minutes; 0 turns it off. Only the tab you are
        # looking at reloads, and a hidden tab is skipped entirely, so leaving
        # the dashboard open overnight bills nothing.
        "auto_refresh_minutes": 60,
    },
    "server": {"host": "127.0.0.1", "port": 5006},
}

# Env vars that map onto config paths, for Docker/CI without a config file.
ENV_OVERRIDES = {
    "CRUCIX_PROJECTS": ("projects", "csv"),
    "CRUCIX_REGION": ("region", "str"),
    "CRUCIX_REGIONS": ("regions", "csv"),
    "CRUCIX_DAYS": ("days", "int"),
    "CRUCIX_SA_KEY_FILE": ("auth.credentials_file", "str"),
    "CRUCIX_IMPERSONATE_SA": ("auth.impersonate_service_account", "str"),
    "CRUCIX_SA_SUBJECT": ("auth.subject", "str"),
    "CRUCIX_PRICING_AUTO": ("pricing.auto", "bool"),
    "CRUCIX_PRICE_PER_TIB": ("pricing.on_demand_per_tib", "float"),
    "CRUCIX_CURRENCY": ("pricing.currency", "str"),
    "CRUCIX_PRICING_MODEL": ("pricing.model", "str"),
    "CRUCIX_AUTO_REFRESH_MINUTES": ("ui.auto_refresh_minutes", "int"),
    "PORT": ("server.port", "int"),
    "BIND_HOST": ("server.host", "str"),
}

CONFIG: dict = copy.deepcopy(DEFAULTS)
_CONFIG_LOCK = threading.Lock()


def _merge(base: dict, override: dict) -> dict:
    """Recursively overlay `override` on `base`."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _coerce(raw: str, kind: str):
    if kind == "csv":
        return [p.strip() for p in raw.split(",") if p.strip()]
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return raw.strip()


def _set_path(target: dict, dotted: str, value):
    node = target
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def cfg(dotted: str, fallback=None):
    """Read a config value by dotted path."""
    node: Any = CONFIG
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return fallback
        node = node[part]
    return node if node is not None else fallback


def load_config():
    """config.yaml → config.local.json (UI edits) → environment."""
    global CONFIG
    merged = copy.deepcopy(DEFAULTS)

    if yaml is not None and os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                merged = _merge(merged, yaml.safe_load(f) or {})
        except Exception as exc:  # a broken config must not stop the server
            print(f" WARN: could not read {CONFIG_FILE}: {exc}")

    if os.path.isfile(CONFIG_OVERRIDE_FILE):
        try:
            with open(CONFIG_OVERRIDE_FILE, "r", encoding="utf-8") as f:
                merged = _merge(merged, json.load(f) or {})
        except Exception as exc:
            print(f" WARN: could not read {CONFIG_OVERRIDE_FILE}: {exc}")

    env_patch: dict = {}
    for env_name, (path, kind) in ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        try:
            _set_path(env_patch, path, _coerce(raw, kind))
        except ValueError:
            print(f" WARN: ignoring {env_name}={raw!r} (not a {kind})")
    merged = _merge(merged, env_patch)

    with _CONFIG_LOCK:
        CONFIG = merged
    return CONFIG


def save_overrides(patch: dict) -> tuple[bool, str]:
    """Persist UI-side setting changes next to the code, best effort.

    In Docker the image is read-only unless the user mounts over it, so a
    failure here is normal and non-fatal: the values still apply in memory
    for the life of the process.
    """
    global CONFIG
    with _CONFIG_LOCK:
        CONFIG = _merge(CONFIG, patch)
    existing = {}
    if os.path.isfile(CONFIG_OVERRIDE_FILE):
        try:
            with open(CONFIG_OVERRIDE_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}
    try:
        with open(CONFIG_OVERRIDE_FILE, "w", encoding="utf-8") as f:
            json.dump(_merge(existing, patch), f, indent=2)
        return True, CONFIG_OVERRIDE_FILE
    except OSError as exc:
        return False, f"not persisted ({exc.strerror or exc}); applied for this session only"


load_config()

# ─── Auth ────────────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/bigquery",
    "https://www.googleapis.com/auth/cloud-platform",
]
# A service-account token can safely carry the Cloud Identity scope as well
# (needed by /user_groups); user ADC cannot request scopes it was never granted.
SA_SCOPES = SCOPES + ["https://www.googleapis.com/auth/cloud-identity.groups.readonly"]

# Populated by _resolve_credentials(); guarded because Flask serves concurrently
# and several endpoints fan out over a ThreadPoolExecutor.
_CREDS: Any = None
_CREDS_LOCK = threading.Lock()
AUTH_MODE = "unresolved"
AUTH_PRINCIPAL = "(unknown)"
AUTH_SOURCE = ""
AUTH_PROJECT = ""
SA_KEY_FILE: str | None = None


class MissingCredentials(Exception):
    """No service-account key file was found."""


def _is_service_account_key(path: str) -> bool:
    """True only for a real service-account key file.

    The secrets folder is also where saved settings land (and users drop all
    sorts of things there), so "any .json" is not good enough — a key has to
    declare itself as one.
    """
    try:
        if os.path.getsize(path) > 64 * 1024:
            return False
        with open(path, "r", encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    return (
        isinstance(blob, dict)
        and blob.get("type") == "service_account"
        and bool(blob.get("private_key"))
        and bool(blob.get("client_email"))
    )


def _secrets_keys() -> list[str]:
    """Every service-account key in a secrets/ folder, alphabetically."""
    found, seen = [], set()
    for directory in SECRETS_DIRS:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if not name.lower().endswith(".json"):
                continue
            path = os.path.join(directory, name)
            if path in seen or not os.path.isfile(path):
                continue
            seen.add(path)
            if _is_service_account_key(path):
                found.append(path)
    return found


def _key_file_path() -> str | None:
    """The service-account key to use, or None to fall back to ADC."""
    configured = cfg("auth.credentials_file", "") or ""
    if configured and not os.path.isabs(configured):
        configured = os.path.join(BASE_DIR, configured)
    for candidate in (SA_KEY_FILE, configured):
        if candidate and os.path.isfile(candidate):
            return candidate
    keys = _secrets_keys()
    if keys:
        return keys[0]
    for candidate in (
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        os.path.join(BASE_DIR, "service_account.json"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _impersonation_target() -> str | None:
    return cfg("auth.impersonate_service_account", "") or None


def _resolve_credentials():
    """Credentials from the service-account key, optionally impersonating.

    No ADC fallback on purpose — if the key is missing we say so rather than
    running as some other identity that happens to be configured on the host.
    """
    global AUTH_MODE, AUTH_PRINCIPAL, AUTH_SOURCE, AUTH_PROJECT

    key_file = _key_file_path()
    if not key_file:
        searched = SECRETS_DIRS + [os.path.join(BASE_DIR, "service_account.json")]
        raise MissingCredentials(
            "No service-account key file found. Put a .json key in the secrets "
            "folder (looked in: " + ", ".join(searched) + "), or pass --sa-key PATH."
        )

    subject = cfg("auth.subject", "") or None
    creds = service_account.Credentials.from_service_account_file(
        key_file, scopes=SA_SCOPES, subject=subject
    )
    AUTH_MODE = "service_account_key"
    AUTH_PRINCIPAL = subject or creds.service_account_email
    AUTH_SOURCE = key_file
    AUTH_PROJECT = getattr(creds, "project_id", "") or ""

    target = _impersonation_target()
    if target and target != creds.service_account_email:
        # The key's own identity is the source; the target is what we act as.
        # Requires roles/iam.serviceAccountTokenCreator on the target.
        creds = impersonated_credentials.Credentials(
            source_credentials=creds,
            target_principal=target,
            target_scopes=SA_SCOPES,
        )
        AUTH_MODE = "impersonated_service_account"
        AUTH_PRINCIPAL = target
        AUTH_SOURCE = f"{key_file} impersonating {target}"

    return creds


def get_credentials() -> Any:
    """Cached credentials, refreshed on demand. Safe to call from any thread."""
    global _CREDS
    with _CREDS_LOCK:
        if _CREDS is None:
            _CREDS = _resolve_credentials()
        if not getattr(_CREDS, "token", None) or getattr(_CREDS, "expired", False):
            _CREDS.refresh(google.auth.transport.requests.Request())
        return _CREDS


def preflight() -> str | None:
    """Load the key at startup, and try once to mint a token.

    A missing or malformed key is fatal (it can only be fixed by the operator).
    A failed token exchange is not: it may be a proxy or a blip, and the ⚙ Setup
    panel reports it far better than a dead process would — so it comes back as
    a warning and the dashboard still starts.
    """
    global _CREDS
    with _CREDS_LOCK:
        if _CREDS is None:
            _CREDS = _resolve_credentials()
    try:
        get_credentials()
        return None
    except Exception as exc:
        return str(exc)


def reset_credentials():
    """Drop the cached credentials so the next call re-reads the key from disk."""
    global _CREDS, AUTH_MODE, AUTH_PRINCIPAL, AUTH_SOURCE, AUTH_PROJECT
    with _CREDS_LOCK:
        _CREDS = None
        AUTH_MODE, AUTH_PRINCIPAL, AUTH_SOURCE, AUTH_PROJECT = "unresolved", "(unknown)", "", ""
    # Regions and prices were fetched as the previous identity.
    _CACHE.clear()


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_credentials().token}"}


def credentials_project() -> str:
    """The project the key itself belongs to — a good default selection."""
    try:
        get_credentials()
    except Exception:
        return ""
    return AUTH_PROJECT


def get_bq_client(project_id: str):
    return bigquery.Client(project=project_id, credentials=get_credentials())


# ─── Small TTL cache ─────────────────────────────────────────────────────────────
# Region detection and price lookups are stable and cost API calls, so they are
# cached per key for a configurable window.

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def cached(key: str, ttl_seconds: int, producer):
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = producer()
    with _CACHE_LOCK:
        _CACHE[key] = (now + max(0, ttl_seconds), value)
    return value


# ─── Region discovery ────────────────────────────────────────────────────────────

def _normalise_region(value: str) -> str:
    """'EU', 'eu', 'region-eu' → 'region-eu' (the INFORMATION_SCHEMA prefix)."""
    safe = "".join(c for c in (value or "") if c.isalnum() or c in "-_").lower()
    if not safe:
        return ""
    return safe if safe.startswith("region-") else f"region-{safe}"


def detect_regions(project_id: str) -> list[str]:
    """BigQuery locations that actually hold datasets in this project.

    INFORMATION_SCHEMA is per-region, so the dashboard has to know which
    regions are worth querying. list_datasets() returns light-weight items
    whose .location is often unset, so each dataset is fetched — in parallel,
    and bounded by discovery.max_datasets to stay fast on huge projects.
    """
    def produce():
        client = get_bq_client(project_id)
        limit = int(cfg("discovery.max_datasets", 300) or 300)
        refs = []
        for i, ds in enumerate(client.list_datasets(project=project_id)):
            if i >= limit:
                break
            refs.append(ds)

        def location_of(ds):
            loc = getattr(ds, "location", None)
            if loc:
                return str(loc)
            try:
                return str(client.get_dataset(ds.reference).location or "")
            except Exception:
                return ""

        locations = set()
        if refs:
            with ThreadPoolExecutor(max_workers=min(16, len(refs))) as ex:
                for loc in ex.map(location_of, refs):
                    if loc:
                        locations.add(loc.strip().lower())
        return sorted(_normalise_region(loc) for loc in locations)

    return cached(
        f"regions:{project_id}",
        int(cfg("discovery.cache_seconds", 900) or 900),
        produce,
    )


def region_options(project_id: str) -> dict:
    """What the region picker should show, and which entry to select."""
    pinned = [_normalise_region(r) for r in (cfg("regions", []) or []) if r]
    configured = _normalise_region(cfg("region", "") or "")

    if pinned:
        detected, source, error = [], "configured (config.regions)", None
    else:
        try:
            detected = detect_regions(project_id) if project_id else []
            source = "detected from dataset locations"
            error = None
        except Exception as exc:
            detected, source, error = [], "detection failed", str(exc)

    options = pinned or detected
    if configured and configured not in options:
        options = [configured] + options
    if not options:
        options = [DEFAULT_REGION]
        if not error:
            source = f"fallback default ({DEFAULT_REGION})"

    selected = configured if configured in options else options[0]
    return {
        "regions": options,
        "selected": selected,
        "detected": detected,
        "source": source,
        "error": error,
    }


# ─── Pricing discovery (Cloud Billing Catalog API) ──────────────────────────────
# The catalog mixes on-demand analysis SKUs with Editions/slot and BigQuery Omni
# SKUs, so a naive "first BigQuery SKU" lookup can silently return a wrong
# number. Every candidate is therefore filtered by resource group + unit and
# sanity-checked against a plausible range; anything ambiguous falls back to the
# configured/default rate and says so in `origin`.

BILLING_CATALOG = "https://cloudbilling.googleapis.com/v1"
BQ_SERVICE_ID = "24E6-581D-38E5"  # BigQuery, stable published service id

# resource group → (config key, plausible min, plausible max, unit fragment)
PRICE_SPECS = {
    "Analysis": ("on_demand_per_tib", 0.5, 50.0, "tib"),
    "ActiveLogicalStorage": ("active_storage_per_gib_month", 0.001, 0.5, "gib"),
    "LongTermLogicalStorage": ("long_term_storage_per_gib_month", 0.0005, 0.5, "gib"),
    "ActivePhysicalStorage": ("active_physical_per_gib_month", 0.001, 0.5, "gib"),
    "LongTermPhysicalStorage": ("long_term_physical_per_gib_month", 0.0005, 0.5, "gib"),
}
# Descriptions that are BigQuery-priced but not the plain rate we want.
PRICE_EXCLUDE = ("omni", "edition", "slot", "commit", "reservation", "flex", "bi engine")


def _unit_price(sku: dict) -> float | None:
    """Highest non-zero tier price of a SKU, in major currency units."""
    best = None
    for info in sku.get("pricingInfo", []) or []:
        expression = info.get("pricingExpression", {}) or {}
        for tier in expression.get("tieredRates", []) or []:
            price = tier.get("unitPrice", {}) or {}
            amount = float(price.get("units", 0) or 0) + float(price.get("nanos", 0) or 0) / 1e9
            if amount > 0 and (best is None or amount > best):
                best = amount
    return best


def _sku_unit(sku: dict) -> str:
    for info in sku.get("pricingInfo", []) or []:
        unit = (info.get("pricingExpression", {}) or {}).get("usageUnit", "")
        if unit:
            return unit.lower()
    return ""


def fetch_catalog_skus(currency: str) -> list[dict]:
    """All BigQuery SKUs from the public Cloud Billing Catalog."""
    skus: list[dict] = []
    page_token = ""
    for _ in range(20):  # hard page bound; BigQuery has a few hundred SKUs
        params = {"currencyCode": currency, "pageSize": 500}
        if page_token:
            params["pageToken"] = page_token
        r = http_requests.get(
            f"{BILLING_CATALOG}/services/{BQ_SERVICE_ID}/skus",
            headers=auth_headers(), params=params, timeout=30,
        )
        if r.status_code != 200:
            message = f"HTTP {r.status_code}"
            try:
                message = r.json().get("error", {}).get("message", message)
            except Exception:
                pass
            raise RuntimeError(message)
        body = r.json()
        skus.extend(body.get("skus", []) or [])
        page_token = body.get("nextPageToken") or ""
        if not page_token:
            break
    return skus


def _catalog_rate(skus: list[dict], group: str, region: str) -> tuple[float | None, str]:
    """Resolve one rate for one region, or (None, reason-it-was-rejected)."""
    key, low, high, unit_fragment = PRICE_SPECS[group]
    location = re.sub(r"^region-", "", region or "")

    candidates = []
    for sku in skus:
        category = sku.get("category", {}) or {}
        if category.get("resourceGroup") != group:
            continue
        if (category.get("usageType") or "").lower() not in ("ondemand", "", "preemptible"):
            continue
        description = (sku.get("description") or "").lower()
        if any(word in description for word in PRICE_EXCLUDE):
            continue
        if unit_fragment not in _sku_unit(sku):
            continue
        amount = _unit_price(sku)
        if amount is None or not (low <= amount <= high):
            continue
        regions = [str(x).lower() for x in (sku.get("serviceRegions") or [])]
        # Prefer an exact regional SKU, then a global one, then anything left.
        rank = 0 if location and location in regions else (1 if "global" in regions else 2)
        candidates.append((rank, amount, sku.get("description", "")))

    if not candidates:
        return None, "no matching SKU in the billing catalog"

    candidates.sort(key=lambda c: (c[0], c[1]))
    best_rank = candidates[0][0]
    same_rank = {round(c[1], 6) for c in candidates if c[0] == best_rank}
    if len(same_rank) > 1:
        # Several equally-plausible prices — refuse to guess.
        return None, f"ambiguous catalog SKUs ({sorted(same_rank)})"
    return candidates[0][1], f"billing catalog SKU '{candidates[0][2]}'"


def resolve_pricing(region: str = "") -> dict:
    """Effective rates plus, per rate, where the number came from.

    Precedence: live billing catalog (when pricing.auto) → configured value →
    built-in default. `origin` is surfaced in the UI so a number is never
    unexplained.
    """
    currency = (cfg("pricing.currency", "USD") or "USD").upper()
    model = cfg("pricing.model", "on_demand") or "on_demand"
    configured = cfg("pricing", {}) or {}
    auto = bool(cfg("pricing.auto", True))

    out: dict = {
        "currency": currency,
        "model": model,
        "auto": auto,
        "region": region,
        "origins": {},
        "catalog_error": None,
    }

    skus: list[dict] = []
    if auto:
        try:
            skus = cached(
                f"skus:{currency}",
                int(cfg("discovery.pricing_cache_seconds", 21600) or 21600),
                lambda: fetch_catalog_skus(currency),
            )
        except Exception as exc:
            out["catalog_error"] = str(exc)

    for group, (key, _low, _high, _unit) in PRICE_SPECS.items():
        default_value = float(DEFAULTS["pricing"][key])
        fallback = float(configured.get(key, default_value))

        value, origin, rejected = None, "", ""
        if skus:
            value, detail = _catalog_rate(skus, group, region)
            if value is None:
                rejected = detail
            else:
                origin = detail

        if value is None:
            value = fallback
            origin = "built-in default" if value == default_value else "configured rate"
            if not auto:
                origin += " (auto-discovery off)"
            elif out["catalog_error"]:
                origin += f" (billing catalog unavailable: {out['catalog_error']})"
            elif rejected:
                origin += f" ({rejected})"

        out[key] = float(value)
        out["origins"][key] = origin

    if model != "on_demand":
        out["on_demand_per_tib"] = None
        out["origins"]["on_demand_per_tib"] = "capacity pricing — billed in slot-hours, not bytes"
    return out


# ─── Helpers ─────────────────────────────────────────────────────────────────────

def parse_days(arg, default=None):
    if default is None:
        default = int(cfg("days", 7) or 7)
    try:
        d = int(arg)
        # Community Edition: cap the time window at 30 days.
        d = max(1, min(d, 30))
        return d
    except (TypeError, ValueError):
        return default


def time_clause(days: int) -> str:
    return f"creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)"


def jobs_view(region: str) -> str:
    # Sanitised by _normalise_region: only region-* names reach the SQL.
    return f"`{_normalise_region(region) or DEFAULT_REGION}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT"


def resolve_region(project: str, requested: str = "") -> str:
    """The region to query: explicit request → config → detected → default."""
    explicit = _normalise_region(requested)
    if explicit and requested.strip().lower() not in ("auto", "region-auto"):
        return explicit
    configured = _normalise_region(cfg("region", "") or "")
    if configured:
        return configured
    if project:
        try:
            detected = detect_regions(project)
            if detected:
                return detected[0]
        except Exception:
            pass
    return DEFAULT_REGION


def common_args():
    project = request.args.get("project", "").strip()
    region = resolve_region(project, request.args.get("region", ""))
    days = parse_days(request.args.get("days"))
    return project, region, days


def run_query(project_id: str, sql: str, params=None):
    """Run one read-only metadata query.

    Two guardrails, because this dashboard's own queries are the only thing it
    can ever be billed for:
      * a label, so its jobs are identifiable in JOBS_BY_PROJECT — that is how
        /self_cost prices the dashboard itself;
      * maximum_bytes_billed, enforced by BigQuery, so a 180-day window over a
        huge project fails loudly instead of quietly billing for a big scan.
    """
    if not SELECT_ONLY.match(sql.lstrip()):
        # Nothing here should ever be anything but a read.
        raise ValueError("run_query only runs SELECT/WITH statements")

    client = get_bq_client(project_id)
    ceiling_gb = float(cfg("safety.max_bytes_billed_gb", 0) or 0)
    config = bigquery.QueryJobConfig(
        query_parameters=params or [],
        labels={"tool": TOOL_LABEL},
        maximum_bytes_billed=int(ceiling_gb * 1024 ** 3) if ceiling_gb > 0 else None,
    )
    return list(client.query(sql, job_config=config).result())


def serialize_row(row):
    """Convert a BQ row to a JSON-safe dict."""
    d = dict(row)
    for k, v in list(d.items()):
        if v is None:
            continue
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif isinstance(v, (int, float)):
            d[k] = v
        elif isinstance(v, str):
            d[k] = v
        else:
            d[k] = str(v)
    return d


# ─── UI hosting ──────────────────────────────────────────────────────────────────
# Serving ui.html from the same origin means Docker needs one published port and
# the browser needs no CORS exception. Opening ui.html straight off disk still
# works — the page falls back to http://localhost:5006.

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "ui.html")


@app.route("/ui.html")
def ui_html():
    return send_from_directory(BASE_DIR, "ui.html")


# ─── Auth / discovery endpoints ──────────────────────────────────────────────────

@app.route("/ping")
def ping():
    return jsonify({
        "status": "ok",
        "service": "cloud_crucix_community",
        "default_region": DEFAULT_REGION,
        "auth_mode": AUTH_MODE,
    })


@app.route("/settings", methods=["GET", "POST"])
def settings():
    """The effective configuration, and a way to change it from the UI.

    GET  → every setting in force, plus what was discovered vs defaulted.
    POST → a partial config patch, e.g. {"pricing": {"on_demand_per_tib": 4.2}}.
           Applied immediately and persisted to config.local.json when the
           filesystem allows it.
    """
    if request.method == "POST":
        patch = request.get_json(silent=True) or {}
        if not isinstance(patch, dict):
            return jsonify({"error": "body must be a JSON object"}), 400
        allowed = {"projects", "region", "regions", "days", "pricing", "discovery",
                   "auth", "ui", "safety"}
        unknown = set(patch) - allowed
        if unknown:
            return jsonify({"error": f"unknown setting(s): {', '.join(sorted(unknown))}"}), 400
        persisted, where = save_overrides(patch)
        # Auth or pricing changes invalidate anything we cached from them.
        if "auth" in patch:
            reset_credentials()
        else:
            _CACHE.clear()
        return jsonify({"status": "ok", "persisted": persisted, "location": where,
                        "settings": CONFIG})

    project = request.args.get("project", "").strip() or credentials_project()
    payload = {
        "settings": CONFIG,
        "defaults": DEFAULTS,
        "auth": {
            "mode": AUTH_MODE,
            "principal": AUTH_PRINCIPAL,
            "source": AUTH_SOURCE,
            "key_files_found": [os.path.basename(p) for p in _secrets_keys()],
            "secrets_dirs": SECRETS_DIRS,
        },
        "config_file": CONFIG_FILE if os.path.isfile(CONFIG_FILE) else None,
        "overrides_file": CONFIG_OVERRIDE_FILE if os.path.isfile(CONFIG_OVERRIDE_FILE) else None,
        "credentials_project": credentials_project(),
    }
    if project:
        payload["region_options"] = region_options(project)
    return jsonify(payload)


@app.route("/regions")
def regions():
    """Regions worth querying for a project, detected from dataset locations."""
    project = request.args.get("project", "").strip()
    if not project:
        return jsonify({"error": "project parameter required", "regions": [DEFAULT_REGION]}), 400
    try:
        return jsonify(region_options(project))
    except Exception as e:
        return jsonify({
            "error": str(e), "regions": [DEFAULT_REGION], "selected": DEFAULT_REGION,
            "detected": [], "source": "detection failed",
        }), 200


@app.route("/pricing")
def pricing():
    """The rates the cost view uses, and where each number came from."""
    project = request.args.get("project", "").strip()
    region = resolve_region(project, request.args.get("region", ""))
    return jsonify(resolve_pricing(region))


@app.route("/diagnostics")
def diagnostics():
    """Probe every API the dashboard depends on, in parallel.

    Service accounts are usually granted a narrow set of roles, so a panel that
    silently stays empty is the most likely thing to go wrong. This says exactly
    which API is refusing the call and why."""
    project = request.args.get("project", "").strip() or credentials_project()

    def probe_bigquery():
        list(get_bq_client(project).list_datasets(project=project, max_results=1))
        return "can list datasets"

    def probe_information_schema():
        region = resolve_region(project)
        run_query(project, f"SELECT job_id FROM {jobs_view(region)} LIMIT 1")
        return f"can read {region} INFORMATION_SCHEMA.JOBS_BY_PROJECT"

    def probe_resource_manager():
        client = resourcemanager_v3.ProjectsClient(credentials=get_credentials())
        client.get_project(name=f"projects/{project}")
        return "can read project metadata"

    def probe_iam():
        client = resourcemanager_v3.ProjectsClient(credentials=get_credentials())
        req = iam_policy_pb2.GetIamPolicyRequest(resource=f"projects/{project}")  # type: ignore[attr-defined]
        policy = client.get_iam_policy(request=req)
        return f"{len(policy.bindings)} role bindings visible"

    def probe_logging():
        r = http_requests.post(
            "https://logging.googleapis.com/v2/entries:list",
            headers={**auth_headers(), "Content-Type": "application/json"},
            json={"resourceNames": [f"projects/{project}"],
                  "filter": 'protoPayload.serviceName="bigquery.googleapis.com"',
                  "pageSize": 1},
            timeout=20,
        )
        if r.status_code != 200:
            raise RuntimeError(r.json().get("error", {}).get("message", f"HTTP {r.status_code}"))
        return "can read audit logs"

    def probe_billing_catalog():
        rates = resolve_pricing(resolve_region(project))
        if rates.get("catalog_error"):
            raise RuntimeError(rates["catalog_error"])
        return rates["origins"].get("on_demand_per_tib", "resolved")

    # label, probe, the role that fixes it, and what breaks without it.
    probes = {
        "information_schema": (
            "Job history (INFORMATION_SCHEMA)", probe_information_schema,
            "roles/bigquery.resourceViewer + roles/bigquery.jobUser",
            "REQUIRED — without it every panel is empty",
        ),
        "dataset_metadata": (
            "Dataset metadata", probe_bigquery,
            "roles/bigquery.metadataViewer",
            "region auto-detection stops working; pin `region:` in config.yaml instead",
        ),
        "resource_manager": (
            "Project list / metadata", probe_resource_manager,
            "roles/browser",
            "the project picker only offers the key's own project",
        ),
        "iam_policy": (
            "IAM policy", probe_iam,
            "roles/iam.securityReviewer",
            "the per-user IAM tooltip cannot show granted roles",
        ),
        "logging": (
            "Audit logs", probe_logging,
            "roles/logging.privateLogViewer",
            "denials that never became a job (e.g. no jobs.create) stay invisible",
        ),
        "billing_catalog": (
            "Live pricing", probe_billing_catalog,
            "enable the Cloud Billing API on the key's project",
            "the cost panel falls back to the configured or default rate",
        ),
    }

    def run_probe(item):
        key, (label, fn, fix, impact) = item
        base = {"label": label, "fix": fix, "impact": impact}
        if not project:
            return key, {**base, "ok": False, "detail": "no project selected"}
        try:
            return key, {**base, "ok": True, "detail": fn()}
        except Exception as exc:
            return key, {**base, "ok": False, "detail": str(exc)[:300]}

    with ThreadPoolExecutor(max_workers=len(probes)) as ex:
        results = dict(ex.map(run_probe, probes.items()))

    return jsonify({
        "project": project,
        "auth": {"mode": AUTH_MODE, "principal": AUTH_PRINCIPAL, "source": AUTH_SOURCE},
        "checks": results,
        "ok_count": sum(1 for v in results.values() if v["ok"]),
        "total": len(probes),
        # This tool never reads table rows, so bigquery.dataViewer is not in the
        # list above and should not be granted: it would add nothing to the
        # dashboard and let the key read every table's contents.
        "note": "roles/bigquery.dataViewer is deliberately NOT needed — the dashboard "
                "reads job metadata only, never table contents.",
    })


@app.route("/whoami")
def whoami():
    """The identity every API call is made as.

    It comes straight off the key — no `userinfo` round-trip, which only
    answers for user credentials and 401s on a service-account token."""
    try:
        get_credentials()
        names = {
            "service_account_key": "service account",
            "impersonated_service_account": "service account (impersonated)",
        }
        return jsonify({
            "account": AUTH_PRINCIPAL,
            "name": names.get(AUTH_MODE, AUTH_MODE),
            "auth_mode": AUTH_MODE,
            "source": AUTH_SOURCE,
        })
    except Exception as e:
        # The key may have loaded fine and only the token exchange failed —
        # show who we are trying to be, with the reason it isn't working.
        known = AUTH_PRINCIPAL if AUTH_MODE != "unresolved" else "(no credentials)"
        return jsonify({"account": known, "error": str(e), "auth_mode": AUTH_MODE}), 200


@app.route("/projects")
def projects():
    """Projects the current credentials can see — no gcloud CLI involved.

    Tries Resource Manager search first (the complete answer), then BigQuery's
    own project list (works with only BigQuery roles granted), then the key's
    own project. Pin `projects:` in config.yaml to skip discovery entirely."""
    pinned = [p for p in (cfg("projects", []) or []) if p]
    if pinned:
        return jsonify({
            "projects": [{"projectId": p, "name": p} for p in sorted(pinned)],
            "source": "configured (config.projects)",
        })

    errors = []
    out: dict[str, str] = {}

    try:
        client = resourcemanager_v3.ProjectsClient(credentials=get_credentials())
        for p in client.search_projects(request={"query": "state:ACTIVE"}):
            out[p.project_id] = p.display_name or p.project_id
            if len(out) >= 500:
                break
        source = "resource manager search"
    except Exception as e:
        errors.append(f"resource manager: {e}")
        source = ""

    if not out:
        # A BigQuery-only service account can still enumerate its own projects.
        try:
            client = bigquery.Client(
                project=credentials_project() or None, credentials=get_credentials()
            )
            for p in client.list_projects():
                out[p.project_id] = getattr(p, "friendly_name", None) or p.project_id
                if len(out) >= 500:
                    break
            source = "bigquery project list"
        except Exception as e:
            errors.append(f"bigquery: {e}")

    if not out:
        own = credentials_project()
        if own:
            out[own] = own
            source = "service account's own project"

    if not out:
        return jsonify({
            "projects": [],
            "error": "No projects visible to this identity. Grant it "
                     "resourcemanager.projects.get / bigquery.jobs.create, or pin "
                     "`projects:` in config.yaml. " + " | ".join(errors),
        }), 200

    return jsonify({
        "projects": sorted(
            ({"projectId": pid, "name": name} for pid, name in out.items()),
            key=lambda p: p["projectId"],
        ),
        "source": source,
        "warnings": errors,
    })


@app.route("/reload_credentials", methods=["POST"])
@app.route("/relogin", methods=["POST"])  # kept: older UI builds call this path
def reload_credentials():
    """Re-read the key from disk and drop every cached token and lookup.

    This is the whole "switch identity" story: swap the .json in secrets/ and
    press the button. There is no interactive login to run."""
    try:
        reset_credentials()
        get_credentials()  # re-resolves now, so a bad or missing key reports here
        return jsonify({
            "status": "reloaded",
            "message": f"Credentials reloaded — authenticating as {AUTH_PRINCIPAL}.",
            "account": AUTH_PRINCIPAL,
            "auth_mode": AUTH_MODE,
            "source": AUTH_SOURCE,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ─── Data endpoints ──────────────────────────────────────────────────────────────

@app.route("/overview")
def overview():
    project, region, days = common_args()
    if not project:
        return jsonify({"error": "project parameter required"}), 400

    sql = f"""
    SELECT
      COUNT(*) AS total_jobs,
      COUNTIF(state = 'DONE' AND error_result IS NULL) AS successful_jobs,
      COUNTIF(error_result IS NOT NULL) AS failed_jobs,
      COUNTIF(error_result.reason = 'accessDenied') AS permission_errors,
      COUNT(DISTINCT user_email) AS unique_users,
      SUM(total_bytes_processed) AS total_bytes,
      SUM(total_slot_ms) AS total_slot_ms,
      AVG(TIMESTAMP_DIFF(end_time, start_time, MILLISECOND)) AS avg_duration_ms
    FROM {jobs_view(region)}
    WHERE {time_clause(days)}
    """
    try:
        rows = run_query(project, sql)
        if not rows:
            return jsonify({})
        return jsonify(serialize_row(rows[0]))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/permission_errors")
def permission_errors():
    project, region, days = common_args()
    if not project:
        return jsonify({"error": "project parameter required"}), 400

    user_filter = request.args.get("user", "").strip()
    user_clause = ""
    params = []
    if user_filter:
        user_clause = "AND LOWER(user_email) LIKE @u"
        params.append(bigquery.ScalarQueryParameter("u", "STRING", f"%{user_filter.lower()}%"))

    sql = f"""
    SELECT
      job_id,
      user_email,
      creation_time,
      error_result.reason AS reason,
      error_result.message AS message,
      SUBSTR(query, 1, 400) AS query_preview
    FROM {jobs_view(region)}
    WHERE {time_clause(days)}
      AND error_result.reason IN ('accessDenied', 'forbidden', 'invalidUser')
      {user_clause}
    ORDER BY creation_time DESC
    LIMIT 100
    """
    try:
        rows = run_query(project, sql, params)
        return jsonify({"errors": [serialize_row(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/users")
def users():
    project, region, days = common_args()
    if not project:
        return jsonify({"error": "project parameter required"}), 400

    sql = f"""
    SELECT
      user_email,
      COUNT(*) AS jobs,
      COUNTIF(error_result IS NOT NULL) AS errors,
      COUNTIF(error_result.reason = 'accessDenied') AS permission_errors,
      SUM(total_bytes_processed) AS bytes_processed,
      SUM(total_slot_ms) AS slot_ms
    FROM {jobs_view(region)}
    WHERE {time_clause(days)}
    GROUP BY user_email
    ORDER BY jobs DESC
    LIMIT 50
    """
    try:
        rows = run_query(project, sql)
        return jsonify({"users": [serialize_row(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/tables")
def tables():
    project, region, days = common_args()
    if not project:
        return jsonify({"error": "project parameter required"}), 400

    sql = f"""
    SELECT
      CONCAT(t.project_id, '.', t.dataset_id, '.', t.table_id) AS full_table,
      COUNT(*) AS query_count,
      COUNT(DISTINCT user_email) AS unique_users,
      SUM(total_bytes_processed) AS bytes_processed
    FROM {jobs_view(region)}, UNNEST(referenced_tables) AS t
    WHERE {time_clause(days)}
      AND state = 'DONE'
      AND error_result IS NULL
    GROUP BY full_table
    ORDER BY query_count DESC
    LIMIT 50
    """
    try:
        rows = run_query(project, sql)
        return jsonify({"tables": [serialize_row(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/search")
def search():
    project, region, days = common_args()
    if not project:
        return jsonify({"error": "project parameter required"}), 400
    q = request.args.get("q", "").strip().lower()
    if not q or len(q) < 2:
        return jsonify({"users": [], "tables": [], "queries": []})

    pattern = f"%{q}%"
    pq = [bigquery.ScalarQueryParameter("q", "STRING", pattern)]

    sql_users = f"""
    SELECT user_email, COUNT(*) AS jobs,
           COUNTIF(error_result IS NOT NULL) AS errors
    FROM {jobs_view(region)}
    WHERE {time_clause(days)} AND LOWER(user_email) LIKE @q
    GROUP BY user_email
    ORDER BY jobs DESC LIMIT 20
    """
    sql_tables = f"""
    SELECT CONCAT(t.dataset_id, '.', t.table_id) AS table_name, COUNT(*) AS hits
    FROM {jobs_view(region)}, UNNEST(referenced_tables) AS t
    WHERE {time_clause(days)} AND LOWER(t.table_id) LIKE @q
    GROUP BY table_name
    ORDER BY hits DESC LIMIT 20
    """
    sql_queries = f"""
    SELECT job_id, user_email, creation_time,
           SUBSTR(query, 1, 300) AS query_preview,
           total_bytes_processed AS bytes
    FROM {jobs_view(region)}
    WHERE {time_clause(days)} AND LOWER(query) LIKE @q
    ORDER BY creation_time DESC LIMIT 15
    """
    try:
        users_r = [serialize_row(r) for r in run_query(project, sql_users, pq)]
        tables_r = [serialize_row(r) for r in run_query(project, sql_tables, pq)]
        queries_r = [serialize_row(r) for r in run_query(project, sql_queries, pq)]
        return jsonify({"users": users_r, "tables": tables_r, "queries": queries_r})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/activity")
@app.route("/heatmap")  # kept: earlier UI builds call this path
def activity():
    """The day×hour heatmap AND the daily spend series, from ONE scan.

    Both views are just different roll-ups of the same grouping, so grouping by
    (day, hour) once and folding it up in Python here costs a single query
    instead of two — which matters because INFORMATION_SCHEMA is never served
    from cache and bills a 10 MB minimum per query.
    """
    project, region, days = common_args()
    if not project:
        return jsonify({"error": "project parameter required"}), 400

    rates = resolve_pricing(region)
    per_tib = float(rates.get("on_demand_per_tib") or 0)

    sql = f"""
    SELECT
      DATE(creation_time) AS day,
      EXTRACT(DAYOFWEEK FROM creation_time) AS dow,
      EXTRACT(HOUR FROM creation_time) AS hour,
      COUNT(*) AS jobs,
      COUNTIF(error_result IS NOT NULL) AS errors,
      SUM(COALESCE(total_bytes_billed, 0)) AS bytes_billed,
      SUM(COALESCE(total_bytes_processed, 0)) AS bytes_processed
    FROM {jobs_view(region)}
    WHERE {time_clause(days)}
    GROUP BY day, dow, hour
    ORDER BY day, hour
    """
    try:
        rows = run_query(project, sql)

        cells: dict = {}
        by_day: dict = {}
        for r in rows:
            key = (int(r["dow"]), int(r["hour"]))
            cells[key] = cells.get(key, 0) + int(r["jobs"] or 0)

            day = r["day"].isoformat() if hasattr(r["day"], "isoformat") else str(r["day"])
            d = by_day.setdefault(day, {
                "date": day, "jobs": 0, "errors": 0,
                "bytes_billed": 0, "bytes_processed": 0,
            })
            d["jobs"] += int(r["jobs"] or 0)
            d["errors"] += int(r["errors"] or 0)
            d["bytes_billed"] += int(r["bytes_billed"] or 0)
            d["bytes_processed"] += int(r["bytes_processed"] or 0)

        daily = sorted(by_day.values(), key=lambda d: d["date"])
        for d in daily:
            d["estimated_usd"] = d["bytes_billed"] / (1024 ** 4) * per_tib

        # A spike is a day well clear of a typical day. The median (not the
        # mean) is the baseline, so one very expensive day cannot raise the bar
        # enough to hide itself.
        costs = sorted(d["estimated_usd"] for d in daily)
        median = costs[len(costs) // 2] if costs else 0.0
        floor = float(cfg("analysis.spike_min_usd", 0.50) or 0)
        for d in daily:
            d["spike"] = bool(median > 0 and d["estimated_usd"] > max(2 * median, floor))

        total_usd = sum(d["estimated_usd"] for d in daily)
        return jsonify({
            "heatmap": [
                {"dow": dow, "hour": hour, "jobs": jobs}
                for (dow, hour), jobs in sorted(cells.items())
            ],
            "daily": daily,
            "summary": {
                "days": len(daily),
                "median_usd": median,
                "total_usd": total_usd,
                "total_bytes_billed": sum(d["bytes_billed"] for d in daily),
                "total_jobs": sum(d["jobs"] for d in daily),
                "spikes": sum(1 for d in daily if d["spike"]),
                "peak": max(daily, key=lambda d: d["estimated_usd"])["date"] if daily else None,
            },
            "pricing": rates,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/self_cost")
def self_cost():
    """What this dashboard has cost to run, measured — not estimated.

    Crucix reads the same view that records its own jobs, so its own spend is
    exactly knowable: find the jobs it labelled and sum what they billed.
    Deliberately NOT part of a refresh — it is one more billable query, so it
    runs only when someone opens the Setup panel.
    """
    project = request.args.get("project", "").strip()
    region = resolve_region(project, request.args.get("region", ""))
    days = parse_days(request.args.get("days"), 7)
    if not project:
        return jsonify({"error": "project parameter required"}), 400

    rates = resolve_pricing(region)
    per_tib = float(rates.get("on_demand_per_tib") or 0)

    sql = f"""
    SELECT
      DATE(creation_time) AS day,
      COUNT(*) AS queries,
      SUM(COALESCE(total_bytes_billed, 0)) AS bytes_billed
    FROM {jobs_view(region)}
    WHERE {time_clause(days)}
      AND EXISTS(
        SELECT 1 FROM UNNEST(labels) l
        WHERE l.key = 'tool' AND l.value = @tool
      )
    GROUP BY day
    ORDER BY day DESC
    """
    try:
        params = [bigquery.ScalarQueryParameter("tool", "STRING", TOOL_LABEL)]
        rows = [serialize_row(r) for r in run_query(project, sql, params)]

        total_bytes = sum(int(r["bytes_billed"] or 0) for r in rows)
        total_queries = sum(int(r["queries"] or 0) for r in rows)
        for r in rows:
            r["estimated_usd"] = int(r["bytes_billed"] or 0) / (1024 ** 4) * per_tib

        total_usd = total_bytes / (1024 ** 4) * per_tib
        per_query_usd = (total_usd / total_queries) if total_queries else 0.0
        active_days = len(rows)

        return jsonify({
            "days_window": days,
            "daily": rows,
            "measured": {
                "queries": total_queries,
                "bytes_billed": total_bytes,
                "total_usd": total_usd,
                "active_days": active_days,
                "per_query_usd": per_query_usd,
                # The unit that matters: one press of Refresh.
                "per_refresh_usd": per_query_usd * QUERIES_PER_REFRESH,
                "avg_per_active_day_usd": (total_usd / active_days) if active_days else 0.0,
            },
            "queries_per_refresh": QUERIES_PER_REFRESH,
            "queries_per_endpoint": QUERIES_PER_ENDPOINT,
            # Priced catalogue of every action in the UI.
            "actions": _action_catalogue(per_query_usd),
            "auto_refresh_minutes": int(cfg("ui.auto_refresh_minutes", 60) or 0),
            # Crucix has no timer and no background polling: sitting open costs
            # nothing at all. Only a refresh (or a search) issues queries.
            "idle_cost_per_hour_usd": 0.0,
            "pricing": rates,
            "note": ("Counts only jobs labelled tool=" + TOOL_LABEL +
                     ", so anything run before labelling was added is not included."),
        })
    except Exception as e:
        # Hand back everything that does not depend on the measurement, so the
        # Setup panel can still show what each action costs in queries.
        return jsonify({
            "error": str(e),
            "daily": [],
            "measured": {"queries": 0, "bytes_billed": 0, "total_usd": 0.0,
                         "active_days": 0, "per_query_usd": 0.0,
                         "per_refresh_usd": 0.0, "avg_per_active_day_usd": 0.0},
            "queries_per_refresh": QUERIES_PER_REFRESH,
            "queries_per_endpoint": QUERIES_PER_ENDPOINT,
            "actions": _action_catalogue(0.0),
            "auto_refresh_minutes": int(cfg("ui.auto_refresh_minutes", 60) or 0),
            "idle_cost_per_hour_usd": 0.0,
        }), 200


def _action_catalogue(per_query_usd: float) -> list:
    """Every action in the UI with its query count, priced if the rate is known."""
    return [
        {"key": key, "label": tab["label"], "queries": tab["queries"],
         "usd": per_query_usd * tab["queries"], "note": tab["note"], "kind": "tab"}
        for key, tab in TAB_QUERIES.items()
    ] + [
        {"key": key, "label": item["label"], "queries": item["queries"],
         "usd": per_query_usd * item["queries"], "note": item["note"],
         "kind": "on demand"}
        for key, item in LAZY_QUERIES.items()
    ]


@app.route("/failed_reasons")
def failed_reasons():
    project, region, days = common_args()
    if not project:
        return jsonify({"error": "project parameter required"}), 400

    sql = f"""
    WITH failures AS (
      SELECT
        COALESCE(error_result.reason, 'unknown') AS reason,
        user_email,
        error_result.message AS message,
        creation_time
      FROM {jobs_view(region)}
      WHERE {time_clause(days)} AND error_result IS NOT NULL
    )
    SELECT
      reason,
      COUNT(*) AS count,
      ARRAY_AGG(
        STRUCT(user_email, message, creation_time)
        ORDER BY creation_time DESC
        LIMIT 5
      ) AS examples
    FROM failures
    GROUP BY reason
    ORDER BY count DESC
    """
    try:
        rows = run_query(project, sql)
        out = []
        for r in rows:
            rdict = dict(r)
            examples = []
            for e in (rdict.get("examples") or []):
                edict = dict(e) if not isinstance(e, dict) else e
                ct = edict.get("creation_time")
                examples.append({
                    "user_email": edict.get("user_email"),
                    "message": (edict.get("message") or "")[:400],
                    "creation_time": ct.isoformat() if ct else None,
                })
            out.append({
                "reason": rdict.get("reason"),
                "count": int(rdict.get("count") or 0),
                "examples": examples,
            })
        return jsonify({"reasons": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/stats_detail")
def stats_detail():
    """Breakdowns powering the hover tooltips on the top stat cards.
    Runs ~7 small INFORMATION_SCHEMA queries in parallel for speed."""
    project, region, days = common_args()
    if not project:
        return jsonify({"error": "project parameter required"}), 400

    base = f"WHERE {time_clause(days)}"

    queries = {
        "by_statement": f"""
            SELECT
              COALESCE(statement_type, '(none)') AS type,
              COUNT(*) AS total,
              COUNTIF(error_result IS NULL) AS successful,
              COUNTIF(error_result IS NOT NULL) AS failed,
              SUM(total_bytes_processed) AS bytes
            FROM {jobs_view(region)}
            {base}
            GROUP BY type
            ORDER BY total DESC
        """,
        "by_job_type": f"""
            SELECT job_type AS type, COUNT(*) AS count
            FROM {jobs_view(region)}
            {base}
            GROUP BY job_type
            ORDER BY count DESC
        """,
        "by_error_reason": f"""
            SELECT COALESCE(error_result.reason, 'unknown') AS reason, COUNT(*) AS count
            FROM {jobs_view(region)}
            {base} AND error_result IS NOT NULL
            GROUP BY reason
            ORDER BY count DESC
        """,
        "perm_errors_by_user": f"""
            SELECT user_email, COUNT(*) AS count
            FROM {jobs_view(region)}
            {base} AND error_result.reason IN ('accessDenied','forbidden')
            GROUP BY user_email
            ORDER BY count DESC
            LIMIT 15
        """,
        "top_users_by_jobs": f"""
            SELECT user_email, COUNT(*) AS jobs, COUNTIF(error_result IS NOT NULL) AS errors
            FROM {jobs_view(region)}
            {base}
            GROUP BY user_email
            ORDER BY jobs DESC
            LIMIT 20
        """,
        "top_users_by_bytes": f"""
            SELECT user_email, SUM(total_bytes_processed) AS bytes
            FROM {jobs_view(region)}
            {base}
            GROUP BY user_email
            HAVING bytes > 0
            ORDER BY bytes DESC
            LIMIT 10
        """,
        "top_tables_by_bytes": f"""
            SELECT
              CONCAT(t.dataset_id, '.', t.table_id) AS table_name,
              SUM(total_bytes_processed) AS bytes
            FROM {jobs_view(region)}, UNNEST(referenced_tables) AS t
            {base}
            GROUP BY table_name
            HAVING bytes > 0
            ORDER BY bytes DESC
            LIMIT 10
        """,
    }

    def run_one(item):
        name, sql = item
        try:
            return name, [serialize_row(r) for r in run_query(project, sql)]
        except Exception as e:
            return name, {"error": str(e)}

    with ThreadPoolExecutor(max_workers=len(queries)) as ex:
        results = dict(ex.map(run_one, queries.items()))

    return jsonify(results)


# ─── Entry point ─────────────────────────────────────────────────────────────────

def find_free_port(host: str, preferred: int, span: int = 100) -> int:
    """The first free port at or after `preferred`.

    Several of these dashboards (or any other container) may already hold 5006,
    so rather than dying with "address already in use" we move up and print the
    port we actually took. --port-strict turns this off when a fixed port
    matters, e.g. a published Docker port mapped 1:1.
    """
    for candidate in range(preferred, preferred + span + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            # No SO_REUSEADDR: we want to know if anything is bound at all.
            try:
                probe.bind((host, candidate))
                return candidate
            except OSError:
                continue
    raise SystemExit(
        f" No free port between {preferred} and {preferred + span} on {host}. "
        "Free one up or pass --port."
    )


def _print_banner(host: str, port: int, requested_port: int):
    line = "=" * 64
    print("\n" + line)
    print(" ◇  Cloud Crucix Community Edition — BigQuery activity dashboard")
    print(line)

    keys = _secrets_keys()
    try:
        warning = preflight()
        print(f"\n  Identity      : {AUTH_PRINCIPAL}")
        print(f"  Auth mode     : {AUTH_MODE}")
        print(f"  Credentials   : {AUTH_SOURCE}")
        if len(keys) > 1:
            print(f"  NOTE          : {len(keys)} keys in secrets/ — using "
                  f"{os.path.basename(keys[0])}. Pin one with auth.credentials_file.")
        if warning:
            print(f"\n  WARNING       : could not get a token for this key:")
            print(f"                  {warning}")
            print("                  Starting anyway — fix the key and press "
                  "'Reload key' in the UI.")
    except MissingCredentials:
        print("\n  Auth FAILED   : no service-account key found.")
        print("\n  Put a .json service-account key in the secrets folder:")
        for directory in SECRETS_DIRS:
            print(f"      {directory}")
        print("\n  ...or pass --sa-key PATH. Nothing else is needed.")
        sys.exit(1)
    except Exception as exc:
        print(f"\n  Auth FAILED   : {exc}")
        print(f"\n  Key file      : {_key_file_path()}")
        print("  Check that the file is a valid service-account key and that the")
        print("  account still exists and is enabled.")
        sys.exit(1)

    project = credentials_project()
    print(f"\n  Project       : {project or '(discovered in the UI)'}")
    print(f"  Region        : {cfg('region') or 'auto-detected per project'}")
    model = cfg("pricing.model", "on_demand")
    if cfg("pricing.auto") and model == "on_demand":
        print("  Pricing       : live from the Cloud Billing Catalog, "
              f"else ${cfg('pricing.on_demand_per_tib')}/TiB")
    elif model == "on_demand":
        print(f"  Pricing       : ${cfg('pricing.on_demand_per_tib')}/TiB (fixed)")
    else:
        print(f"  Pricing       : {model} — no per-TiB estimate shown")
    if os.path.isfile(CONFIG_FILE):
        print(f"  Config        : {CONFIG_FILE}")

    shown_host = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    if port != requested_port:
        print(f"\n  NOTE          : port {requested_port} was busy — using {port} instead")
    print(f"\n  OPEN          : http://{shown_host}:{port}")
    print(f"  Health check  : http://{shown_host}:{port}/diagnostics")
    print("\n  Press Ctrl+C to stop\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Cloud Crucix Community Edition dashboard backend (read-only)",
        epilog="With a service-account JSON key in secrets/, no arguments are needed.",
    )
    parser.add_argument("--sa-key", metavar="PATH",
                        help="Service-account key JSON. Default: any *.json in secrets/")
    parser.add_argument("--impersonate", metavar="SA_EMAIL",
                        help="Act as this service account, using the key above as the source")
    parser.add_argument("--subject", metavar="USER_EMAIL",
                        help="Domain-wide-delegation subject; only /user_groups needs it")
    parser.add_argument("--project", metavar="PROJECT_ID",
                        help="Pin the project picker to this project")
    parser.add_argument("--region", metavar="REGION",
                        help="Pin the BigQuery region (eu, us, europe-west1, ...). "
                             "Default: auto-detected from the project's datasets")
    parser.add_argument("--price-per-tib", type=float, metavar="USD",
                        help="Your negotiated on-demand rate, instead of the discovered one")
    parser.add_argument("--no-auto-pricing", action="store_true",
                        help="Skip the Cloud Billing Catalog lookup and use the configured rate")
    parser.add_argument("--host", default=None, help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None,
                        help="Preferred port (default 5006). If it is taken, the next "
                             "free one is used and printed")
    parser.add_argument("--port-strict", action="store_true",
                        help="Fail instead of moving to the next free port")
    args = parser.parse_args()

    if args.sa_key:
        if not os.path.isfile(args.sa_key):
            parser.error(f"--sa-key: no such file: {args.sa_key}")
        SA_KEY_FILE = args.sa_key

    cli_patch: dict = {}
    if args.impersonate:
        _set_path(cli_patch, "auth.impersonate_service_account", args.impersonate)
    if args.subject:
        _set_path(cli_patch, "auth.subject", args.subject)
    if args.project:
        _set_path(cli_patch, "projects", [args.project])
    if args.region:
        _set_path(cli_patch, "region", args.region)
    if args.price_per_tib is not None:
        _set_path(cli_patch, "pricing.on_demand_per_tib", args.price_per_tib)
    if args.no_auto_pricing:
        _set_path(cli_patch, "pricing.auto", False)
    if args.host:
        _set_path(cli_patch, "server.host", args.host)
    if args.port:
        _set_path(cli_patch, "server.port", args.port)
    if cli_patch:
        CONFIG = _merge(CONFIG, cli_patch)

    host = cfg("server.host", "127.0.0.1") or "127.0.0.1"
    requested_port = int(cfg("server.port", 5006) or 5006)
    port = requested_port if args.port_strict else find_free_port(host, requested_port)

    _print_banner(host, port, requested_port)

    # Waitress if it is installed (quiet, and no "development server" warning
    # in front of a colleague), otherwise Flask's own server. Either is fine:
    # this binds to loopback and serves one person.
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        app.run(debug=False, host=host, port=port, use_reloader=False)
    else:
        waitress_serve(app, host=host, port=port, threads=16, ident="cloud-crucix-community")
