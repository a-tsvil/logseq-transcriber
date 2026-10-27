"""Unlock only the selected graph key locally; never save the account password/private key."""
import getpass
import json
from pathlib import Path
import subprocess
import uuid
import warnings

root = Path(__file__).resolve().parents[1]
env = dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines() if line and not line.startswith("#"))
graph_id = str(uuid.UUID(env["LOGSEQ_GRAPH_ID"]))
target = root / ".secrets/logseq-encryption.json"
if target.exists() and target.stat().st_size:
    raise SystemExit("Graph key file already exists; refusing to overwrite it.")
state = json.loads((root / ".secrets/lightsail.json").read_text())
ssh = ["ssh", "-F", "/dev/null", "-i", str(root / ".secrets/lightsail_ssh"), "-o", "BatchMode=yes",
       "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=yes",
       "-o", "UserKnownHostsFile=" + str(root / ".secrets/known_hosts"), "ubuntu@" + state["ip"], "python3 -"]
# Read the graph owner's existing encrypted key packages, without modifying the database.
remote = '''import json, pathlib, sqlite3
paths=list(pathlib.Path('/opt/training-hub/data/logseq/wrangler/v3/d1/miniflare-D1DatabaseObject').glob('*.sqlite'))
for path in paths:
 db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
 if not db.execute("SELECT 1 FROM sqlite_master WHERE name='graph_aes_keys'").fetchone(): continue
 row=db.execute("SELECT g.graph_id,r.encrypted_private_key,a.encrypted_aes_key FROM graphs g JOIN user_rsa_keys r ON r.user_id=g.user_id JOIN graph_aes_keys a ON a.graph_id=g.graph_id AND a.user_id=g.user_id WHERE g.graph_id=? AND g.graph_e2ee=1", (GRAPH_ID,)).fetchone()
 if row:
  print(json.dumps(dict(zip(('graph_id','encrypted_private_key','encrypted_aes_key'),row))))
  break
else: raise SystemExit('Encrypted graph key not found')
'''.replace("GRAPH_ID", repr(graph_id))
bundle = json.loads(subprocess.run(ssh, input=remote, text=True, capture_output=True, check=True).stdout)
print("This saves this graph's key for the bot. Your password and account private key remain unsaved.")
with warnings.catch_warnings():
    warnings.simplefilter("error", getpass.GetPassWarning)
    password = getpass.getpass("Logseq encryption password (hidden): ")
result = subprocess.run(["node", str(root / "scripts/unlock-graph-key.mjs")],
                        input=json.dumps({"bundle": bundle, "password": password}), text=True, capture_output=True)
if result.returncode:
    raise SystemExit("Could not unlock graph key; check the encryption password. No file was written.")
key = json.loads(result.stdout)
assert key["graph_id"] == graph_id
target.touch(mode=0o600, exist_ok=True)
target.chmod(0o600)
target.write_text(json.dumps(key) + "\n")
print("Saved .secrets/logseq-encryption.json. Keep this file private.")
