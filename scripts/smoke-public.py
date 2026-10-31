"""Verify public IP HTTPS with normal certificate and hostname validation."""
from pathlib import Path
import base64
import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

root = Path(__file__).resolve().parents[1]
env = dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines() if line and not line.startswith("#"))
ip = env.get("PUBLIC_IP", "")
assert ip, "PUBLIC_IP is not configured"
credential = base64.b64encode(("training:" + env["MEDIA_PASSWORD"]).encode()).decode()
headers = {"Authorization": "Basic " + credential}

for path in ("/", "/training-config.js", "/js/main.js"):
    try:
        with urlopen(f"https://{ip}" + path, timeout=15):
            raise AssertionError("Removed web client is still served")
    except HTTPError as error:
        assert error.code == 404, error.code
        error.close()
print("PASS hosted web client is absent; trusted HTTPS remains available")

try:
    with urlopen(f"https://{ip}/media/0", timeout=15):
        raise AssertionError("Attachment accepted an unauthenticated visitor")
except HTTPError as error:
    assert error.code == 401
    assert error.headers.get("WWW-Authenticate", "").startswith("Basic")
    error.close()
print("PASS attachments require Basic authentication")

with urlopen(f"https://{ip}:8443/health", timeout=15) as response:
    assert response.status == 200
    if env.get("LOGSEQ_SYNC_IMAGE") in ("training-hub-logseq-sync:69da7e-selfhost3", "training-hub-logseq-sync:69da7e-selfhost4", "training-hub-logseq-sync:69da7e-selfhost5", "training-hub-logseq-sync:69da7e-selfhost6"):
        health = json.load(response)
        assert health.get("account-allowlist") is True
        if env.get("LOGSEQ_SYNC_IMAGE") in ("training-hub-logseq-sync:69da7e-selfhost4", "training-hub-logseq-sync:69da7e-selfhost5", "training-hub-logseq-sync:69da7e-selfhost6"):
            assert health.get("journal-checklists") is True
            if env.get("LOGSEQ_SYNC_IMAGE") in ("training-hub-logseq-sync:69da7e-selfhost5", "training-hub-logseq-sync:69da7e-selfhost6"):
                assert health.get("shared-graphs") is True
                if env.get("LOGSEQ_SYNC_IMAGE") == "training-hub-logseq-sync:69da7e-selfhost6":
                    assert health.get("boolean-checklists") is True
print("PASS sync HTTPS")

for path in ("/training/graphs", "/training/allowlist", "/admin/graphs/00000000-0000-4000-8000-000000000000"):
    try:
        with urlopen(f"https://{ip}:8443" + path, timeout=15):
            raise AssertionError("Public management endpoint accepted a request")
    except HTTPError as error:
        assert error.code == 404, error.code
        error.close()
print("PASS sync management endpoints are private")

for url in (f"https://{ip}:8443/api/v1/graphs", f"https://{ip}/media/0"):
    try:
        with urlopen(url, timeout=15):
            raise AssertionError("Unauthenticated access was accepted")
    except HTTPError as error:
        assert error.code == 401, error.code
        error.close()
print("PASS graph API and attachments still require authentication")

fake_claims = {"sub": "00000000-0000-4000-8000-000000000000", "exp": int(time.time()) + 600,
               "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_dtagLnju8",
               "aud": "69cs1lgme7p8kbgld8n5kseii6", "token_use": "id"}
if env.get("LOGSEQ_SYNC_IMAGE") in ("training-hub-logseq-sync:69da7e-selfhost3", "training-hub-logseq-sync:69da7e-selfhost4", "training-hub-logseq-sync:69da7e-selfhost5", "training-hub-logseq-sync:69da7e-selfhost6"):
    policy = dict(line.split("=", 1) for line in (root / ".secrets/sync-policy.env").read_text().splitlines() if "=" in line)
    fake_claims.update(email=policy["TRAINING_OWNER_EMAIL"], email_verified=True)
encoded = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
unsigned_token = encoded({"alg": "none"}) + "." + encoded(fake_claims) + "."
for authorization in (None, "Bearer " + unsigned_token):
    request_headers = {"Content-Type": "application/json"}
    if authorization:
        request_headers["Authorization"] = authorization
    request = Request(f"https://{ip}:8443/api/v1/personal-access-tokens", data=b"{}", headers=request_headers)
    try:
        with urlopen(request, timeout=15):
            raise AssertionError("Token creation accepted an unverified identity")
    except HTTPError as error:
        assert error.code == 401, error.code
        error.close()
print("PASS token creation rejects missing and unsigned login tokens")

if env.get("LOGSEQ_SYNC_IMAGE") in ("training-hub-logseq-sync:69da7e-selfhost3", "training-hub-logseq-sync:69da7e-selfhost4", "training-hub-logseq-sync:69da7e-selfhost5", "training-hub-logseq-sync:69da7e-selfhost6"):
    token = (root / ".secrets/logseq-token").read_text().strip()
    key = json.loads((root / ".secrets/logseq-encryption.json").read_text())
    assert token and key["graph_id"] == env["LOGSEQ_GRAPH_ID"]
    request = Request(f"https://{ip}:8443/api/v1/graphs/{key['graph_id']}/e2ee/encrypt",
                      data=json.dumps({"key": key["key"], "texts": ["sync access check"]}).encode(),
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urlopen(request, timeout=15) as response:
        result = json.load(response)
        assert len(result["texts"]) == 1 and isinstance(result["texts"][0], str)
    print("PASS allowed account's existing PAT still authenticates without writing graph data")

graph = env.get("LOGSEQ_GRAPH_ID") or "00000000-0000-4000-8000-000000000000"
snapshot = f"https://{ip}:8443/sync/{graph}/snapshot/stream"
preflight = Request(snapshot, method="OPTIONS", headers={"Origin": f"https://{ip}",
                    "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"})
with urlopen(preflight, timeout=15) as response:
    assert response.status == 204
    assert response.headers.get("Access-Control-Allow-Origin") in ("*", f"https://{ip}")
    assert "authorization" in response.headers.get("Access-Control-Allow-Headers", "").lower()
try:
    with urlopen(snapshot, timeout=15):
        raise AssertionError("Snapshot accepted an unauthenticated download")
except HTTPError as error:
    assert error.code == 401, error.code
    error.close()
print("PASS snapshot allows browser preflight and still requires authentication")

try:
    with urlopen(Request(f"https://{ip}/media/0", headers=headers), timeout=15):
        raise AssertionError("Missing attachment unexpectedly exists")
except HTTPError as error:
    assert error.code == 404, "Attachment authentication rejected MEDIA_PASSWORD"
    error.close()
print("PASS attachment password still authenticates downloads")
