# Training Hub

A private, portable first draft for two people: Telegram → n8n → durable inbox → shared Logseq DB. Instagram/YouTube descriptions are fetched when available. Optional Gemini analysis is **off by default**; saving links/captions requires no AI key or AI calls.

## Files and checks

- `src/inbox.py`: Telegram polling, allowlist, attachment downloads, SQLite queue, JSON exports, Logseq delivery.
- `src/analysis.py`: optional Gemini requests, duration gates, cache, and daily quota.
- `workflows/telegram-inbox.json`: importable polling workflow; AI policy lives in the inbox.
- `compose.yaml`: inbox, n8n, Logseq web, and Logseq sync worker.
- `scripts/`: configuration, Lightsail deployment, and SSH tunnel.
- `tests/`: offline Python standard-library tests; caption extraction is mocked.
- `requirements.txt`: hash-pinned yt-dlp, installed by the inbox Docker image.

```sh
python3 -m unittest discover -s tests -v
node --test tests/*.cjs
python3 scripts/setup.py
docker compose config --quiet
docker compose up -d --build
python3 scripts/smoke.py
```

Keep `AGENTS.md` unchanged; this README documents the new implementation and commands.

## Configuration

Setup creates `.env`, `.secrets/logseq-token`, and the `data/` directories without replacing existing secrets. All are ignored by Git. Edit `.env` locally:

- `TELEGRAM_BOT_TOKEN`: obtain from Telegram's official `@BotFather` using `/newbot`.
- `TELEGRAM_ALLOWED_USER_IDS`: your two numeric user IDs, comma-separated. An empty list accepts nobody. Only private bot chats are accepted.
- `BRIDGE_TOKEN`, `MEDIA_PASSWORD`, `N8N_ENCRYPTION_KEY`: generated automatically; retain these during migration.
- `LOGSEQ_GRAPH_ID`: set after creating the shared graph.
- `LOGSEQ_API_URL`: internal sync-worker URL; leave the default with Compose.
- `MEDIA_BASE_URL`: browser-facing attachment URL, default `http://localhost:8001` through the tunnel.
- `AI_ENABLED`: defaults to `false`; an API key alone does not turn AI on.
- `GEMINI_TEXT_MODEL`, `GEMINI_VIDEO_MODEL`: both default to `gemini-3.5-flash-lite`. The video model can be changed deliberately after checking availability and pricing.
- Image variables: deployment pins downloaded images by digest; upgrade deliberately and back up first.

For local runs, pin after the initial pull with `python3 scripts/pin-images.py`.

The bot token belongs in `.env`, never in workflow JSON, chat, or a command pasted into an issue. To discover your numeric ID without another bot, message your new bot and inspect the official `getUpdates` response locally; do this before activating polling. The workflow uses polling, so remove any existing webhook for this bot before using it.

## Lightsail deployment

The deployment script uses AWS CLI credentials on your computer, not on the server. It creates only a dedicated `training-hub-dev` Ubuntu 24.04 instance and SSH key pair under profile `ctd`, region `us-east-1`. It refuses to reuse an untracked instance. Default bundle: **2 GB RAM / 60 GB disk, $12/month before tax**, plus a 2 GB swap file. No database service, load balancer, domain, or AI subscription is provisioned.

```sh
python3 scripts/deploy-lightsail.py --profile ctd
python3 scripts/tunnel.py
```

Only SSH is open, restricted to your current public IPv4. Application ports bind to loopback. Keep the tunnel running and open:

- n8n: <http://localhost:5678> — immediately create your owner account.
- Logseq: <http://localhost:3001> — Chromium recommended.
- Logseq sync/API docs: <http://localhost:8787/api-docs>.
- Inbox health: <http://localhost:8001/health>.

For your brother's tunnel, add his public SSH key to the server's authorized keys and his public IP to the SSH firewall rule. He should use his own private key; do not share the deployment key. Both tunnels must forward the same local ports. Stop any local copy of this stack before tunnelling those ports to the VPS.

Localhost HTTP is intentional: remote transport is encrypted by SSH. Do not expose these ports publicly. A changed operator IP requires updating the Lightsail SSH firewall rule. A stopped instance still incurs storage/bundle charges; delete the dedicated instance when finished, after backing up its data. The instance IP can change after stop/start; refresh `.secrets/lightsail.json` or rerun deployment.

## Activate Telegram intake

Set the bot token and allowlist, deploy, then import the workflow in n8n using **Import from file** → `workflows/telegram-inbox.json`. Publish/activate it. Each 30-second run fetches Telegram updates, queues authorized submissions durably, saves attachments, and attempts Logseq delivery. With no bot configured it does nothing.

Send a text note, YouTube/Instagram URL, or video/file to the bot. You should receive a reference number and a clear saved/pending status. Inspect `data/inbox/exports/<reference>.json` on the server. Two deliveries of the same Telegram update create one submission; sending the same link as a new message intentionally creates another. Edited messages are not ingested in this draft.

For new Instagram posts/reels and YouTube videos/Shorts, the inbox attempts to copy the written description into a **Post description** block and uses the video title in the page name when available. It preserves line breaks, records the source URL, and encrypts the description with the rest of the Logseq note. Results are saved before publishing so retries do not fetch again or change already prepared blocks. Fetching has a shared 20-second budget per submission; an empty/blocked/timed-out description is marked accordingly while the original link is still published. Existing completed notes are not automatically reprocessed.

## Optional Gemini analysis

Preparation makes no Gemini calls. To activate later:

1. Save a Gemini API key in `.secrets/gemini-api-key`, with permissions `600`; never put it in workflow JSON or chat. Setup creates an empty file and preserves existing contents.
2. Keep the Google project on its Free Tier if you want free-tier quotas. The application cannot determine the key's billing tier and does not switch to another model when quota runs out. Free-tier data handling follows Google's terms.
3. Set `AI_ENABLED=true` in `.env`, deploy, and reimport/publish the updated `workflows/telegram-inbox.json` (the capture timeout is 180 seconds). New submissions are analyzed; completed older submissions are not automatically reopened.

Original links/captions are published and acknowledged first. Subsequent polling passes analyze the supplied text with Flash-Lite. If it contains an exercise breakdown, video analysis is skipped. Otherwise, Flash-Lite analyzes one video per message. The live setup uses Flash-Lite for both stages; Flash 3.8 returned provider availability errors during validation. Results are appended to the same encrypted page, always marked **needs review**. Missing sets, reps, weight, and rest stay null and are displayed as **not specified**. Source text is preserved separately; text-only analysis never claims to be an audio transcript.

| Video duration | Video analysis policy |
| --- | --- |
| Up to 120 seconds | Automatic when the caption is insufficient |
| Over 120 through 600 seconds | Bot asks; an allowlisted user must reply `/analyze <reference>` privately |
| Over 600 seconds | No analysis; send a shorter clip |
| Unknown duration | Accepted on user confirmation that submitted videos are short; Gemini input is clipped to the first 120 seconds and marked duration unverified |

Known duration is checked before AI-specific video downloads or Gemini video requests. When metadata has no duration, both users are responsible for checking that the video is under two minutes; the API additionally receives an explicit 120-second end offset. Known longer videos still require approval or a shorter clip. Original Telegram attachments still use the existing archive/download behavior. Public YouTube videos are submitted by canonical URL; Instagram videos are temporarily downloaded as MP4; Telegram MP4/WebM files reuse the saved attachment. Video downloads/uploads are capped at 20 MB, with a kernel file-size limit and temporary-file cleanup. Carousels/multiple videos require separate messages. Timestamp clipping is not implemented.

The shared budget is **10 video attempts per UTC day**, including approved videos and failed attempts. SQLite caches completed results by canonical URL or Telegram file identity, analyzed interval, model, and prompt version; reuse costs no new request. Caption requests are cached by text. Changing the model invalidates its cache. Video sampling is fixed at 1 FPS and low resolution; each request allows at most 2,048 output/thinking tokens and caption input is capped at 16,000 characters.

HTTP 429 pauses all Gemini requests for 15 minutes. Explicit provider 5xx errors retry after 30 then 60 seconds. Each cached request allows at most three attempts. Daily limits resume after UTC midnight. These waits do not block saving the original note. Ambiguous network timeouts, interrupted requests, incomplete/invalid model output, and other non-retryable API errors stop for manual review rather than automatically repeating a potentially billed request. Such cache entries need operator review before any explicit retry. Gemini calls time out after 45 seconds; AI work is serial and can delay subsequent polling while a request is running.

## Access through a static IP without a domain

```sh
python3 scripts/deploy-lightsail.py --profile ctd --public-ip
python3 scripts/smoke-public.py
```

This attaches a dedicated Lightsail static IP and starts the Caddy Compose profile. The IP costs nothing extra while attached; release it after deleting the instance to avoid detached-IP charges. Deployment records the address in `.secrets/lightsail.json` and `.env`.

- Browser: `https://<PUBLIC_IP>`.
- Logseq **Sync Server URL**: `https://<PUBLIC_IP>:8443`.
- n8n and inbox administration stay behind the SSH tunnel.

The browser prompts for username **`training`** and the **`MEDIA_PASSWORD`** from your local `.env`. The same password unlocks attachments. Public deployment generates a bcrypt hash using Caddy's stdin input; the proxy receives only the hash. Change `MEDIA_PASSWORD` and redeploy to change this password. Basic auth protects the web page; the sync port keeps its existing bearer-token authentication so clients can sync normally.

The browser automatically initializes its sync URL to the current HTTPS host on port 8443, or localhost port 8787 when opened through the HTTP SSH tunnel. Existing explicit sync settings are preserved. The bootstrap script runs before Logseq starts and is served without browser caching. This applies to our hosted browser app; desktop apps still need their sync URL configured once.

Caddy requests a publicly trusted Let's Encrypt IP certificate with the `shortlived` profile and renews it automatically. Preserve `data/caddy/`. Port 80 is globally accessible for HTTP certificate validation; 443 and 8443 accept only your current public IPv4. Each deployment resets these firewall rules to the deploying network. Add your brother's network when enabling his access. Restart the tunnel after first attaching the static IP because the SSH destination changes.

The public URL is a separate browser origin: notes stored locally at `localhost:3001` do not automatically appear there. Keep that tab and export any unsynced notes before moving them. Configure sync on both clients and confirm a test note reaches the server. With public access, attachments use the HTTPS URL and still require the `training` username and `MEDIA_PASSWORD`.

## Connect Logseq DB

This draft uses the community-packaged **sync worker**, not the older Node sync adapter. Their storage formats differ. Logseq collaboration and the worker deployment remain experimental.

1. In Logseq's advanced settings, set **Sync Server URL** to `http://localhost:8787` while using the tunnel. Both people need the same reachable URL through their respective tunnels.
2. Sign in and create a synced DB graph. Invite your brother through the graph's sharing controls. Test both accounts editing the same graph before relying on it.
3. Set `LOGSEQ_GRAPH_ID` in `.env` to the uploaded encrypted graph UUID. In Settings → Personal access tokens, create a **Write** token scoped to this graph and save it in `.secrets/logseq-token`. Never paste it into chat.
4. Run `python3 scripts/unlock-graph-key.py` locally. It reads the owner's encrypted key packages through the deployment SSH connection and prompts for the Logseq encryption password without echoing it. It saves only this graph's AES key in `.secrets/logseq-encryption.json`, bound to the graph UUID. The password and decrypted account private key are never saved. Requires local Node.js and the existing Lightsail SSH configuration.
5. Redeploy. The bot encrypts page titles, names, and blocks using the native graph API. Missing credentials leave submissions queued; the bot never falls back to plaintext graph writes. Successful delivery creates `<video title> — AI pending · <reference>` pages (or **AI not processed** when AI is off), containing the original links/text. Completed AI results rename the same page to **AI processed**. Failed/skipped analysis is labeled **AI not processed**; the reference keeps repeated submissions distinct. Ciphertext and deterministic block UUIDs are persisted before writes so retries reuse them.

Tokens expire; replace the token and redeploy when required. The worker still uses external Cognito login. Giving the bot a graph key means the bot and its VPS can read that graph; keep the VPS and `.secrets/` trusted. Original Telegram submissions are also retained unencrypted in the inbox. Graph-key rotation requires updating the key and reviewing pending encrypted payloads before resuming delivery.

### Self-hosted token authorization

The pinned upstream worker checks hosted RTC subscription status when creating tokens, which returns 403 for this self-hosted setup. `deploy/logseq-selfhost-tokens.patch` skips only that subscription check when `LOGSEQ_SELF_HOSTED=true`; verified login, graph membership, token scopes, and expiry checks remain enabled.

```sh
python3 scripts/build-sync.py
# Set LOGSEQ_SYNC_IMAGE=training-hub-logseq-sync:69da7e-selfhost2 in .env
python3 scripts/deploy-lightsail.py --profile ctd
```

The build also applies `deploy/logseq-proxy-origin.patch`: snapshot and temporary asset URLs preserve HTTPS terminated by the local reverse proxy. Direct HTTP tunnel URLs stay HTTP. The worker port must remain private so forwarded protocol headers come from the trusted proxy. A focused upstream ClojureScript regression check runs during the build.

The build pins both upstream source revisions and packages the self-hosted flag. Toolchains run in a temporary Docker builder. Deployment loads this local image onto Lightsail when necessary. Registry images remain pinned by digest. Back up the graph before worker upgrades; `python3 scripts/smoke-public.py` checks that token creation rejects missing and unsigned login tokens.

## Limits and troubleshooting

- Caption fetching uses yt-dlp in metadata-only mode. Only canonical Instagram/YouTube post URLs are accepted; arbitrary submitted hosts are not fetched. Video downloading and Gemini calls occur only in the explicitly enabled AI path.
- Instagram and YouTube may block VPS requests or require login. Account cookies are not configured. A failed caption fetch is saved as `unavailable`; resend the link to try again. Update the pinned yt-dlp version deliberately when platform changes break extraction.
- Telegram's hosted Bot API limits file downloads to 20 MB. Larger attachments are flagged and retained as metadata, not silently reported as downloaded.
- Attachments are private downloads linked from notes, not inline video previews. Through the tunnel, use username `training` and the generated `MEDIA_PASSWORD` when prompted.
- Logseq failures retain the submission for retry. Telegram acknowledgement delivery is at least once; a timeout after Telegram accepts a reply can cause a duplicate acknowledgement.
- A small VPS may use swap. Video transcoding and local AI are excluded. Keep disk usage under review as media accumulate.
- `docker compose ps` and `docker compose logs --tail=60 inbox n8n logseq-sync` show service status. Logs omit API tokens; n8n failed executions can contain private notes and are pruned after seven days.

## Backup and move to Hetzner

All persistent application data is in `data/`; credentials are in `.env` and `.secrets/` (token and graph encryption key). The sync worker stores its local database, objects, and attachments under `data/logseq`. Logseq's browser also holds local graph state: confirm sync is complete and export a graph backup from the client before moving.

1. Stop Compose on the old host: `docker compose stop`.
2. Make a restricted archive of `data/`, `.env`, `.secrets/logseq-token`, `.secrets/logseq-encryption.json`, and `.secrets/gemini-api-key`. Copy it off the VPS; it contains secrets and unencrypted inbox notes/AI results. Preserve `data/inbox/inbox.sqlite`, including AI caches and quota counters. Keep a previous backup as well.
3. Copy this repository's deployment files and the archive to the new Ubuntu host. For the patched worker, transfer `docker save training-hub-logseq-sync:69da7e-selfhost2` output and restore with `docker load`, or rebuild using `scripts/build-sync.py`. Restore data ownership to UID/GID 1000 and secret permissions to 600, install Docker/Compose, then run `docker compose up -d --build` with the same images.
4. Change your SSH tunnel's destination to the Hetzner IP. Keeping the same localhost ports preserves the sync URL and attachment links. Keep the old host stopped so only one Telegram poller runs.
5. Verify notes, attachments, and a fresh Telegram submission before deleting AWS resources. Never use `docker compose down -v` or remove `data/` as a migration step.

With public-IP access, the AWS IP cannot move to Hetzner. Update `PUBLIC_IP`, `MEDIA_BASE_URL`, client sync URLs, and existing attachment links for the new address; issue a new certificate and test graph synchronization before cutover. The localhost tunnel approach above avoids changing those client URLs.

Sources: [Logseq DB](https://github.com/logseq/docs/blob/master/db-version.md), [self-hosted worker](https://github.com/yshalsager/logseq-selfhost/tree/master/images/sync-worker), [n8n Community Edition](https://docs.n8n.io/hosting/community-edition-features/), [Lightsail pricing](https://aws.amazon.com/lightsail/pricing/).
