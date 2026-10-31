"""Check the private stack without Telegram or Logseq credentials."""
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

root = Path(__file__).resolve().parents[1]
env = dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines() if line and not line.startswith("#"))


def get(url, headers=None):
    return urlopen(Request(url, headers=headers or {}), timeout=10)


for name, url in [("inbox", "http://localhost:8001/health"), ("n8n", "http://localhost:5678/healthz"),
                  ("Logseq sync", "http://localhost:8787/health")]:
    with get(url) as response:
        assert response.status == 200, name
    print(f"PASS {name}")

for path in ("http://localhost:8001/pending", "http://localhost:8001/media/1", "http://localhost:8787/api/v1/graphs"):
    try:
        with get(path):
            raise AssertionError(f"Unauthenticated access accepted: {path}")
    except HTTPError as error:
        assert error.code == 401, (path, error.code)
        error.close()
print("PASS inbox, attachments, and graph API reject unauthenticated requests")

if env.get("LOGSEQ_SYNC_IMAGE") in ("training-hub-logseq-sync:69da7e-selfhost3", "training-hub-logseq-sync:69da7e-selfhost4", "training-hub-logseq-sync:69da7e-selfhost5", "training-hub-logseq-sync:69da7e-selfhost6"):
    with get("http://localhost:8787/health") as response:
        health = json.load(response)
        assert health.get("account-allowlist") is True
        if env.get("LOGSEQ_SYNC_IMAGE") in ("training-hub-logseq-sync:69da7e-selfhost4", "training-hub-logseq-sync:69da7e-selfhost5", "training-hub-logseq-sync:69da7e-selfhost6"):
            assert health.get("journal-checklists") is True
            if env.get("LOGSEQ_SYNC_IMAGE") in ("training-hub-logseq-sync:69da7e-selfhost5", "training-hub-logseq-sync:69da7e-selfhost6"):
                assert health.get("shared-graphs") is True
                if env.get("LOGSEQ_SYNC_IMAGE") == "training-hub-logseq-sync:69da7e-selfhost6":
                    assert health.get("boolean-checklists") is True
    policy = dict(line.split("=", 1) for line in (root / ".secrets/sync-policy.env").read_text().splitlines() if "=" in line)
    with get("http://localhost:8787/training/allowlist", {"Authorization": "Bearer " + policy["TRAINING_ALLOWLIST_TOKEN"]}) as response:
        assert json.load(response)["owner"] == policy["TRAINING_OWNER_EMAIL"].strip().lower()
    print("PASS hardened worker and private account policy management")

with get("http://localhost:8001/pending", {"Authorization": "Bearer " + env["BRIDGE_TOKEN"]}) as response:
    assert isinstance(json.load(response)["submissions"], list)
print("PASS authenticated inbox access")
