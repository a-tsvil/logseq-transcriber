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

try:
    with urlopen("https://" + ip, timeout=15):
        raise AssertionError("Browser accepted an unauthenticated visitor")
except HTTPError as error:
    assert error.code == 401
    assert error.headers.get("WWW-Authenticate", "").startswith("Basic")
    error.close()
print("PASS browser requires basic authentication")

with urlopen(Request("https://" + ip, headers=headers), timeout=15) as response:
    assert response.status == 200
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Cross-Origin-Embedder-Policy"] == "require-corp"
    assert '<head><script src="/training-config.js"></script>' in response.read().decode()
print("PASS browser HTTPS, trusted IP certificate, and isolation headers")

with urlopen(Request(f"https://{ip}/training-config.js", headers=headers), timeout=15) as response:
    assert "sync-server-url" in response.read().decode()
    assert "no-cache" in response.headers.get("Cache-Control", "")
print("PASS sync configuration loads before Logseq without a stale browser cache")

with urlopen(f"https://{ip}:8443/health", timeout=15) as response:
    assert response.status == 200
print("PASS sync HTTPS")

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
    assert error.code == 404, "Attachment authentication rejected the browser password"
    error.close()
print("PASS browser password also authenticates attachment downloads")
