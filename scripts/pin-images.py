"""Pin pulled third-party images so migration uses the same versions."""
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
env = root / ".env"
lines = env.read_text().splitlines()
public = any(line.startswith("PUBLIC_IP=") and line.split("=", 1)[1] for line in lines)
for index, line in enumerate(lines):
    if line.startswith("CADDY_IMAGE=") and not public:
        continue
    if line.startswith(("N8N_IMAGE=", "LOGSEQ_SYNC_IMAGE=", "CADDY_IMAGE=")):
        name, image = line.split("=", 1)
        # This image is built from pinned source plus our checked-in patch.
        if name == "LOGSEQ_SYNC_IMAGE" and image in ("training-hub-logseq-sync:69da7e-selfhost1", "training-hub-logseq-sync:69da7e-selfhost2", "training-hub-logseq-sync:69da7e-selfhost3", "training-hub-logseq-sync:69da7e-selfhost4", "training-hub-logseq-sync:69da7e-selfhost5", "training-hub-logseq-sync:69da7e-selfhost6"):
            continue
        result = subprocess.run(["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}"],
                                check=True, capture_output=True, text=True)
        digests = json.loads(result.stdout)
        if not digests:
            raise SystemExit(f"No registry digest available for {name}; pull the image first.")
        lines[index] = name + "=" + digests[0]
env.write_text("\n".join(lines) + "\n")
print("Pinned third-party images by registry digest.")
