import contextlib
import copy
import io
import json
import os
import tempfile
import time
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import nbformat
import requests

import demo_support as demo


ROOT = Path(__file__).resolve().parents[1]
TENANT = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
OTHER_USER = "33333333-3333-4333-8333-333333333333"
PREFIX = "offline-demo"


def jwt(claims):
    def encode(value):
        return demo.base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return ".".join([encode({"alg": "none"}), encode(claims), "offline-signature"])


def response(body=None, status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode() if body is not None else b""
    result.headers.update(headers or {})
    return result


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).joinpath("state.json")
        self.state = demo.DemoState(TENANT, PREFIX, self.path)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def test_settings_reject_common_and_placeholder_tenants(self):
        for tenant in ("", "common", "organizations", "consumers", "your-tenant-id-or-verified-domain"):
            with self.subTest(tenant=tenant), patch.dict(os.environ, {"TENANT_ID": tenant}, clear=True):
                with self.assertRaises(demo.DemoError):
                    demo.settings_from_environment()

    def test_settings_reject_unsafe_prefix_and_invalid_user_id(self):
        for overrides in ({"AGENT_PREFIX": "..\\other"}, {"DEMO_USER_OBJECT_ID": "not-an-object-id"}):
            with self.subTest(overrides=overrides), patch.dict(
                os.environ, {"TENANT_ID": TENANT, **overrides}, clear=True
            ):
                with self.assertRaises(demo.DemoError):
                    demo.settings_from_environment()

    def test_state_is_bound_to_tenant_prefix_and_demo_user(self):
        self.state.bind_user(USER)
        self.state.save(blueprint_credential={"secret": "private-test-secret"})
        reloaded = demo.DemoState(TENANT, PREFIX, self.path)
        self.assertEqual(reloaded.get("blueprint_credential")["secret"], "private-test-secret")
        for tenant, prefix in ((OTHER_USER, PREFIX), (TENANT, "different")):
            with self.assertRaises(demo.DemoError):
                demo.DemoState(tenant, prefix, self.path)
        with self.assertRaises(demo.DemoError):
            reloaded.bind_user(OTHER_USER)

    def test_explicit_state_path_does_not_require_a_home_directory(self):
        with patch.dict(os.environ, {}, clear=True):
            state = demo.DemoState(TENANT, PREFIX, self.path)
        self.assertEqual(state.path, self.path)

    def test_failed_atomic_write_keeps_previous_state(self):
        self.state.save(marker="old")
        with patch.object(demo.os, "replace", side_effect=PermissionError("simulated write failure")):
            with self.assertRaises(PermissionError):
                self.state.save(marker="new")
        self.assertEqual(self.state.get("marker"), "old")
        self.assertEqual(json.loads(self.path.read_text())["marker"], "old")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_state_records_only_nonsecret_resource_fields(self):
        self.state.record("agent", {"id": USER, "appId": OTHER_USER, "secretText": "do-not-store"})
        self.assertNotIn("secretText", self.state.resource("agent"))

    def test_pending_app_id_does_not_break_recorded_identity_recovery(self):
        self.state.record("agent", {"id": USER, "appId": None})
        graph = Mock()
        graph.get.return_value = {"id": USER, "appId": OTHER_USER}
        current = demo.recorded_resource(graph, self.state, "agent", demo.RESOURCE_PATHS["agent"])
        self.assertEqual(current["appId"], OTHER_USER)

    def test_untracked_names_are_not_adopted(self):
        graph = Mock()
        graph.list.return_value = [{"id": USER, "displayName": "existing"}]
        with self.assertRaises(demo.DemoError):
            demo.require_unused_name(graph, "/v1.0/applications", "O'Brien & demo")
        self.assertEqual(graph.list.call_args.args[1]["$filter"], "displayName eq 'O''Brien & demo'")
        graph.post.assert_not_called()

    def test_recorded_object_parent_mismatch_stops_reuse(self):
        self.state.record("agent", {"id": USER, "agentIdentityBlueprintId": TENANT})
        graph = Mock()
        graph.get.return_value = {"id": USER, "agentIdentityBlueprintId": OTHER_USER}
        with self.assertRaises(demo.DemoError):
            demo.recorded_resource(graph, self.state, "agent", demo.RESOURCE_PATHS["agent"])

    def test_retry_does_not_hide_programming_or_permission_errors(self):
        for error in (TypeError("bug"), demo.ApiError(403, "Authorization_RequestDenied", "denied")):
            fn = Mock(side_effect=error)
            with patch.object(demo.time, "sleep") as sleep:
                with self.assertRaises(type(error)):
                    demo.retry(fn, label="operation", propagation=True)
                self.assertEqual(fn.call_count, 1)
                sleep.assert_not_called()

    def test_retry_does_not_repeat_ambiguous_resource_creation(self):
        fn = Mock(side_effect=demo.ApiError(503, "ServiceUnavailable", "unknown result", method="POST"))
        with self.assertRaises(demo.ApiError):
            demo.retry(fn, label="create", propagation=True)
        self.assertEqual(fn.call_count, 1)

    def test_retry_honors_throttle_delay_and_known_propagation(self):
        fn = Mock(side_effect=[
            demo.ApiError(429, "TooManyRequests", "throttle", retry_after=17),
            demo.PropagationPending("appId pending"),
            "ready",
        ])
        with patch.object(demo.time, "sleep") as sleep:
            self.assertEqual(demo.retry(fn, label="read", propagation=True), "ready")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [17, 10])

    def test_missing_consent_retry_is_explicit_not_global(self):
        error = demo.ApiError(400, "invalid_grant", "consent not visible", error_codes=[65001])
        no_retry = Mock(side_effect=error)
        with self.assertRaises(demo.ApiError):
            demo.retry(no_retry, label="read")
        self.assertEqual(no_retry.call_count, 1)
        pending = Mock(side_effect=[error, {"access_token": "fake"}])
        with patch.object(demo.time, "sleep"):
            self.assertEqual(demo.retry(pending, label="OBO", consent_pending=True)["access_token"], "fake")

    def test_retry_repeats_service_errors_only_for_retry_safe_operations(self):
        fn = Mock(side_effect=[
            demo.ApiError(503, "ServiceUnavailable", "retry later", safe_to_retry=True),
            "ready",
        ])
        with patch.object(demo.time, "sleep"):
            self.assertEqual(demo.retry(fn, label="read"), "ready")
        self.assertEqual(fn.call_count, 2)

    def test_admin_consent_accepts_graph_qualified_scopes_and_rejects_wrong_tenant(self):
        result = {"id_token_claims": {"tid": TENANT},
                  "scope": " ".join(f"{demo.GRAPH}/{scope}" for scope in demo.CLEANUP_PERMISSIONS)}
        demo.require_admin_consent(result, TENANT, demo.CLEANUP_PERMISSIONS)
        with self.assertRaises(demo.DemoError):
            demo.require_admin_consent(result, OTHER_USER, demo.CLEANUP_PERMISSIONS)

    def test_user_sign_in_denial_is_not_retried_or_switched_to_another_mode(self):
        for mode in ("device_code", "interactive"):
            with self.subTest(mode=mode):
                app = Mock()
                app.initiate_device_flow.return_value = {"user_code": "OFFLINE", "message": "Offline only"}
                denied = {"error": "access_denied", "error_description": "Sign-in was cancelled.",
                          "correlation_id": "offline-correlation"}
                app.acquire_token_by_device_flow.return_value = denied
                app.acquire_token_interactive.return_value = denied
                with patch.object(demo.msal, "PublicClientApplication", return_value=app), \
                        patch.object(demo.time, "sleep") as sleep:
                    with self.assertRaises(demo.ApiError) as caught:
                        demo.sign_in(USER, "https://login.microsoftonline.com/" + TENANT,
                                     ["User.Read"], mode, "Offline sign-in")
                self.assertIn("offline-correlation", str(caught.exception))
                sleep.assert_not_called()
                if mode == "device_code":
                    app.acquire_token_by_device_flow.assert_called_once()
                    app.acquire_token_interactive.assert_not_called()
                else:
                    app.acquire_token_interactive.assert_called_once()
                    app.acquire_token_by_device_flow.assert_not_called()

    def test_request_token_is_form_encoded_and_redacts_errors(self):
        token_response = response({"access_token": "result"})
        with patch.object(demo.requests, "post", return_value=token_response) as post:
            self.assertEqual(demo.request_token("https://login.microsoftonline.com/token",
                                             client_secret="private", scope="scope")["access_token"], "result")
        self.assertEqual(post.call_args.kwargs["data"], {"client_secret": "private", "scope": "scope"})
        self.assertNotIn("json", post.call_args.kwargs)
        with self.assertRaises(demo.ApiError) as caught:
            demo.response_json(response({"error": "invalid_client", "error_description": "secret private"}, 400),
                               "token", secrets=["private"])
        self.assertNotIn("private", str(caught.exception))

    def test_safe_request_display_never_exposes_credentials(self):
        demo.show_token_request("https://example.invalid/token", {
            "client_id": "public-id", "client_secret": "short-secret",
            "assertion": "user-access-token", "client_assertion": "agent-access-token",
        }, title="request", token_names={"assertion": "Tc", "client_assertion": "T1"})
        text = self.output.getvalue()
        for secret in ("short-secret", "user-access-token", "agent-access-token"):
            self.assertNotIn(secret, text)
        self.assertIn("Tc;", text)
        self.assertIn("T1;", text)

    def test_opaque_graph_tokens_are_explicit_not_fake_claims(self):
        self.assertIsNone(demo.show_claims("opaque-access-token", ["aud", "oid"], "Graph"))
        self.assertIn("No claims were checked locally", self.output.getvalue())
        self.assertIsNone(demo.decode_jwt("x.%%%bad.x"))
        self.assertEqual(demo.claim({"appid": "v1"}, "azp", "appid"), "v1")
        with self.assertRaises(demo.DemoError):
            demo.require_live_claims({"exp": time.time() - 1}, "section 7")

    def test_graph_headers_and_pagination_stay_on_graph(self):
        pages = [
            response({"value": [{"id": "one"}], "@odata.nextLink": demo.GRAPH + "/v1.0/users?$skiptoken=next"}),
            response({"value": [{"id": "two"}]}),
        ]
        with patch.object(demo.requests, "request", side_effect=pages) as request:
            items = demo.Graph("private-token").list("/v1.0/users", {"$top": 1})
        self.assertEqual(len(items), 2)
        self.assertEqual(request.call_args_list[0].kwargs["headers"]["OData-Version"], "4.0")
        self.assertEqual(request.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer private-token")
        self.assertIsNone(request.call_args_list[1].kwargs["params"])
        with patch.object(demo.requests, "request") as request:
            with self.assertRaises(demo.DemoError):
                demo.Graph("private").get("https://other.invalid/v1.0/users")
            request.assert_not_called()

    def test_grant_is_single_user_and_uses_object_ids(self):
        graph = Mock()
        graph.list.return_value = []
        graph.post.side_effect = lambda path, body: {**body, "id": "grant-id"}
        result = demo.ensure_graph_grant(graph, self.state, USER, TENANT, OTHER_USER)
        expected = {"clientId": USER, "resourceId": TENANT, "principalId": OTHER_USER,
                    "consentType": "Principal", "scope": "User.Read"}
        graph.post.assert_called_once_with("/v1.0/oauth2PermissionGrants", expected)
        self.assertNotIn("expiryTime", result)
        self.assertEqual(self.state.resource("grant")["principalId"], OTHER_USER)

    def test_broader_tenantwide_or_other_user_grants_stop_without_changes(self):
        expected = {"id": "g", "clientId": USER, "resourceId": TENANT, "principalId": OTHER_USER,
                    "consentType": "Principal", "scope": "User.Read"}
        for change in ({"scope": "User.Read User.ReadBasic.All"}, {"consentType": "AllPrincipals"},
                       {"principalId": USER}):
            graph = Mock()
            graph.list.return_value = [{**expected, **change}]
            with self.subTest(change=change), self.assertRaises(demo.DemoError):
                demo.ensure_graph_grant(graph, self.state, USER, TENANT, OTHER_USER)
            graph.post.assert_not_called()
            graph.patch.assert_not_called()

    def test_recorded_grant_is_reused_when_collection_read_is_stale(self):
        saved = {"id": "recorded-grant", "clientId": USER, "resourceId": TENANT,
                 "principalId": OTHER_USER, "consentType": "Principal", "scope": "User.Read"}
        self.state.record("grant", saved)
        graph = Mock()
        graph.get.return_value = saved
        graph.list.return_value = []
        graph.post.return_value = {**saved, "id": "duplicate-grant"}
        result = demo.ensure_graph_grant(graph, self.state, USER, TENANT, OTHER_USER)
        self.assertEqual(result["id"], saved["id"])
        graph.post.assert_not_called()

    def test_missing_recorded_grant_is_not_silently_recreated(self):
        saved = {"id": "recorded-grant", "clientId": USER, "resourceId": TENANT,
                 "principalId": OTHER_USER, "consentType": "Principal", "scope": "User.Read"}
        self.state.record("grant", saved)
        graph = Mock()
        graph.get.side_effect = demo.ApiError(404, "Request_ResourceNotFound", "not found")
        graph.list.return_value = []
        graph.post.return_value = {**saved, "id": "replacement-grant"}
        with patch.object(demo.time, "sleep"), self.assertRaises(demo.DemoError):
            demo.ensure_graph_grant(graph, self.state, USER, TENANT, OTHER_USER)
        graph.post.assert_not_called()
        self.assertEqual(self.state.resource("grant"), saved)

    def test_recorded_grant_cannot_be_replaced_by_another_matching_grant(self):
        saved = {"id": "recorded-grant", "clientId": USER, "resourceId": TENANT,
                 "principalId": OTHER_USER, "consentType": "Principal", "scope": "User.Read"}
        self.state.record("grant", saved)
        graph = Mock()
        graph.get.return_value = saved
        graph.list.return_value = [{**saved, "id": "different-grant"}]
        with self.assertRaises(demo.DemoError):
            demo.ensure_graph_grant(graph, self.state, USER, TENANT, OTHER_USER)
        self.assertEqual(self.state.resource("grant"), saved)
        graph.post.assert_not_called()

    def test_only_access_denied_403_counts_as_permission_boundary(self):
        graph = Mock()
        for code in ("Authorization_RequestDenied", "ErrorAccessDenied", "accessDenied"):
            graph.get.side_effect = demo.ApiError(403, code, "denied")
            demo.expect_directory_denial(graph)
        graph.get.side_effect = demo.ApiError(403, "insufficient_claims", "Conditional Access challenge")
        with self.assertRaises(demo.ApiError):
            demo.expect_directory_denial(graph)
        for status in (400, 401, 404, 429, 500):
            graph.get.side_effect = demo.ApiError(status, "Authorization_RequestDenied", "not the expected denial")
            with self.subTest(status=status), self.assertRaises(demo.ApiError):
                demo.expect_directory_denial(graph)
        graph.get.side_effect = requests.Timeout("network")
        with self.assertRaises(requests.Timeout):
            demo.expect_directory_denial(graph)
        graph.get.side_effect = None
        graph.get.return_value = {"value": []}
        with self.assertRaises(demo.DemoError):
            demo.expect_directory_denial(graph)

    def test_cleanup_preserves_state_on_failed_delete(self):
        saved = {"id": USER, "appId": TENANT, "displayName": "client"}
        self.state.record("client_app", saved)
        graph = Mock()
        graph.get.return_value = saved
        graph.delete.side_effect = demo.ApiError(403, "Authorization_RequestDenied", "no delete permission")
        with self.assertRaises(demo.ApiError):
            demo.cleanup_demo(graph, self.state)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.state.resource("client_app"), saved)
        self.assertTrue(self.state.get("cleanup_started"))

    def test_cleanup_refuses_additional_children(self):
        self.state.record("blueprint", {"id": USER, "appId": TENANT})
        graph = Mock()
        graph.list.return_value = [{"id": OTHER_USER, "agentIdentityBlueprintId": TENANT}]
        with self.assertRaises(demo.DemoError):
            demo.cleanup_demo(graph, self.state)
        graph.delete.assert_not_called()

    def test_cleanup_accepts_missing_recorded_objects_and_removes_state(self):
        self.state.record("client_app", {"id": USER})
        graph = Mock()
        graph.get.side_effect = demo.ApiError(404, "Request_ResourceNotFound", "not found")
        demo.cleanup_demo(graph, self.state)
        self.assertFalse(self.path.exists())
        graph.delete.assert_not_called()


class FakeTenant:
    """An in-memory transport, not a tenant emulator or a substitute for a live rehearsal."""

    def __init__(self, *, opaque_graph=False, demo_user=USER, pending_agent_reads=0):
        self.objects = {"applications": {}, "servicePrincipals": {}, "oauth2PermissionGrants": {}}
        self.counter = 10
        self.creates = []
        self.exchanges = []
        self.opaque_graph = opaque_graph
        self.demo_user = demo_user
        self.pending_agent_reads = pending_agent_reads
        self.admin_token = jwt({"aud": demo.GRAPH_APP_ID, "oid": USER, "tid": TENANT, "exp": time.time() + 3600})
        self.graph_token = None
        graph_sp = {"id": self.new_id(), "appId": demo.GRAPH_APP_ID, "displayName": "Microsoft Graph",
                    "oauth2PermissionScopes": [{"id": self.new_id(), "value": "User.Read", "isEnabled": True}]}
        self.objects["servicePrincipals"][graph_sp["id"]] = graph_sp

    def new_id(self):
        self.counter += 1
        return str(uuid.UUID(int=self.counter))

    def public_client(self, client_id, **kwargs):
        tenant = self

        class Client:
            def initiate_device_flow(self, scopes):
                self.scopes = scopes
                return {"user_code": "OFFLINE", "message": "Offline authentication stub"}

            def acquire_token_by_device_flow(self, flow):
                return self.result(self.scopes)

            def acquire_token_interactive(self, scopes, timeout):
                return self.result(scopes)

            def result(self, scopes):
                if client_id == demo.BOOTSTRAP_CLIENT_ID:
                    token = tenant.admin_token
                    user = USER
                else:
                    blueprint_id = scopes[0].split("/")[2]
                    user = tenant.demo_user
                    token = jwt({"aud": blueprint_id, "azp": client_id, "oid": user, "tid": TENANT,
                                 "scp": "access_agent", "exp": time.time() + 3600, "sub": "user-pairwise-subject"})
                return {"access_token": token, "scope": " ".join(scopes),
                        "id_token_claims": {"tid": TENANT, "oid": user}}

        return Client()

    def token_post(self, url, *, data, **kwargs):
        self.exchanges.append(dict(data))
        self.assert_token_endpoint(url)
        if data["grant_type"] == "client_credentials":
            assert data["scope"] == demo.TOKEN_EXCHANGE_SCOPE
            blueprint = next(app for app in self.objects["applications"].values()
                             if app["appId"] == data["client_id"])
            assert any(blueprint["_secrets"].get(key["keyId"]) == data["client_secret"]
                       for key in blueprint["passwordCredentials"])
            assert any(sp.get("appId") == data["fmi_path"] and
                       sp.get("agentIdentityBlueprintId") == blueprint["appId"]
                       for sp in self.objects["servicePrincipals"].values())
            claims = {"aud": "api://AzureADTokenExchange", "azp": blueprint["appId"], "tid": TENANT,
                      "sub": "opaque-fmi-subject-not-containing-the-agent-guid", "exp": time.time() + 3600}
            return response({"access_token": jwt(claims), "token_type": "Bearer", "expires_in": 3600})
        assert data["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
        assert data["requested_token_use"] == "on_behalf_of"
        assert data["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        assert data["scope"] == demo.GRAPH + "/.default"
        tc = demo.decode_jwt(data["assertion"])
        t1 = demo.decode_jwt(data["client_assertion"])
        agent = next(sp for sp in self.objects["servicePrincipals"].values() if sp.get("appId") == data["client_id"])
        assert tc["aud"] == t1["azp"] == agent["agentIdentityBlueprintId"]
        grant = next(iter(self.objects["oauth2PermissionGrants"].values()))
        assert grant["clientId"] == agent["id"] and grant["principalId"] == tc["oid"]
        self.graph_token = ("opaque-graph-access-token" if self.opaque_graph else jwt({
            "aud": demo.GRAPH_APP_ID, "appid": agent["appId"], "oid": tc["oid"],
            "tid": TENANT, "scp": "User.Read", "exp": time.time() + 3600,
        }))
        return response({"access_token": self.graph_token, "scope": "User.Read", "expires_in": 3600})

    @staticmethod
    def assert_token_endpoint(url):
        assert url == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"

    def graph_request(self, method, url, *, json=None, params=None, headers=None, **kwargs):
        parts = urlparse(url).path.strip("/").split("/")
        assert headers["OData-Version"] == "4.0"
        authorization = headers["Authorization"]
        if parts[1] not in {"me", "users"}:
            assert authorization == f"Bearer {self.admin_token}"
        if parts[1] == "me":
            assert authorization == f"Bearer {self.admin_token}" or (
                self.graph_token is not None and authorization == f"Bearer {self.graph_token}"
            )
            user = USER if headers["Authorization"] == f"Bearer {self.admin_token}" else self.demo_user
            return response({"id": user, "displayName": "Offline demo member",
                             "userPrincipalName": "offline@example.invalid", "userType": "Member"})
        if parts[1] == "users":
            assert headers["Authorization"] == f"Bearer {self.graph_token}"
            return response({"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}}, 403)
        store = self.objects[parts[1]]
        item_id = parts[2] if len(parts) > 2 and not parts[2].startswith("microsoft.graph.") else None
        if method == "GET" and item_id:
            if item_id not in store:
                return self.not_found()
            item = self.public(store[item_id], params)
            if store[item_id].get("_type") == "microsoft.graph.agentIdentity" and self.pending_agent_reads:
                item["appId"] = None
                self.pending_agent_reads -= 1
            return response(item)
        if method == "GET":
            values = list(store.values())
            if len(parts) > 2:
                values = [value for value in values if value.get("_type") == parts[2]]
            if params and "$filter" in params:
                for field, value in demo.re.findall(r"(\w+) eq '([^']*)'", params["$filter"]):
                    values = [item for item in values if item.get(field) == value]
            return response({"value": [self.public(item, params) for item in values]})
        if method == "POST" and parts[-1] == "addPassword":
            key_id = self.new_id()
            credential = {"keyId": key_id, "secretText": f"offline-private-blueprint-credential-{key_id}",
                          **json["passwordCredential"]}
            store[item_id]["passwordCredentials"].append({key: value for key, value in credential.items()
                                                         if key != "secretText"})
            store[item_id].setdefault("_secrets", {})[key_id] = credential["secretText"]
            self.creates.append("credential")
            return response(credential)
        if method == "POST":
            item = {"id": self.new_id(), **copy.deepcopy(json)}
            if len(parts) > 2:
                item["_type"] = parts[2]
            if parts[1] == "applications":
                item.update(appId=self.new_id(), passwordCredentials=[])
                item.setdefault("api", {})
            elif parts[1] == "servicePrincipals":
                if item.get("_type") == "microsoft.graph.agentIdentity":
                    item.update(appId=self.new_id(), servicePrincipalType="ServiceIdentity")
                else:
                    parent = next(app for app in self.objects["applications"].values()
                                  if app["appId"] == item["appId"])
                    item.update(displayName=parent["displayName"], servicePrincipalType="Application")
            store[item["id"]] = item
            self.creates.append(item.get("_type", parts[1]))
            result = self.public(item)
            if item.get("_type") == "microsoft.graph.agentIdentity" and self.pending_agent_reads:
                result["appId"] = None
            return response(result, 201)
        if method == "PATCH":
            for key, value in json.items():
                if key == "api":
                    store[item_id].setdefault("api", {}).update(copy.deepcopy(value))
                else:
                    store[item_id][key] = copy.deepcopy(value)
            return response(status=204)
        if method == "DELETE":
            if item_id not in store:
                return self.not_found()
            del store[item_id]
            return response(status=204)
        raise AssertionError(f"Unhandled fake request: {method} {url}")

    @staticmethod
    def public(item, params=None):
        fields = set(params["$select"].split(",")) if params and "$select" in params else None
        return copy.deepcopy({key: value for key, value in item.items()
                              if not key.startswith("_") and (fields is None or key in fields)})

    @staticmethod
    def not_found():
        return response({"error": {"code": "Request_ResourceNotFound", "message": "not found"}}, 404)


class NotebookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.notebook = nbformat.read(ROOT.joinpath("entra_agent_obo.ipynb"), as_version=4)
        cls.code_cells = {cell.id: cell.source for cell in cls.notebook.cells if cell.cell_type == "code"}

    def test_notebook_schema_syntax_and_clean_outputs(self):
        nbformat.validate(self.notebook)
        for cell in self.notebook.cells:
            if cell.cell_type == "code":
                compile(cell.source, cell.id, "exec")
                self.assertIsNone(cell.execution_count)
                self.assertEqual(cell.outputs, [])
        self.assertIn("CLEANUP = False", self.code_cells["cleanup"])

    def test_readme_covers_every_permission_and_setup_file(self):
        readme = ROOT.joinpath("README.md").read_text(encoding="utf-8")
        for scope in set(demo.ADMIN_PERMISSIONS) | set(demo.CLEANUP_PERMISSIONS):
            self.assertIn(scope, readme)
        for name in ("requirements.txt", ".env.example", "demo_support.py"):
            self.assertTrue(ROOT.joinpath(name).is_file())
            self.assertIn(name, readme)

    @contextlib.contextmanager
    def notebook_session(self, backend=None, *, mode="device_code"):
        backend = backend or FakeTenant()
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            state_file = Path(temp).joinpath("state.json")
            stack.enter_context(patch.dict(os.environ, {
                "TENANT_ID": TENANT, "AGENT_PREFIX": PREFIX, "AUTH_MODE": mode,
                "AGENT_STATE_FILE": str(state_file),
                "DEMO_USER_OBJECT_ID": backend.demo_user, "BOOTSTRAP_CLIENT_ID": demo.BOOTSTRAP_CLIENT_ID,
            }, clear=True))
            stack.enter_context(patch.object(demo.requests, "get",
                return_value=response({"issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0"})))
            stack.enter_context(patch.object(demo.requests, "request", side_effect=backend.graph_request))
            stack.enter_context(patch.object(demo.requests, "post", side_effect=backend.token_post))
            stack.enter_context(patch.object(requests.sessions.Session, "request",
                side_effect=AssertionError("Unexpected live HTTP request in an offline notebook test.")))
            stack.enter_context(patch.object(demo.msal, "PublicClientApplication", side_effect=backend.public_client))
            stack.enter_context(patch("IPython.display.display"))
            stack.enter_context(patch.object(demo.time, "sleep"))
            stack.enter_context(patch("builtins.input", side_effect=lambda prompt:
                                     f"delete {PREFIX}" if "Type delete" in prompt else PREFIX))
            output = io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(output))
            namespace = {"__name__": "__notebook_test__"}
            yield backend, state_file, namespace, output

    def execute_cell(self, cell_id, namespace, *, cleanup=False):
        source = self.code_cells[cell_id]
        if cleanup:
            source = source.replace("CLEANUP = False", "CLEANUP = True", 1)
        exec(compile(source, cell_id, "exec"), namespace)

    def run_walkthrough(self, *, opaque_graph=False, mode="device_code", demo_user=USER, pending_agent_reads=0):
        backend = FakeTenant(opaque_graph=opaque_graph, demo_user=demo_user,
                             pending_agent_reads=pending_agent_reads)
        with self.notebook_session(backend, mode=mode) as (backend, state_file, namespace, output):
            for source in self.code_cells.values():
                exec(compile(source, "notebook", "exec"), namespace)
            creates = list(backend.creates)
            scope_id = namespace["SCOPE_ID"]
            for source in self.code_cells.values():
                exec(compile(source, "notebook-rerun", "exec"), namespace)
            self.assertEqual(backend.creates, creates, "A top-to-bottom rerun created duplicate objects or secrets.")
            self.assertEqual(namespace["SCOPE_ID"], scope_id)
            self.assertTrue(state_file.exists())
            self.assertEqual(len(backend.exchanges), 4)
            self.assertNotIn("offline-private-blueprint-credential", output.getvalue())
            for form in backend.exchanges:
                for key in ("assertion", "client_assertion"):
                    if key in form:
                        self.assertNotIn(form[key], output.getvalue())
            cleanup_namespace = {"__name__": "__cleanup_test__"}
            self.execute_cell("setup", cleanup_namespace)
            self.execute_cell("cleanup", cleanup_namespace, cleanup=True)
            self.assertFalse(state_file.exists())
            self.assertEqual(backend.objects["applications"], {})
            self.assertEqual(backend.objects["oauth2PermissionGrants"], {})
            remaining = list(backend.objects["servicePrincipals"].values())
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["appId"], demo.GRAPH_APP_ID)
            return output.getvalue()

    def test_full_notebook_replay_and_cleanup_with_mocked_transport(self):
        self.run_walkthrough()

    def test_full_notebook_with_opaque_graph_token_and_different_demo_user(self):
        output = self.run_walkthrough(opaque_graph=True, demo_user=OTHER_USER)
        self.assertIn("opaque/unreadable", output)

    def test_full_notebook_browser_authentication_option(self):
        self.run_walkthrough(mode="interactive")

    def test_full_notebook_waits_for_agent_app_id_propagation(self):
        output = self.run_walkthrough(pending_agent_reads=1)
        self.assertIn("object not ready", output)

    def test_scope_id_survives_a_stale_blueprint_api_read(self):
        with self.notebook_session() as (backend, state_file, namespace, output):
            for cell_id in ("setup", "admin-sign-in", "create-blueprint", "expose-blueprint-api"):
                self.execute_cell(cell_id, namespace)
            scope_id = namespace["SCOPE_ID"]
            stale = copy.deepcopy(namespace["blueprint"])
            stale["api"] = {}
            with patch.object(namespace["admin"], "get", return_value=stale):
                self.execute_cell("expose-blueprint-api", namespace)
            self.assertEqual(namespace["SCOPE_ID"], scope_id)
            self.assertEqual(namespace["state"].get("scope_id"), scope_id)

    def test_changed_scope_id_stops_without_overwriting_saved_state(self):
        with self.notebook_session() as (backend, state_file, namespace, output):
            for cell_id in ("setup", "admin-sign-in", "create-blueprint", "expose-blueprint-api"):
                self.execute_cell(cell_id, namespace)
            scope_id = namespace["SCOPE_ID"]
            blueprint = backend.objects["applications"][namespace["BLUEPRINT_OBJECT_ID"]]
            blueprint["api"]["oauth2PermissionScopes"][0]["id"] = OTHER_USER
            with patch.object(namespace["admin"], "patch") as update:
                with self.assertRaises(demo.DemoError):
                    self.execute_cell("expose-blueprint-api", namespace)
                update.assert_not_called()
            self.assertEqual(namespace["state"].get("scope_id"), scope_id)

    def test_cleanup_rejects_incomplete_or_forbidden_bootstrap_consent(self):
        for granted in (
            ["User.Read"],
            [*demo.CLEANUP_PERMISSIONS, "Directory.AccessAsUser.All"],
        ):
            with self.subTest(granted=granted), self.notebook_session() as (_, _, namespace, _):
                self.execute_cell("setup", namespace)
                result = {"access_token": "offline-cleanup-token", "scope": " ".join(granted),
                          "id_token_claims": {"tid": TENANT, "oid": USER}}
                namespace["sign_in"] = Mock(return_value=result)
                namespace["cleanup_demo"] = Mock()
                with self.assertRaises(demo.DemoError):
                    self.execute_cell("cleanup", namespace, cleanup=True)
                namespace["cleanup_demo"].assert_not_called()

    def test_setup_and_failed_admin_sign_in_clear_old_provisioning_approval(self):
        for cell_id in ("setup", "admin-sign-in"):
            with self.subTest(cell=cell_id), self.notebook_session() as (_, _, namespace, _):
                self.execute_cell("setup", namespace)
                namespace["PROVISIONING_APPROVED"] = True
                if cell_id == "admin-sign-in":
                    namespace["sign_in"] = Mock(side_effect=demo.DemoError("Sign-in cancelled."))
                    with self.assertRaises(demo.DemoError):
                        self.execute_cell(cell_id, namespace)
                else:
                    self.execute_cell(cell_id, namespace)
                self.assertFalse(namespace["PROVISIONING_APPROVED"])

    def test_cleanup_clears_provisioning_approval_even_when_deletion_fails(self):
        with self.notebook_session() as (_, _, namespace, _):
            self.execute_cell("setup", namespace)
            namespace["PROVISIONING_APPROVED"] = True
            namespace["cleanup_demo"] = Mock(side_effect=demo.DemoError("Cleanup failed."))
            with self.assertRaises(demo.DemoError):
                self.execute_cell("cleanup", namespace, cleanup=True)
            self.assertFalse(namespace["PROVISIONING_APPROVED"])

    def test_scope_id_is_saved_before_a_failed_graph_update(self):
        with self.notebook_session() as (_, _, namespace, _):
            for cell_id in ("setup", "admin-sign-in", "create-blueprint"):
                self.execute_cell(cell_id, namespace)
            with patch.object(namespace["admin"], "patch", side_effect=demo.ApiError(
                503, "ServiceUnavailable", "Update outcome unknown."
            )):
                with self.assertRaises(demo.ApiError):
                    self.execute_cell("expose-blueprint-api", namespace)
            scope_id = namespace["state"].get("scope_id")
            self.assertIsNotNone(scope_id)
            self.execute_cell("expose-blueprint-api", namespace)
            self.assertEqual(namespace["SCOPE_ID"], scope_id)

    def test_expired_missing_and_explicitly_rotated_credentials_are_replaced(self):
        for reason in ("expired", "missing", "explicit"):
            with self.subTest(reason=reason), self.notebook_session() as (backend, _, namespace, _):
                for cell_id in ("setup", "admin-sign-in", "create-blueprint", "blueprint-credential"):
                    self.execute_cell(cell_id, namespace)
                saved = dict(namespace["state"].get("blueprint_credential"))
                blueprint = backend.objects["applications"][namespace["BLUEPRINT_OBJECT_ID"]]
                if reason == "expired":
                    blueprint["passwordCredentials"][0]["endDateTime"] = "2000-01-01T00:00:00Z"
                elif reason == "missing":
                    blueprint["passwordCredentials"] = []
                source = self.code_cells["blueprint-credential"]
                if reason == "explicit":
                    source = source.replace("ROTATE_SECRET = False", "ROTATE_SECRET = True", 1)
                exec(compile(source, "blueprint-credential", "exec"), namespace)
                replacement = namespace["state"].get("blueprint_credential")
                self.assertNotEqual(replacement["key_id"], saved["key_id"])
                self.assertNotEqual(replacement["secret"], saved["secret"])
                self.assertEqual(replacement["app_id"], namespace["BLUEPRINT_APP_ID"])
                self.assertEqual(backend.creates.count("credential"), 2)
                if reason == "explicit":
                    self.assertIn(saved["key_id"], [key["keyId"] for key in blueprint["passwordCredentials"]])
                self.execute_cell("blueprint-credential", namespace)
                self.assertEqual(backend.creates.count("credential"), 2)

    def test_tc_rejects_wrong_audience_user_tenant_client_scope_and_expiry(self):
        with self.notebook_session() as (_, _, namespace, _):
            for cell_id in self.code_cells:
                self.execute_cell(cell_id, namespace)
            valid = dict(namespace["tc_claims"])
            for change in ({"aud": demo.GRAPH_APP_ID}, {"oid": OTHER_USER}, {"tid": OTHER_USER},
                           {"azp": OTHER_USER}, {"scp": "User.Read"}, {"exp": time.time() - 1}):
                with self.subTest(change=change):
                    namespace["sign_in"] = Mock(return_value={"access_token": jwt({**valid, **change})})
                    with self.assertRaises(demo.DemoError):
                        self.execute_cell("acquire-tc", namespace)

    def test_tg_rejects_wrong_audience_user_tenant_actor_and_extra_permissions(self):
        with self.notebook_session() as (_, _, namespace, _):
            for cell_id in self.code_cells:
                self.execute_cell(cell_id, namespace)
            valid = dict(namespace["tg_claims"])
            for change in ({"aud": USER}, {"oid": OTHER_USER}, {"tid": OTHER_USER},
                           {"appid": USER}, {"scp": "User.Read Mail.Read"}, {"scp": ""}):
                with self.subTest(change=change):
                    namespace["request_token"] = Mock(return_value={"access_token": jwt({**valid, **change})})
                    with self.assertRaises(demo.DemoError):
                        self.execute_cell("acquire-tg", namespace)

    def test_opaque_graph_token_without_scope_metadata_still_calls_graph(self):
        backend = FakeTenant(opaque_graph=True)
        with self.notebook_session(backend) as (_, _, namespace, output):
            for cell_id in self.code_cells:
                if cell_id == "acquire-tg":
                    original = namespace["request_token"]

                    def without_scope(*args, **kwargs):
                        result = original(*args, **kwargs)
                        result.pop("scope", None)
                        return result

                    namespace["request_token"] = without_scope
                self.execute_cell(cell_id, namespace)
            self.assertIn("Effective scopes are not inspectable here", output.getvalue())
            self.assertIn("GET /me succeeded using Tg", output.getvalue())
            self.assertIn("Expected denial: HTTP 403", output.getvalue())

    def test_cleanup_after_partial_setup_needs_only_setup_in_a_fresh_namespace(self):
        with self.notebook_session() as (backend, state_file, namespace, _):
            for cell_id in ("setup", "admin-sign-in", "create-blueprint", "blueprint-credential"):
                self.execute_cell(cell_id, namespace)
            cleanup_namespace = {"__name__": "__partial_cleanup_test__"}
            self.execute_cell("setup", cleanup_namespace)
            self.execute_cell("cleanup", cleanup_namespace, cleanup=True)
            self.assertFalse(state_file.exists())
            self.assertEqual(backend.objects["applications"], {})

    def test_partial_cleanup_can_be_resumed_after_restart(self):
        with self.notebook_session() as (backend, state_file, namespace, _):
            for cell_id in self.code_cells:
                self.execute_cell(cell_id, namespace)
            client_id = namespace["CLIENT_APP_OBJECT_ID"]

            def fail_client_delete(method, url, **kwargs):
                if method == "DELETE" and url.endswith(f"/applications/{client_id}"):
                    return response({"error": {"code": "Authorization_RequestDenied", "message": "denied"}}, 403)
                return backend.graph_request(method, url, **kwargs)

            with patch.object(demo.requests, "request", side_effect=fail_client_delete):
                with self.assertRaises(demo.ApiError):
                    self.execute_cell("cleanup", namespace, cleanup=True)
            self.assertTrue(state_file.exists())
            self.assertTrue(namespace["state"].get("cleanup_started"))
            self.assertEqual(backend.objects["oauth2PermissionGrants"], {})
            self.assertNotIn(namespace["AGENT_ID"], backend.objects["servicePrincipals"])
            cleanup_namespace = {"__name__": "__resumed_cleanup_test__"}
            self.execute_cell("setup", cleanup_namespace)
            self.execute_cell("cleanup", cleanup_namespace, cleanup=True)
            self.assertFalse(state_file.exists())
            self.assertEqual(backend.objects["applications"], {})
            self.assertEqual(len(backend.objects["servicePrincipals"]), 1)

    def test_svg_and_excalidraw_labels_and_bindings_agree(self):
        drawing = json.loads(ROOT.joinpath("entra-obo-token-exchange.excalidraw").read_text(encoding="utf-8"))
        svg = ET.parse(ROOT.joinpath("entra-obo-token-exchange.svg"))
        svg_labels = ["".join(element.itertext()) for element in svg.iter("{http://www.w3.org/2000/svg}text")]
        drawing_labels = [element["text"] for element in drawing["elements"] if element["type"] == "text"]
        self.assertEqual(sorted(svg_labels), sorted(drawing_labels))
        ids = {element["id"] for element in drawing["elements"]}
        self.assertEqual(len(ids), len(drawing["elements"]))
        for element in drawing["elements"]:
            for binding in element.get("boundElements", []):
                self.assertIn(binding["id"], ids)
            if element.get("containerId"):
                self.assertIn(element["containerId"], ids)


if __name__ == "__main__":
    unittest.main()
