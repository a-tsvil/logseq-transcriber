"""Prepare public browser authentication using the existing attachment password."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
path = root / ".env"
lines = path.read_text().splitlines()
env = dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))
if not env.get("PUBLIC_IP"):
    raise SystemExit(0)
password = env.get("MEDIA_PASSWORD", "")
if not password:
    raise SystemExit("MEDIA_PASSWORD must be set before enabling browser access.")
result = subprocess.run(["docker", "run", "--rm", "-i", env["CADDY_IMAGE"], "caddy", "hash-password"],
                        input=password + "\n", capture_output=True, text=True, check=True)
hashed = result.stdout.strip()
if not hashed.startswith(("$2a$", "$2b$", "$2y$")) or "\n" in hashed:
    raise SystemExit("Caddy did not return a valid bcrypt password hash.")
# Single quotes stop Compose from interpreting bcrypt's dollar signs as variables.
setting = "WEB_PASSWORD_HASH='" + hashed + "'"
if "WEB_PASSWORD_HASH" in env:
    lines = [setting if line.startswith("WEB_PASSWORD_HASH=") else line for line in lines]
else:
    lines.append(setting)
path.write_text("\n".join(lines) + "\n")
print("Configured browser authentication; password was not displayed.")
