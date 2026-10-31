"""Create one isolated Lightsail host and deploy the portable Compose stack."""
import argparse
import base64
import ipaddress
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--profile", default="ctd")
parser.add_argument("--region", default="us-east-1")
parser.add_argument("--name", default="training-hub-dev")
parser.add_argument("--public-ip", action="store_true", help="Attach a static IP and enable public HTTPS (account-restricted with the hardened worker)")
args = parser.parse_args()


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def aws(*command):
    try:
        result = run(["aws", "--profile", args.profile, "--region", args.region, "lightsail", *command, "--output", "json"], capture_output=True)
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.stderr.strip() or "AWS request failed") from None
    return json.loads(result.stdout)


subprocess.run(["python3", "scripts/setup.py"], cwd=ROOT, check=True)
key = ROOT / ".secrets/lightsail_ssh"
state_file = ROOT / ".secrets/lightsail.json"
state = {"name": args.name, "region": args.region, "profile": args.profile}
existing = {item["name"] for item in aws("get-instances")["instances"]}
if args.name in existing and not state_file.exists():
    raise SystemExit("An instance with this name already exists; refusing to modify an untracked host.")
if state_file.exists():
    state = json.loads(state_file.read_text())
    if (state["name"], state["region"], state["profile"]) != (args.name, args.region, args.profile):
        raise SystemExit("Deployment state belongs to a different host/profile; refusing to overwrite it.")
if args.name not in existing:
    bundles = aws("get-bundles")["bundles"]
    bundle = next(item for item in bundles if item["bundleId"] == "small_3_0")
    if bundle["price"] > 12 or not bundle["isActive"]:
        raise SystemExit("Expected active $12/month bundle; price or availability changed.")
    key_name = args.name + "-ssh"
    known_keys = {item["name"] for item in aws("get-key-pairs")["keyPairs"]}
    if key_name not in known_keys:
        if key.exists():
            raise SystemExit("Local deployment key already exists but is absent from Lightsail; refusing to replace it.")
        generated = aws("create-key-pair", "--key-pair-name", key_name)
        raw_key = generated["privateKeyBase64"]
        private_key = raw_key.encode() if raw_key.startswith("-----BEGIN ") else base64.b64decode(raw_key)
        if b"PRIVATE KEY" not in private_key[:100]:
            raise SystemExit("Lightsail returned an unexpected private key format.")
        key.touch(mode=0o600, exist_ok=False)
        key.write_bytes(private_key)
    elif not key.exists():
        raise SystemExit("Lightsail deployment key exists but its local private key is missing.")
    print(f"Creating {args.name}: $12/month before tax, {args.region}.", flush=True)
    aws("create-instances", "--instance-names", args.name, "--availability-zone", args.region + "a",
        "--blueprint-id", "ubuntu_24_04", "--bundle-id", "small_3_0", "--key-pair-name", key_name,
        "--user-data", "file://" + str(ROOT / "scripts/bootstrap.sh"),
        "--tags", "key=project,value=training-hub", "key=environment,value=dev")
    state_file.write_text(json.dumps({"name": args.name, "region": args.region, "profile": args.profile}))
    state_file.chmod(0o600)

with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as response:
    operator_ip = response.read().decode().strip()
ipaddress.IPv4Address(operator_ip)
for _ in range(90):
    instance = aws("get-instance", "--instance-name", args.name)["instance"]
    if instance["state"]["name"] == "running" and instance.get("publicIpAddress"):
        break
    time.sleep(5)
else:
    raise SystemExit("Instance is not running yet. Rerun this script to resume.")
ip = instance["publicIpAddress"]
if args.public_ip or state.get("static_ip_name"):
    static_name = state.get("static_ip_name", args.name + "-ip")
    available = {item["name"]: item for item in aws("get-static-ips")["staticIps"]}
    if static_name not in available:
        aws("allocate-static-ip", "--static-ip-name", static_name)
    static = aws("get-static-ip", "--static-ip-name", static_name)["staticIp"]
    if static.get("attachedTo") not in (None, args.name):
        raise SystemExit("The static IP belongs to another instance; refusing to reassign it.")
    if static.get("attachedTo") != args.name:
        aws("attach-static-ip", "--static-ip-name", static_name, "--instance-name", args.name)
    ip = static["ipAddress"]
    state["static_ip_name"] = static_name
    env_path = ROOT / ".env"
    values = {"PUBLIC_IP": ip, "COMPOSE_PROFILES": "public", "MEDIA_BASE_URL": "https://" + ip}
    lines = env_path.read_text().splitlines()
    names = {line.split("=", 1)[0] for line in lines if "=" in line and not line.startswith("#")}
    lines = [line.split("=", 1)[0] + "=" + values[line.split("=", 1)[0]]
             if line.split("=", 1)[0] in values else line for line in lines]
    lines.extend(name + "=" + value for name, value in values.items() if name not in names)
    if "CADDY_IMAGE" not in names:
        lines.append("CADDY_IMAGE=caddy:2.11.7-alpine")
    env_path.write_text("\n".join(lines) + "\n")
    print("Static IP: " + ip, flush=True)
state["ip"] = ip
state_file.write_text(json.dumps(state, indent=2))
state_file.chmod(0o600)
sync_image = next(line.split("=", 1)[1] for line in (ROOT / ".env").read_text().splitlines()
                  if line.startswith("LOGSEQ_SYNC_IMAGE="))
account_restricted = sync_image in ("training-hub-logseq-sync:69da7e-selfhost3", "training-hub-logseq-sync:69da7e-selfhost4", "training-hub-logseq-sync:69da7e-selfhost5", "training-hub-logseq-sync:69da7e-selfhost6")
if account_restricted:
    policy = dict(line.split("=", 1) for line in (ROOT / ".secrets/sync-policy.env").read_text().splitlines() if "=" in line)
    if not policy.get("TRAINING_OWNER_EMAIL") or len(policy.get("TRAINING_ALLOWLIST_TOKEN", "")) < 64:
        raise SystemExit("Configure .secrets/sync-policy.env before exposing the account-restricted worker.")
ports = [{"fromPort": 22, "toPort": 22, "protocol": "tcp", "cidrs": [operator_ip + "/32"]}]
if state.get("static_ip_name"):
    ports.append({"fromPort": 80, "toPort": 80, "protocol": "tcp", "cidrs": ["0.0.0.0/0"]})
    ports.extend({"fromPort": port, "toPort": port, "protocol": "tcp", "cidrs": ["0.0.0.0/0"] if account_restricted else [operator_ip + "/32"]}
                 for port in (443, 8443))
# Keep application ports IP-restricted until the deployed worker passes its checks.
initial_ports = [{**port, "cidrs": [operator_ip + "/32"]} if port["fromPort"] in (443, 8443) else port
                 for port in ports]
aws("put-instance-public-ports", "--instance-name", args.name, "--port-infos", json.dumps(initial_ports))
if not key.exists():
    raise SystemExit("The local deployment SSH key is missing.")
ssh = ["ssh", "-F", "/dev/null", "-i", str(key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new",
       "-o", f"UserKnownHostsFile={ROOT / '.secrets/known_hosts'}", "ubuntu@" + ip]
print(f"Waiting for SSH on {ip}...", flush=True)
for _ in range(90):
    result = subprocess.run([*ssh, "true"], capture_output=True)
    if result.returncode == 0:
        break
    time.sleep(5)
else:
    raise SystemExit("SSH is not ready. Inspect instance status; rerun to resume.")
print("Installing Docker if bootstrap has not completed...", flush=True)
run([*ssh, "sudo cloud-init status --wait >/dev/null 2>&1 || true; if [ ! -f /var/lib/training-hub-ready ]; then sudo sh -s; fi"],
    input=(ROOT / "scripts/bootstrap.sh").read_text())

with tempfile.TemporaryDirectory() as temporary:
    sync_image = next(line.split("=", 1)[1] for line in (ROOT / ".env").read_text().splitlines()
                      if line.startswith("LOGSEQ_SYNC_IMAGE="))
    if sync_image in ("training-hub-logseq-sync:69da7e-selfhost1", "training-hub-logseq-sync:69da7e-selfhost2", "training-hub-logseq-sync:69da7e-selfhost3", "training-hub-logseq-sync:69da7e-selfhost4", "training-hub-logseq-sync:69da7e-selfhost5", "training-hub-logseq-sync:69da7e-selfhost6"):
        image_id = run(["docker", "image", "inspect", sync_image, "--format", "{{.Id}}"], capture_output=True).stdout.strip()
        remote_id = subprocess.run([*ssh, "docker image inspect " + sync_image + " --format '{{.Id}}'"],
                                   capture_output=True, text=True)
        if remote_id.returncode or remote_id.stdout.strip() != image_id:
            image_archive = Path(temporary) / "sync-worker.tar"
            run(["docker", "save", "-o", str(image_archive), sync_image])
            with image_archive.open("rb") as stream:
                subprocess.run([*ssh, "docker load"], stdin=stream, check=True)
    archive = Path(temporary) / "training-hub.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for entry in ("compose.yaml", "Dockerfile", "requirements.txt", "requirements-import.txt", ".dockerignore", "README.md", "src", "workflows", "deploy", "scripts/setup.py", "scripts/pin-images.py", "scripts/smoke.py", ".env", ".secrets/logseq-token", ".secrets/logseq-encryption.json", ".secrets/gemini-api-key", ".secrets/sync-policy.env", ".secrets/logseq-graphs.json"):
            # An empty local placeholder must not erase a key configured on the server.
            if entry == ".secrets/gemini-api-key" and not (ROOT / entry).read_bytes().strip():
                continue
            output.add(ROOT / entry, arcname=entry)
    with archive.open("rb") as stream:
        subprocess.run([*ssh, "umask 022; tar xzf - -C /opt/training-hub && touch /opt/training-hub/.secrets/gemini-api-key && chmod 700 /opt/training-hub/.secrets && chmod 600 /opt/training-hub/.env /opt/training-hub/.secrets/logseq-token /opt/training-hub/.secrets/logseq-encryption.json /opt/training-hub/.secrets/gemini-api-key /opt/training-hub/.secrets/sync-policy.env /opt/training-hub/.secrets/logseq-graphs.json"], stdin=stream, check=True)
run([*ssh, "cd /opt/training-hub && python3 scripts/setup.py && docker compose pull --policy missing && python3 scripts/pin-images.py && docker compose up -d --build --remove-orphans"])
run([*ssh, "cd /opt/training-hub && docker compose up -d --force-recreate --no-deps inbox"])
if state.get("static_ip_name"):
    run([*ssh, "cd /opt/training-hub && docker compose up -d --force-recreate --no-deps caddy"])
for _ in range(45):
    check = subprocess.run([*ssh, "cd /opt/training-hub && python3 scripts/smoke.py"], capture_output=True, text=True)
    if check.returncode == 0:
        print(check.stdout, end="", flush=True)
        break
    time.sleep(2)
else:
    raise SystemExit("Services did not pass remote health checks. Inspect docker compose logs, then rerun to resume.")
aws("put-instance-public-ports", "--instance-name", args.name, "--port-infos", json.dumps(ports))
run([*ssh, "cd /opt/training-hub && docker compose exec -T inbox python configure_bot.py"])
if "IMAGE_SEARCH_ENABLED=true" in (ROOT / ".env").read_text().splitlines():
    run([*ssh, "cd /opt/training-hub && if [ -s .secrets/telegram-index/telegram-index.session ]; then docker compose --profile image-search up -d --build --no-deps image-delivery; fi"])
run([*ssh, "cd /opt/training-hub && if [ ! -f data/n8n/.training-workflow-imported ]; then docker compose exec -T n8n n8n import:workflow --input=/workflows/telegram-inbox.json && touch data/n8n/.training-workflow-imported; fi"])
# Retain the exact deployed versions for subsequent deploys and migration.
deployed_env = run([*ssh, "cat /opt/training-hub/.env"], capture_output=True).stdout
(ROOT / ".env").write_text(deployed_env)
state_file.write_text(json.dumps(state, indent=2))
print("Deployment complete. Start the tunnel with: python3 scripts/tunnel.py", flush=True)
