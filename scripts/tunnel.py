"""Forward private administration ports for the deployed Lightsail host."""
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
state = json.loads((root / ".secrets/lightsail.json").read_text())
command = ["ssh", "-F", "/dev/null", "-N", "-i", str(root / ".secrets/lightsail_ssh"),
           "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
           "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={root / '.secrets/known_hosts'}"]
for port in (5678, 8787, 8001):
    command.extend(["-L", f"127.0.0.1:{port}:127.0.0.1:{port}"])
command.append("ubuntu@" + state["ip"])
print("n8n: http://localhost:5678 | Logseq sync: http://localhost:8787", flush=True)
subprocess.run(command, check=True)
