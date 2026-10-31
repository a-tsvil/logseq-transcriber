"""Create local configuration once; never overwrite credentials."""
import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
os.umask(0o077)
for folder in (".secrets", ".secrets/telegram-index", "data/inbox", "data/n8n", "data/logseq", "data/caddy", "data/caddy-config"):
    (root / folder).mkdir(parents=True, exist_ok=True)
(root / ".secrets/logseq-token").touch(exist_ok=True)
(root / ".secrets/logseq-encryption.json").touch(exist_ok=True)
(root / ".secrets/gemini-api-key").touch(exist_ok=True)
graphs = root / ".secrets/logseq-graphs.json"
if not graphs.exists():
    graphs.write_text("[]\n")
policy = root / ".secrets/sync-policy.env"
if not policy.exists():
    policy.write_text("TRAINING_OWNER_EMAIL=\nTRAINING_ALLOWLIST_TOKEN=" + secrets.token_hex(32) + "\n")
env = root / ".env"
if not env.exists():
    template = (root / ".env.example").read_text()
    for name in ("BRIDGE_TOKEN", "MEDIA_PASSWORD", "N8N_ENCRYPTION_KEY"):
        template = template.replace(f"{name}=replace-with-generated-secret", f"{name}={secrets.token_hex(32)}")
    with env.open("x") as stream:
        stream.write(template)
    print("Created .env with generated secrets; add Telegram credentials when ready.")
else:
    print("Existing .env preserved.")
