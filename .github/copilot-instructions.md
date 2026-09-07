# Copilot instructions

## Setup and run

Use Python 3.11+ and run from the repository root. Windows needs x64 Python, including on ARM64 hosts: MSAL's cryptography dependency has no Windows ARM64 wheel. This workstation's `.venv` is already x64; do not recreate it with the native ARM64 `python` command. Fresh-machine PowerShell setup with registered Python 3.13 x64:

```powershell
py -3.13-64 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt
.\.venv\Scripts\python.exe -m ipykernel install --prefix .venv --name entra-obo-demo --display-name "Entra OBO demo (.venv)"
.\.venv\Scripts\python.exe -m notebook entra_agent_obo.ipynb
```

Copy `.env.example` to `.env` only if `.env` does not exist. Configuration loads through `python-dotenv`; existing environment variables win. Restart the kernel after changing configuration.

| Variable | Meaning / default |
|----------|-------------------|
| `TENANT_ID` | Required tenant ID or verified domain. |
| `BOOTSTRAP_CLIENT_ID` | Admin device-code client; defaults to Microsoft Graph Command Line Tools (`14d82eec-204b-4c2f-b7e8-296a70dab67e`). |
| `AGENT_PREFIX` | Created-object display-name prefix; defaults to `obo-demo`. |
| `AUTH_MODE` | `device_code` (default) or `interactive` (browser authorization code + PKCE). |
| `DEMO_USER_OBJECT_ID` | Optional member-user object ID; defaults to the provisioning administrator. |
| `AGENT_STATE_FILE` | Optional private state path; default is outside the repository under local application data. |

Execution requires a real global-cloud workforce tenant and two sign-ins: provisioning admin, then the selected demo user. This secret-based setup needs Agent ID Administrator plus an appropriate client/consent role such as Cloud Application Administrator. Agent ID Developer alone is insufficient for adding the secret. Provisioning is explicitly confirmed by typing the prefix. Do not execute against a tenant as a routine code check.

Prepare sections 1-6, then present 7-11 in the same kernel. For offline validation:

```powershell
.\.venv\Scripts\python.exe -m unittest -v
.\.venv\Scripts\python.exe -m unittest tests.test_demo.HelperTests.test_only_access_denied_403_counts_as_permission_boundary -v
```

The tests execute the actual notebook cells with mocked authentication/HTTP, including full reruns and cleanup. They do not establish live tenant readiness.

## Architecture

`entra_agent_obo.ipynb` is the executable narrative; `README.md` is the operator/audience guide. `demo_support.py` contains HTTP, diagnostic, retry, state, grant, and cleanup helpers. The notebook retains explicit provisioning calls and T1/Tg form bodies rather than hiding the protocol behind an Agent ID SDK. MSAL handles device-code or browser sign-in; `requests` handles token exchanges and Graph calls.

The numbered sections form a dependency chain through shared kernel globals:

- Sections 1-2 load configuration/state, define helpers, and authenticate the provisioning admin.
- Sections 3-4 create a blueprint application, its credential, its `access_agent` API scope and principal, then register a public client and pre-authorize that client on the blueprint.
- Sections 5-6 use the administrator's delegated token to create the child and its direct, one-user Graph `User.Read` grant. No app-only provisioning token or identity fallback is used.
- Sections 7-9 acquire the three tokens below; section 10 calls Graph with Tg. The administrator token is separate from this three-token flow.

| Token | Request and invariant |
|-------|-----------------------|
| `Tc` | Public client signs the user in for `api://<blueprint-appId>/access_agent`. Its audience must be the blueprint, not Graph. |
| `T1` | Blueprint uses `client_credentials`, its own credential, `fmi_path=AGENT_APP_ID`, and `scope=api://AzureADTokenExchange/.default`. This token authenticates the agent in the next exchange. |
| `Tg` | Agent uses the JWT-bearer grant with `client_id=AGENT_APP_ID`, `client_assertion=T1`, `assertion=Tc`, `requested_token_use=on_behalf_of`, and Graph `/.default`. The user remains the subject; the agent becomes the actor. |

Keep notebook Markdown, claim displays, the section 11 request recap, and README explanations aligned when changing the flow.

## Key conventions

- **Distinguish application IDs from directory object IDs.** `*_APP_ID` values are OAuth client IDs. `BLUEPRINT_OBJECT_ID`, `CLIENT_APP_OBJECT_ID`, and `AGENT_ID` are directory object IDs. The delegated grant uses `clientId=AGENT_ID` and `resourceId=GRAPH_SP_ID`; `fmi_path` uses `AGENT_APP_ID`, and `agentIdentityBlueprintId` uses `BLUEPRINT_APP_ID`.
- **Keep the two consent relationships separate.** Frontend preauthorization is for `access_agent`. The child's Graph grant uses `consentType="Principal"`, the selected user's `principalId`, and exactly `User.Read`. The blueprint's `requiredResourceAccess` is only a declaration. Unexpected broader/tenant-wide/other-user grants stop the run instead of being changed.
- **Preserve the limited-permission demonstration.** Section 10 allows `/v1.0/me` and expects a recognized HTTP 403 access denial for `/v1.0/users`. Do not grant `User.ReadBasic.All` to make the second call succeed. Other errors and unexpected successes must fail, not masquerade as an expected refusal. A member user avoids guest-access restrictions confounding this comparison.
- **Use the existing HTTP helpers.** `Graph` centralizes Graph requests, including the required `OData-Version: 4.0` header. Call sites supply explicit `/v1.0/` or `/beta/` paths; preserve this distinction. `request_token(**form)` sends form-encoded data to the tenant's v2.0 token endpoint, not JSON.
- **Allow for directory propagation without hiding failures.** `retry()` handles explicit propagation conditions, throttling and safely retryable service errors. Consent-pending retries are opt-in for OBO. Do not broadly retry programming errors, policy denials, user sign-ins, or ambiguous resource-creation failures.
- **Preserve the bootstrap permission restriction.** The admin token must not carry `Directory.AccessAsUser.All`; the notebook rejects it because Agent ID APIs refuse it. Do not substitute an Azure CLI token for the default bootstrap device-code flow.
- **Keep token inspection diagnostic.** `decode_jwt()` does not validate signatures. Graph tokens may be opaque; report that and use the token without inventing claims. Reuse `claim()` and `show_claims()` for v1/v2 names. Use fingerprints and redacted request views, not token/secret prefixes or full credentials. Do not parse T1's FMI subject as a literal child appId.
- **Keep permission/error documentation grounded.** `ADMIN_PERMISSIONS` and `CLEANUP_PERMISSIONS` define the actual requested scopes; the README must explain all of them. AADSTS65002 is a first-party preauthorization error, not a generic individual-scope error.

## State and cleanup

`DemoState` binds resources to tenant, prefix, and selected demo user. Reuse recorded IDs and verify their identity/parent; never adopt arbitrary name matches. The same blueprint's scope ID and valid credential must survive repeated cell execution.

The default Windows state file is `%LOCALAPPDATA%\EntraOboDemo\<tenant-id>-<prefix>.json`, outside this OneDrive-backed workspace. It contains the plaintext development secret. `DemoState.save()` writes atomically and updates memory only after success. Credential reuse checks the live key, matching blueprint appId, and expiry; never recommend deleting state to fix a bad secret.

Keep `.env` and all private state files out of commits. Clear notebook outputs and execution counts before committing; even the diagnostic output contains tenant IDs, object IDs, and user information.

Section 12 defaults to `CLEANUP = False`, requires prefix confirmation, and authenticates separately with cleanup scopes. It explicitly deletes only recorded objects, refuses extra child identities, and retains state after any partial failure. It can run after section 1 alone. Do not rely on implicit deletion cascades or claim the admin center can restore Agent ID objects.
