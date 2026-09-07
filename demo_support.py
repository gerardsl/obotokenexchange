"""Bookkeeping for the notebook; the provisioning and OAuth requests stay in its cells."""

import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import msal
import requests


GRAPH = "https://graph.microsoft.com"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
TOKEN_EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"
BOOTSTRAP_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"

ADMIN_PERMISSIONS = {
    "User.Read": "Identify the signed-in administrator (GET /me).",
    "Application.ReadWrite.All": "Create/read/configure the ordinary public client and its principal.",
    "AgentIdentityBlueprint.Read.All": "Find and read the demo blueprint, scopes and credential metadata.",
    "AgentIdentityBlueprint.Create": "Create the blueprint.",
    "AgentIdentityBlueprint.AddRemoveCreds.All": "Create the development-only blueprint secret.",
    "AgentIdentityBlueprint.UpdateAuthProperties.All": "Configure the API scope, preauthorization and required resource access.",
    "AgentIdentityBlueprintPrincipal.Read.All": "Find and read the blueprint principal.",
    "AgentIdentityBlueprintPrincipal.Create": "Create the blueprint principal.",
    "AgentIdentity.Read.All": "Find and read the child agent identity.",
    "AgentIdentity.Create.All": "Create the child agent identity as the provisioning administrator.",
    "DelegatedPermissionGrant.ReadWrite.All": "Read/write the one-user Graph User.Read consent grant.",
}

CLEANUP_PERMISSIONS = [
    "User.Read",
    "Application.ReadWrite.All",
    "AgentIdentityBlueprint.Read.All",
    "AgentIdentityBlueprintPrincipal.Read.All",
    "AgentIdentity.Read.All",
    "DelegatedPermissionGrant.ReadWrite.All",
    "AgentIdentity.DeleteRestore.All",
    "AgentIdentityBlueprintPrincipal.DeleteRestore.All",
    "AgentIdentityBlueprint.DeleteRestore.All",
]


class DemoError(RuntimeError):
    pass


class PropagationPending(DemoError):
    pass


class ApiError(DemoError):
    def __init__(self, status, code, message, *, method="", safe_to_retry=False,
                 error_codes=(), request_id=None, retry_after=0):
        self.status = status
        self.code = code
        self.message = message
        self.method = method
        self.safe_to_retry = safe_to_retry
        self.error_codes = error_codes
        self.retry_after = retry_after
        suffix = f" [request/correlation ID: {request_id}]" if request_id else ""
        super().__init__(f"{method}: HTTP {status}, {code}: {message}{suffix}")


def require(condition, message):
    if not condition:
        raise DemoError(message)


def guid(value, name):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise DemoError(f"{name} must be a GUID (directory object ID or application ID as documented).") from exc


def settings_from_environment():
    tenant = os.getenv("TENANT_ID", "").strip()
    require(
        tenant and tenant.lower() not in {"common", "organizations", "consumers"}
        and "your-tenant" not in tenant and re.fullmatch(r"[A-Za-z0-9.-]+", tenant),
        "Set TENANT_ID in .env to your development tenant ID or verified domain, then restart the kernel.",
    )
    prefix = os.getenv("AGENT_PREFIX", "obo-demo").strip()
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", prefix),
            "AGENT_PREFIX must be 1-64 letters, numbers, dots, dashes or underscores.")
    mode = os.getenv("AUTH_MODE", "device_code").strip()
    require(mode in {"device_code", "interactive"}, "AUTH_MODE must be device_code or interactive.")
    user = os.getenv("DEMO_USER_OBJECT_ID", "").strip()
    return {
        "tenant": tenant,
        "prefix": prefix,
        "bootstrap_client_id": guid(os.getenv("BOOTSTRAP_CLIENT_ID") or BOOTSTRAP_CLIENT_ID,
                                    "BOOTSTRAP_CLIENT_ID"),
        "auth_mode": mode,
        "demo_user_id": guid(user, "DEMO_USER_OBJECT_ID") if user else None,
    }


def response_json(response, operation, *, safe_to_retry=False, secrets=()):
    try:
        body = response.json() if response.content else {}
    except (ValueError, requests.exceptions.JSONDecodeError):
        body = None
    if response.status_code not in (200, 201, 204):
        error = body.get("error", {}) if isinstance(body, dict) else {}
        if isinstance(error, dict):
            code, message = error.get("code", "GraphError"), error.get("message", response.text)
        else:
            code, message = error, body.get("error_description", response.text)
        for secret in secrets:
            if secret:
                message = message.replace(secret, "<redacted>")
        retry_after = response.headers.get("Retry-After", "")
        raise ApiError(
            response.status_code, code, message[:1200], method=operation,
            safe_to_retry=safe_to_retry,
            error_codes=body.get("error_codes", []) if isinstance(body, dict) else [],
            request_id=(body.get("correlation_id") if isinstance(body, dict) else None)
            or response.headers.get("request-id"),
            retry_after=int(retry_after) if retry_after.isdigit() else 0,
        )
    if response.status_code == 204:
        return None
    require(isinstance(body, dict), f"{operation} returned a successful status but not a JSON object.")
    return body


def retry(fn, *, label, tries=6, delay=5, propagation=False, consent_pending=False):
    """Retry known transient failures, never programming errors or arbitrary denials."""
    require(tries >= 1, "retry() needs at least one attempt.")
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except PropagationPending:
            if not propagation or attempt == tries:
                raise
            print(f"{label}: object not ready; retry {attempt}/{tries - 1} in {delay}s.")
            time.sleep(delay)
            delay = min(delay * 2, 60)
        except ApiError as exc:
            missing_object = exc.status in (400, 404) and (
                "not found" in exc.message.lower() or "does not exist" in exc.message.lower()
                or exc.code in {"Request_ResourceNotFound", "ResourceNotFound",
                                "AgentIdentity_AgentBlueprintPrincipalDoesNotExist"}
            )
            retryable = (
                exc.status == 429
                or (exc.safe_to_retry and exc.status in (500, 502, 503, 504))
                or (propagation and (missing_object or 700016 in exc.error_codes))
                or (consent_pending and 65001 in exc.error_codes)
            )
            if not retryable or attempt == tries:
                raise
            wait = max(delay, exc.retry_after)
            print(f"{label}: {exc.code}; retry {attempt}/{tries - 1} in {wait}s.")
            time.sleep(wait)
            delay = min(delay * 2, 60)


def discover_tenant(tenant):
    url = f"https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"
    response = requests.get(url, timeout=30, allow_redirects=False)
    metadata = response_json(response, "Tenant discovery", safe_to_retry=True)
    issuer = urlparse(metadata["issuer"])
    require(issuer.scheme == "https" and issuer.netloc == "login.microsoftonline.com",
            "This demo targets the Microsoft Entra global cloud only.")
    tenant_id = guid(issuer.path.strip("/").split("/")[0], "Discovered tenant ID")
    return tenant_id, f"https://login.microsoftonline.com/{tenant_id}"


def fingerprint(token):
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def decode_jwt(token):
    """Diagnostic decoding, NOT signature validation. None means opaque/unreadable."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        result = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def claim(claims, *names):
    if claims is None:
        return None
    return next((claims[name] for name in names if name in claims), None)


def show_claims(token, keys, title):
    print(f"\n{title} ({fingerprint(token)})")
    claims = decode_jwt(token)
    if claims is None:
        print("This access token is opaque/unreadable here. No claims were checked locally.")
        print("Continue using it as an opaque credential; the receiving service validates it.")
        return None
    for key in keys:
        names = key.split("|")
        value = claim(claims, *names)
        if value is None:
            value = "<not present>"
        elif names[0] in {"exp", "iat", "nbf"} and isinstance(value, (int, float)):
            value = datetime.fromtimestamp(value, timezone.utc).isoformat()
        print(f"  {key:<26} {value}")
    return claims


def require_live_claims(claims, section):
    require(claims is not None, f"Cannot inspect this demo token. Rerun {section}; do not fabricate claims.")
    require(isinstance(claims.get("exp"), (int, float)) and claims["exp"] > time.time() + 30,
            f"The token is expired or nearly expired. Rerun {section}.")


def show_token_request(endpoint, form, *, title, token_names=None):
    token_names = token_names or {}
    print(f"\n{title}\nPOST {endpoint}\nContent-Type: application/x-www-form-urlencoded")
    for key, value in form.items():
        if key in {"client_secret", "code", "code_verifier", "device_code", "refresh_token"}:
            value = "<hidden>"
        elif key in {"client_assertion", "assertion"}:
            value = f"<{token_names.get(key, 'token')}; {fingerprint(value)}>"
        print(f"  {key} = {value}")


def request_token(endpoint, **form):
    response = requests.post(
        endpoint, data=form, timeout=30, allow_redirects=False,
        headers={"client-request-id": str(uuid.uuid4())},
    )
    result = response_json(response, "Token endpoint", safe_to_retry=True, secrets=[
        form.get(key) for key in ("client_secret", "client_assertion", "assertion", "refresh_token")
    ])
    require(isinstance(result.get("access_token"), str) and result["access_token"],
            "Token endpoint returned no access_token.")
    return result


def sign_in(client_id, authority, scopes, mode, label):
    print(f"\n{label}: client_id={client_id}")
    print("Requested scopes:", " ".join(scopes))
    app = msal.PublicClientApplication(
        client_id, authority=authority, exclude_scopes=["offline_access"],
    )
    if mode == "device_code":
        def begin():
            flow = app.initiate_device_flow(scopes=scopes)
            if "user_code" not in flow:
                raise ApiError(400, flow.get("error", "DeviceFlowError"),
                               flow.get("error_description", "No device code returned."),
                               method="Device authorization", error_codes=flow.get("error_codes", []))
            return flow

        flow = retry(begin, label="Client registration propagation", propagation=True)
        print(f"MSAL requests a device code at {authority}/oauth2/v2.0/devicecode.")
        print(flow["message"])
        print("MSAL polls /token with grant_type=urn:ietf:params:oauth:grant-type:device_code.")
        result = app.acquire_token_by_device_flow(flow)
    else:
        print("MSAL opens the browser and uses authorization code + PKCE with a localhost callback.")
        result = app.acquire_token_interactive(scopes=scopes, timeout=300)
    if "access_token" not in result:
        raise ApiError(
            400, result.get("error", "SignInError"),
            result.get("error_description", "No access token returned."),
            method=label, error_codes=result.get("error_codes", []),
            request_id=result.get("correlation_id"),
        )
    return result


class Graph:
    def __init__(self, token):
        self.token = token

    def call(self, method, path, body=None, params=None):
        url = path if path.startswith("https://") else GRAPH + path
        parsed = urlparse(url)
        require(
            parsed.scheme == "https" and parsed.netloc == "graph.microsoft.com"
            and parsed.path.startswith(("/v1.0/", "/beta/")),
            "Graph requests, including pagination, must stay on the configured Graph endpoint.",
        )
        response = requests.request(
            method, url, headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "OData-Version": "4.0",
                "client-request-id": str(uuid.uuid4()),
            },
            json=body, params=params, timeout=30, allow_redirects=False,
        )
        return response_json(response, f"{method} {parsed.path}",
                             safe_to_retry=method in {"GET", "PATCH", "DELETE"},
                             secrets=[self.token])

    def get(self, path, params=None):
        return self.call("GET", path, params=params)

    def post(self, path, body):
        return self.call("POST", path, body)

    def patch(self, path, body):
        return self.call("PATCH", path, body)

    def delete(self, path):
        return self.call("DELETE", path)

    def list(self, path, params=None):
        items = []
        while path:
            page = retry(lambda: self.get(path, params), label="Graph read")
            require(isinstance(page.get("value"), list), "Graph collection returned no value array.")
            items.extend(page["value"])
            path, params = page.get("@odata.nextLink"), None
        return items


class DemoState:
    """Private local state, tied to one tenant, prefix and demonstration user."""

    def __init__(self, tenant_id, prefix, path=None):
        configured = path or os.getenv("AGENT_STATE_FILE")
        if configured:
            self.path = Path(os.path.expandvars(str(configured))).expanduser().resolve()
        else:
            default_root = Path(os.getenv("LOCALAPPDATA") or Path.home().joinpath(".local", "state"))
            self.path = default_root.joinpath("EntraOboDemo", f"{tenant_id}-{prefix}.json").resolve()
        expected = {"version": 1, "tenant_id": tenant_id, "prefix": prefix}
        if self.path.exists():
            with self.path.open(encoding="utf-8") as handle:
                self._data = json.load(handle)
            require(isinstance(self._data, dict)
                    and all(self._data.get(k) == v for k, v in expected.items())
                    and isinstance(self._data.get("resources"), dict),
                    "State belongs to a different tenant/prefix or has an unsupported format. "
                    "Restore the matching state or choose a fresh AGENT_PREFIX and state path.")
        else:
            self._data = {**expected, "resources": {}}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def save(self, **values):
        updated = {**self._data, **values}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=self.path.name + ".", suffix=".tmp",
                                             delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(updated, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        self._data = updated

    def resource(self, name):
        return self._data["resources"].get(name)

    def record(self, name, resource):
        allowed = {"id", "appId", "displayName", "agentIdentityBlueprintId",
                   "clientId", "resourceId", "principalId", "consentType", "scope"}
        require(resource.get("id"), f"Cannot record {name}: Graph returned no object ID.")
        records = {**self._data["resources"],
                   name: {key: value for key, value in resource.items()
                          if key in allowed and not (key == "appId" and value is None)}}
        self.save(resources=records)

    def bind_user(self, user_id):
        require(self.get("demo_user_id") in (None, user_id),
                "This state belongs to a different demo user. Use a new prefix or finish cleanup first.")
        self.save(demo_user_id=user_id)

    def remove_after_cleanup(self):
        self.path.unlink()
        self._data = {**self._data, "resources": {}, "blueprint_credential": None, "cleanup_started": True}


def recorded_resource(graph, state, name, path_template):
    require(not state.get("cleanup_started"),
            "Cleanup has started. Finish section 12 before provisioning again.")
    saved = state.resource(name)
    if saved is None:
        return None
    try:
        current = retry(lambda: graph.get(path_template.format(id=saved["id"])),
                        label=f"Read {name}", propagation=True)
    except ApiError as exc:
        if exc.status != 404:
            raise
        raise DemoError(f"The recorded {name} no longer exists. Finish cleanup or restore it; "
                        "do not silently replace it with another object.") from exc
    for key in ("id", "appId", "displayName", "agentIdentityBlueprintId"):
        if key in saved:
            require(current.get(key) == saved[key],
                    f"The recorded {name} has a different {key}. Stop and inspect it in Entra.")
    return current


def require_unused_name(graph, collection, display_name):
    escaped = display_name.replace("'", "''")
    matches = graph.list(collection, {"$filter": f"displayName eq '{escaped}'", "$select": "id,displayName"})
    require(not matches, f"An untracked object named {display_name!r} already exists. "
            "Restore its state file or choose a unique AGENT_PREFIX; this demo will not adopt or delete it.")


def find_service_principal(graph, app_id, *, blueprint=False):
    collection = "/v1.0/servicePrincipals"
    if blueprint:
        collection += "/microsoft.graph.agentIdentityBlueprintPrincipal"
    items = graph.list(collection, {"$filter": f"appId eq '{guid(app_id, 'appId')}'"})
    require(len(items) <= 1, "More than one service principal matched the application ID.")
    return items[0] if items else None


def ensure_graph_grant(graph, state, agent_id, graph_sp_id, user_id):
    grants = graph.list("/v1.0/oauth2PermissionGrants", {
        "$filter": f"clientId eq '{agent_id}' and resourceId eq '{graph_sp_id}'",
    })
    expected = {
        "clientId": agent_id, "resourceId": graph_sp_id,
        "consentType": "Principal", "principalId": user_id, "scope": "User.Read",
    }
    require(len(grants) <= 1 and all(
        all(grant.get(key) == value for key, value in expected.items()) for grant in grants
    ), "This agent has unexpected Graph grants (another user, tenant-wide consent or broader scopes). "
       "Nothing was changed. Inspect the grants or use a fresh demo prefix.")
    if grants:
        grant = grants[0]
        print("Reusing the exact one-user User.Read grant.")
    else:
        grant = retry(lambda: graph.post("/v1.0/oauth2PermissionGrants", expected),
                      label="Create delegated grant", propagation=True)
        print("Created one-user User.Read consent (not tenant-wide).")
    state.record("grant", grant)
    return grant


def expect_directory_denial(graph):
    try:
        graph.get("/v1.0/users", {"$top": 1, "$select": "id"})
    except ApiError as exc:
        if exc.status != 403 or exc.code not in {"Authorization_RequestDenied", "ErrorAccessDenied", "accessDenied"}:
            raise
        print(f"Expected denial: HTTP 403, {exc.code}. User.Read does not permit listing users.")
        return
    raise DemoError("GET /users unexpectedly succeeded. The agent has more access than this demo intended. "
                    "Inspect effective grants/inheritance; this is NOT a successful permission-boundary demo.")


RESOURCE_PATHS = {
    "blueprint": "/v1.0/applications/{id}/microsoft.graph.agentIdentityBlueprint",
    "blueprint_principal": "/v1.0/servicePrincipals/{id}/microsoft.graph.agentIdentityBlueprintPrincipal",
    "client_app": "/v1.0/applications/{id}",
    "client_principal": "/v1.0/servicePrincipals/{id}",
    "agent": "/beta/servicePrincipals/{id}/microsoft.graph.agentIdentity",
    "grant": "/v1.0/oauth2PermissionGrants/{id}",
}


def cleanup_demo(graph, state):
    """Delete only recorded objects, and retain state on every partial failure."""
    resources = state.get("resources")
    require(resources, "No recorded resources to clean up.")
    blueprint = resources.get("blueprint")
    if blueprint:
        children = graph.list("/beta/servicePrincipals/microsoft.graph.agentIdentity")
        unexpected = [child for child in children
                      if child.get("agentIdentityBlueprintId") == blueprint["appId"]
                      and child["id"] != resources.get("agent", {}).get("id")]
        require(not unexpected, "The blueprint has additional child identities. Cleanup refuses to affect them.")
    existing = {}
    for name, saved in resources.items():
        require(name in RESOURCE_PATHS, f"Unknown resource type in state: {name}.")
        path = RESOURCE_PATHS[name].format(id=saved["id"])
        try:
            current = graph.get(path)
        except ApiError as exc:
            if exc.status != 404:
                raise
            print(f"{name}: already absent.")
            continue
        for key, value in saved.items():
            require(current.get(key) == value, f"{name} changed ({key}); cleanup stopped before deleting anything.")
        existing[name] = path
    state.save(cleanup_started=True)
    for name in ("grant", "agent", "client_principal", "client_app", "blueprint_principal", "blueprint"):
        if name not in existing:
            continue
        try:
            retry(lambda: graph.delete(existing[name]), label=f"Delete {name}")
        except ApiError as exc:
            if exc.status != 404:
                raise
        print(f"Removed {name}.")
    state.remove_after_cleanup()
    print("Recorded objects removed; private local state deleted. Restart the kernel to discard in-memory tokens.")
