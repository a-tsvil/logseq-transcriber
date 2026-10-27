"""Build the pinned sync worker with self-hosted authorization and HTTPS download links."""
from pathlib import Path
import json
import re
import shlex
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
wrapper_ref = "7708e3dc9412f0308379a51fe8c6be8c82bd95c2"
logseq_ref = "69da7e2215b336cc5123dafb2a1686996e39596c"
image = "training-hub-logseq-sync:69da7e-selfhost2"

with tempfile.TemporaryDirectory(prefix="training-sync-build-") as temporary:
    context = Path(temporary)
    subprocess.run(["git", "clone", "--quiet", "https://github.com/yshalsager/logseq-selfhost.git", str(context)], check=True)
    subprocess.run(["git", "checkout", "--quiet", wrapper_ref], cwd=context, check=True)
    (context / "selfhost-tokens.patch").write_bytes((root / "deploy/logseq-selfhost-tokens.patch").read_bytes())
    (context / "proxy-origin.patch").write_bytes((root / "deploy/logseq-proxy-origin.patch").read_bytes())
    (context / "proxy_origin_test.cljs").write_bytes((root / "tests/upstream_proxy_origin_test.cljs").read_bytes())
    dockerfile = context / "images/sync-worker/Dockerfile"
    source = dockerfile.read_text()
    # This workspace has Docker's classic builder; caches do not affect the image.
    source = re.sub(r"--mount=type=cache,target=\S+ ", "", source)
    anchor = "COPY mise.toml /src/mise.toml"
    assert source.count(anchor) == 1
    source = source.replace(anchor, "COPY selfhost-tokens.patch proxy-origin.patch /tmp/\nRUN git apply --check /tmp/selfhost-tokens.patch /tmp/proxy-origin.patch && git apply /tmp/selfhost-tokens.patch /tmp/proxy-origin.patch\n\n" + anchor)
    test_target = ':db-sync-proxy-test {:target :node-script :main logseq.db-sync.proxy-origin-test/main :output-to "worker/dist/proxy-origin-test.js" :compiler-options {:elide-asserts false} :devtools {:enabled false}} '
    configure_test = ('const fs=require("node:fs"),p="shadow-cljs.edn",s=fs.readFileSync(p,"utf8"),a=":db-sync {:target :esm";'
                      'if(s.split(a).length!==2)throw Error("Unexpected upstream build config");'
                      'fs.writeFileSync(p,s.replace(a,' + json.dumps(test_target) + '+a));')
    release = "RUN mise exec -- pnpm run release"
    assert source.count(release) == 1
    source = source.replace(release,
                            "COPY proxy_origin_test.cljs /src/deps/db-sync/test/logseq/db_sync/proxy_origin_test.cljs\n"
                            "RUN mise exec -- node -e " + shlex.quote(configure_test) + "\n"
                            "RUN mise exec -- clojure -M:cljs compile db-sync-proxy-test && mise exec -- node worker/dist/proxy-origin-test.js\n"
                            "RUN mise exec -- pnpm run release")
    source = source.replace("COPY --chmod=755 --chown=node:node", "COPY --chown=node:node")
    source = source.replace("USER node:node", "RUN chmod 755 /usr/local/bin/start-sync-worker.sh\n\nUSER node:node")
    dockerfile.write_text(source)
    config = context / "images/sync-worker/wrangler.selfhost.toml"
    config.write_text(config.read_text().replace("[vars]\n", '[vars]\nLOGSEQ_SELF_HOSTED = "true"\n', 1))
    subprocess.run(["docker", "build", "--build-arg", "LOGSEQ_REF=" + logseq_ref,
                    "-f", str(dockerfile), "-t", image, str(context)], check=True)
print("Built " + image)
