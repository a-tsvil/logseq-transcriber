# Training Hub

A private, portable first draft for two people: Telegram → n8n → durable inbox → shared Logseq DB. Instagram/YouTube descriptions are fetched when available. Optional Gemini analysis is **off by default**; saving links/captions requires no AI key or AI calls.

## Files and checks

- `src/inbox.py`: Telegram polling, allowlist, attachment downloads, SQLite queue, JSON exports, Logseq delivery.
- `src/analysis.py`: optional Gemini requests, duration gates, cache, and daily quota.
- `src/image_search.py`: private image descriptions, OCR, message references, and semantic search.
- `src/import_images.py`: bounded, explicit account-based history imports; no image files are saved.
- `src/image_delivery.py`: retrieves images requested with search-result buttons and sends them privately to the owner.
- `src/activity.py`: best-effort working reactions and refreshed typing/upload indicators for slow bot operations.
- `workflows/telegram-inbox.json`: importable polling workflow; AI policy lives in the inbox.
- `compose.yaml`: inbox, n8n, Logseq sync worker, and optional HTTPS proxy.
- `scripts/`: configuration, Lightsail deployment, and SSH tunnel.
- `tests/`: offline Python standard-library tests; caption extraction is mocked.
- `requirements.txt`: hash-pinned yt-dlp, installed by the inbox Docker image.
- `requirements-import.txt`: hash-pinned Telethon and dependencies, installed only in the optional importer image.

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
- `TELEGRAM_ALLOWED_USER_IDS`: configured numeric Telegram user IDs, comma-separated. An empty list disables bot access, including users approved through `/start`. Only private bot chats are accepted.
- `TELEGRAM_ADMIN_USER_ID`: only this numeric Telegram ID may manage sync accounts, import account history, or search the personal image index.
- `TELEGRAM_LOGSEQ_USERS`: JSON mapping Telegram IDs to verified Logseq email addresses. The owner configures this mapping; bot users cannot claim another email.
- `BRIDGE_TOKEN`, `MEDIA_PASSWORD`, `N8N_ENCRYPTION_KEY`: generated automatically; retain these during migration.
- `LOGSEQ_GRAPH_ID`: set after creating the default shared graph.
- `LOGSEQ_GRAPH_NAME`: the name shown by `/select_graph` for that default graph.
- `LOGSEQ_API_URL`: internal sync-worker URL; leave the default with Compose.
- `MEDIA_BASE_URL`: attachment URL, default `http://localhost:8001` through the tunnel.
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

For an SSH-only deployment, only SSH is open, restricted to your current public IPv4. Application ports bind to loopback. Use a native Logseq DB client; keep the tunnel running for private administration:

- n8n: <http://localhost:5678> — immediately create your owner account.
- Logseq sync/API docs: <http://localhost:8787/api-docs>.
- Inbox health: <http://localhost:8001/health>.

For your brother's tunnel, add his public SSH key to the server's authorized keys and his public IP to the SSH firewall rule. He should use his own private key; do not share the deployment key. Both tunnels must forward the same local ports. Stop any local copy of this stack before tunnelling those ports to the VPS.

Localhost HTTP is intentional: remote transport is encrypted by SSH. Do not expose these ports publicly. A changed operator IP requires updating the Lightsail SSH firewall rule. A stopped instance still incurs storage/bundle charges; delete the dedicated instance when finished, after backing up its data. The instance IP can change after stop/start; refresh `.secrets/lightsail.json` or rerun deployment.

## Activate Telegram intake

Set the bot token and allowlist, deploy, then import the workflow in n8n using **Import from file** → `workflows/telegram-inbox.json`. Publish/activate it. Each five-second run fetches Telegram updates, queues authorized submissions durably, saves attachments, and attempts Logseq delivery. With no bot configured it does nothing. Compose uses the legacy n8n scheduler because durable scheduling in n8n 2.44.6 leaves scheduled executions stalled; revisit `N8N_SCHEDULER_ENABLED=false` after an n8n upgrade.

Send a text note, YouTube/Instagram URL, or video/file to the bot. You should receive a reference number and a clear saved/pending status. Inspect `data/inbox/exports/<reference>.json` on the server. Two deliveries of the same Telegram update create one submission; sending the same link as a new message intentionally creates another. Edited messages are not ingested in this draft.

For new Instagram posts/reels and YouTube videos/Shorts, the inbox attempts to copy the written description into a **Post description** block and uses the video title in the page name when available. It preserves line breaks, records the source URL, and encrypts the description with the rest of the Logseq note. Results are saved before publishing so retries do not fetch again or change already prepared blocks. Fetching has a shared 20-second budget per submission; an empty/blocked/timed-out description is marked accordingly while the original link is still published. Existing completed notes are not automatically reprocessed.

## Telegram command menu

Open the bot's **Menu** button or type `/help` to see available commands and examples. `/start` also shows help for existing users. Allowed private chats get `/help`, `/select_graph`, `/checklist`, `/cook`, and `/analyze`; only the configured owner gets `/sync`, `/users`, and experimental `/search`. Help/start messages are handled directly and never become notes or trigger AI.

Deployment configures menus with Telegram after service health checks. To refresh them after changing allowed Telegram IDs or the owner, run `docker compose exec -T inbox python configure_bot.py`. Each person must already have started a private chat with the bot. Menus persist across bot restarts. Menu configuration includes approved posting users from the inbox database and changes command settings without sending messages or consuming pending updates; handlers still enforce all permissions.

### Posting users without a Logseq account

1. The new person opens the bot and sends `/start` privately. Unknown users receive
   a pending-access response; they cannot submit notes yet. Bot accounts and group
   `/start` messages cannot register. Repeated requests do not notify the owner again.
2. The owner receives a request with graph-specific approval buttons. Choose
   **Approve → Shopping Lists**, **Approve → Training-mit-Bruder**, or another
   configured graph. Only that graph is granted; approve additional graphs separately.
3. The person receives a confirmation and can send notes, video links, or
   `/checklist` items to the selected graph. `/select_graph` shows only approved destinations
   and lets her select one with a button. A native Logseq account is unnecessary
   for posting through the bot; this does not grant native Logseq login or graph reading.
4. The owner can use `/users` to review the latest ten registered users/requests,
   grant or remove a graph, disable a user, or restore a denied user. Unknown users
   do not become administrators, and image search remains restricted to the owner.

The registry and graph grants persist in `data/inbox/inbox.sqlite`. Posting uses
the bot's configured graph credentials and keeps the submitter's Telegram ID;
it never maps them to somebody else's Logseq email. The owner's verified graph
access and each explicit posting grant are checked before delivery. Outages,
revoked grants, removed graph configuration, and disabled users do not redirect
queued notes to another destination. Existing account-linked users retain their
verified Logseq discovery behavior.

## Graph selection and journal checklists

Use these commands in a private chat with the bot:

```text
/select_graph
/checklist
Buy oats
Prepare tomorrow's workout
Pack gym clothes
```

`/select_graph` shows the current graph and available destinations, with a button for each ready graph. Tap a button to select it; the same message updates to mark the current choice. Telegram command menus require underscores, so the menu uses `/select_graph`; manually typed `/select-graph` also works.

Discovery includes remote graphs owned by or shared with your linked Logseq account; posting users see only owner-approved destinations. Graphs without bot credentials are labeled **bot setup required** and cannot be selected. Private graphs belonging to another account are excluded. Duplicate graph names include UUIDs so their buttons are unambiguous. Selections persist separately for each Telegram user. Every queued submission and active checklist draft keeps its original destination even if you change your selection later. Button clicks recheck current access.

To override the destination for one checklist, put the graph name on the command's first line:

```text
/checklist "Training-mit-Bruder"
Buy oats
Prepare tomorrow's workout
```

For clients that send the message as soon as you press Enter, send each of these as a separate message:

```text
/checklist Weekly shopping
Milk
Bread
/done
```

`/checklist` alone starts an unnamed list; `/checklist Weekly shopping` uses **Weekly shopping** as its title. Subsequent text messages add items, splitting every nonblank line into a separate checkbox. The bot confirms the running item count. `/done` queues the complete list for Logseq and returns to ordinary notes; `/cancel` discards the draft. Nothing is published until `/done`. Empty lists, attachments, and additions beyond 100 items are rejected without losing the draft. Commands such as `/help` and `/select_graph` remain available during drafting.

Choose the graph with `/select_graph` before starting. Drafts are private to each user, survive bot restarts, and retain the graph and journal date from when they were started. Changing `/select_graph` during a draft affects future lists, not the current one. Access is checked again when saving and publishing. Starting another checklist while one is active asks you to finish or cancel first.

The multiline form above still saves immediately: its optional first-line argument is a **graph name**, while the single-line form's optional argument is the **list title**.

Each nonblank line becomes one clickable square checkbox, grouped under a collapsible **Checklist · HH:MM** block in that day's journal. Blank lines are skipped; 1–100 items are accepted. Graph names containing spaces need quotes. Send text without an attachment. No AI requests or caption fetching run for checklists.

The journal date comes from the Telegram message's timestamp in **Europe/Berlin** (`TZ` in the inbox service). The date, graph, ciphertext, and block IDs are saved before delivery. Retries use the same journal, create no duplicate items, and preserve checked items. New items use an actual boolean `Checked` property positioned before their text. They start unchecked, toggle true/false, and have no Task tag or task status. Existing published lists are unchanged. Boolean checklists require `selfhost6` or later. Account-scoped graph discovery requires `selfhost5` or later. Older workers leave unsupported submissions queued.

### Compact checklist appearance

To display a checklist as one parent bullet with checkbox rows underneath, open the
graph in Logseq and append [assets/logseq/checklists.css](assets/logseq/checklists.css)
to **Settings → General → Edit custom.css**, then save. This hides only child bullet
handles under `Checklist · HH:MM` headings; checkbox controls, text, and the parent
collapse control remain available. The items remain separate blocks internally.
Remove the CSS rule to restore the child bullet/drag handles. The selector follows
Logseq 2.0.2's markup; no bot or server deployment is required.

### Link Telegram and Logseq identities

Set `TELEGRAM_LOGSEQ_USERS` in the private `.env`, for example `{"101":"owner@example.com","202":"brother@example.com"}`. Each email must belong to the corresponding verified Logseq account. Recreate the inbox after changes. With this mapping enabled, an unmapped Telegram user cannot discover or submit to graphs. If the mapping is empty, legacy configured-graph listing remains available.

Discovery uses the internal `/training/graphs?email=...` endpoint with the bot's management credential. The worker filters on verified identity, the sync account allowlist, and graph ownership/membership. The public proxy blocks this endpoint. A discovery outage does not fall back to a stale or broader graph list.

### Enable additional shared graphs

The default graph uses the existing `.secrets/logseq-token` and `.secrets/logseq-encryption.json`. Setup creates `.secrets/logseq-graphs.json` as an empty list. To enable another graph, add a private entry:

```json
[
  {
    "id": "22222222-2222-4222-8222-222222222222",
    "name": "Another shared graph",
    "token": "logseq_pat_replace-with-a-write-token-scoped-to-this-graph",
    "key": "replace-with-the-base64-encoded-32-byte-graph-key"
  }
]
```

For an unencrypted graph, set `"key": null` explicitly; no encryption password or key is needed. The scoped token is still required. A missing key field is rejected, and encrypted graphs must retain their 32-byte key. Server administrators can provision a graph-scoped token locally after verifying the graph owner and encryption status; the bot does not gain unrestricted sync-server access.

Keep this file at mode `600`; never send its credentials through Telegram or chat. The bot uses configured credentials to write, and checks that the submitting person still has access through their linked Logseq account. Graph access is rechecked before delivery; removed access leaves queued submissions pending. Configured graph names and IDs must be unique; unknown names are rejected instead of falling back to another graph. Discovery never retrieves tokens or encryption keys and does not automatically enable writes.

Create the additional graph's scoped token in a native client. For an encrypted graph, to unlock its key without replacing the default, use `python3 scripts/unlock-graph-key.py --graph-id <UUID> --output .secrets/another-graph-key.json`; the helper asks for the graph owner's encryption password. Copy the resulting `key` into the private registry entry. Recreate the inbox after updating this file: `docker compose up -d --force-recreate --no-deps inbox`. The deployment script does this automatically. Removing a graph from configuration leaves its queued submissions pending; it never redirects them.

## Optional Gemini analysis

Preparation makes no Gemini calls. To activate later:

1. Save a Gemini API key in `.secrets/gemini-api-key`, with permissions `600`; never put it in workflow JSON or chat. Setup creates an empty file and preserves existing contents.
2. Keep the Google project on its Free Tier if you want free-tier quotas. The application cannot determine the key's billing tier and does not switch to another model when quota runs out. Free-tier data handling follows Google's terms.
3. Set `AI_ENABLED=true` in `.env`, deploy, and reimport/publish the updated `workflows/telegram-inbox.json` (the capture timeout is 180 seconds). New submissions are analyzed; completed older submissions are not automatically reopened.

Original links/captions are published and acknowledged first. Subsequent polling passes analyze the supplied text with Flash-Lite. If it contains an exercise breakdown, video analysis is skipped. Otherwise, Flash-Lite analyzes one video per message. The live setup uses Flash-Lite for both stages; Flash 3.8 returned provider availability errors during validation. Results are appended to the same encrypted page, always marked **needs review**. Missing sets, reps, weight, and rest stay null and are displayed as **not specified**. Source text is preserved separately; text-only analysis never claims to be an audio transcript.

Every newly completed video analysis also adds a native link to its full video page in the current day’s journal of the same graph. The date uses `TZ` (Europe/Berlin by default) when the link is first prepared after analysis finishes, rather than the video’s posting date or submission date. The journal link is encrypted on encrypted graphs, and its date, ciphertext, and stable block UUID are persisted before capture so retries across midnight cannot duplicate it or move it to another day. Journal delivery failures keep the submission queued without repeating completed AI analysis. Completed older submissions are not backfilled; plain notes and unsuccessful analyses do not create journal links.

For cooking videos, select `/cook` from the menu (or send `/cook` alone), then send a YouTube/Instagram video link or video attachment in your next message. `/cancel` exits without saving a note. The request is private to your account, survives restarts, and ends after one accepted submission; invalid input leaves it waiting. Help and graph selection remain available while waiting. Finish or cancel an active checklist before starting a cooking request. You can also send `/cook <YouTube or Instagram URL>` directly or attach a video with `/cook` in its caption. You can also supply recipe text after `/cook`. This selects a separate cooking prompt; ordinary submissions still use the exercise prompt. Cooking analysis extracts ingredients (including seasonings, oils, and toppings), preparation details, and chronological steps with available heat, timing, texture, and doneness cues. Explicit quantities are preserved. Missing quantities are estimated from visible amounts using teaspoons, tablespoons, cups, pinches, handfuls, or item counts, with estimates clearly labeled; quantities that cannot reasonably be estimated remain **not specified**. Uncertain ingredients, missing steps, and caption/video contradictions should be flagged. Audio transcripts contain only clearly audible speech; the reconstructed recipe is shown separately as ingredients and method. A complete written recipe skips video analysis; otherwise the video is analyzed with the caption as context. AI must be enabled. The same duration approvals and shared daily budget apply, and cooking results are cached separately from exercise results and by caption context.

| Video duration | Video analysis policy |
| --- | --- |
| Up to 120 seconds | Automatic when the caption is insufficient |
| Over 120 through 600 seconds | Bot asks; an allowlisted user must reply `/analyze <reference>` privately |
| Over 600 seconds | No analysis; send a shorter clip |
| Unknown duration | Accepted on user confirmation that submitted videos are short; Gemini input is clipped to the first 120 seconds and marked duration unverified |

Known duration is checked before AI-specific video downloads or Gemini video requests. When metadata has no duration, both users are responsible for checking that the video is under two minutes; the API additionally receives an explicit 120-second end offset. Known longer videos still require approval or a shorter clip. Original Telegram attachments still use the existing archive/download behavior. Public YouTube videos are submitted by canonical URL; Instagram videos are temporarily downloaded as MP4; Telegram MP4/WebM files reuse the saved attachment. Video downloads/uploads are capped at 20 MB, with a kernel file-size limit and temporary-file cleanup. Carousels/multiple videos require separate messages. Timestamp clipping is not implemented.

The shared budget is **10 video attempts per UTC day**, including approved videos and failed attempts. SQLite caches completed results by canonical URL or Telegram file identity, analyzed interval, model, and prompt version; reuse costs no new request. Caption requests are cached by text. Changing the model invalidates its cache. Video sampling is fixed at 1 FPS and low resolution; each request allows at most 2,048 output/thinking tokens (8,192 for detailed cooking recipes) and caption input is capped at 16,000 characters.

The inbox SQLite database is the durable queue; n8n polls it and only due submissions are returned. HTTP 429 and explicit provider 500/502/503/504 errors schedule up to five total attempts with exponential backoff: a 15-minute base for 429, or a 30-second base for provider failures, doubling up to a one-hour base plus 0–25% jitter. A valid `Retry-After` header (seconds or HTTP date) is honored when longer. Temporary errors pause new Gemini requests globally to avoid hammering the provider. Attempt counts, retry deadlines, and terminal HTTP failure categories survive restarts; exhausted requests stop for review. HTTP 400/401/403 and other permanent errors are not retried. Cooking array limits are validated locally rather than expanded into provider schema constraints. Daily limits resume after UTC midnight. These waits do not block saving the original note; no sleeping worker or additional n8n queue is needed. Ambiguous network timeouts, interrupted requests, incomplete/invalid model output, and other non-retryable API errors stop for manual review rather than automatically repeating a potentially billed request. Such cache entries need operator review before any explicit retry. Gemini calls time out after 45 seconds; AI work is serial and can delay subsequent polling while a request is running.

## Personal image search

The account importer reads one explicitly selected cloud chat at a time. It processes
JPEG, PNG, and WebP images in memory (maximum 10 MiB per image), asks Gemini for a
visual description and visible text, and embeds that description for semantic search.
Only descriptions/OCR, vectors, content hashes, chat names, and message references
are persisted in `data/inbox/image-index.sqlite`. Images are sent to Gemini for
processing, so provider data handling still applies. This feature does not change
the existing attachment archival behavior for notes sent directly to the inbox.
Telegram stickers are excluded using message metadata before downloading or making
AI calls. Ordinary WebP image documents remain supported. Previously indexed
stickers must be flagged as excluded using their original message metadata; their
old result buttons then stop working as well.

Telegram's [content terms](https://telegram.org/tos/content-licensing#large-language-models-and-ai)
restrict AI indexing and describe exceptions involving explicit, informed, continued
consent from all relevant users for the specific content and chat. Account membership
alone is not permission. The importer requires an operator's `--consent-confirmed`
declaration for each selected chat; it cannot obtain or verify others' consent.
Protected chats/messages and messages marked for automatic expiry are skipped or
rejected. Secret chats are unsupported.

1. Run `python3 scripts/setup.py` to create the private account-session directory.
   On the deployed server, run it from `/opt/training-hub`. Add `TELEGRAM_API_ID`
   and `TELEGRAM_API_HASH` from [my.telegram.org](https://my.telegram.org) to the
   private `.env`. Set `TELEGRAM_ADMIN_USER_ID` to your account's numeric ID and
   include it in `TELEGRAM_ALLOWED_USER_IDS`. Keep credentials out of chat.
2. Store the Gemini key in `.secrets/gemini-api-key`. Images use
   `GEMINI_TEXT_MODEL`; embeddings use `gemini-embedding-001` at 768 dimensions.
   Importing is explicitly invoked and independent of `AI_ENABLED` (which controls
   the existing exercise analyzer). Do not run a paid import without reviewing
   your provider's pricing and data handling.
3. Build the optional importer and list your dialogs to obtain the numeric chat ID:

   ```sh
   docker compose --profile image-import build image-import
   docker compose --profile image-import run --rm image-import --list-chats
   ```

   Log in interactively as the configured owner when prompted. Listing reads chat
   names/IDs, not images, and makes no AI calls. The session is saved under
   `.secrets/telegram-index/`, mounted only in the importer. The bot server does
   not receive that session. Protect it like a password; revoke the session through
   Telegram's Devices settings when it is no longer needed.

   If Telegram does not deliver a new login code, stop the pending attempt with
   Ctrl+C and authorize using a QR code instead:

   ```sh
   docker compose --profile image-import run --rm image-import --list-chats --qr-login
   ```

   In your phone's Telegram app, open **Settings → Devices → Link Desktop Device**
   and scan the QR shown in the terminal. Keep the terminal open; if it expires,
   rerun the command for a new QR. Enter any two-step verification password only
   in the terminal. QR login receives updates only for the login procedure; regular
   imports continue to run without an account update listener.
4. Import a small batch from one chat for which the required consent is in place:

   ```sh
   docker compose --profile image-import run --rm image-import \
     --chat -1001234567890 --consent-confirmed --max-images 20
   ```

   Replace the example ID with one from the listing. This runs on the same host
   and data volume as the bot. It sends no Telegram messages and does not add the
   bot to the source chat. It scans newest first, skips previously processed
   references, and stops after at most 20 new image references by default
   (maximum configurable batch: 1,000). Each new unique image takes one vision
   request and one embedding request, separate from the exercise video's budget.
   Exact image bytes reuse completed analysis across messages. Provider/network
   failures stop the batch, leaving completed entries intact. Failed/interrupted
   requests are not automatically repeated; `--retry-failed` explicitly permits
   repeating them, potentially incurring another charge. Model changes create a
   separate index profile and require importing again.
   Imports wait six seconds between new references to reduce per-minute quota
   spikes. Completed vision descriptions are saved before embedding; an explicitly
   retried embedding failure reuses the saved description without another image
   analysis call.
5. Set `IMAGE_SEARCH_ENABLED=true` in `.env`, rebuild the server and refresh menus:

   ```sh
   docker compose up -d --build --no-deps inbox
   docker compose exec -T inbox python configure_bot.py
   ```

   After account login, start the optional image retrieval worker:

   ```sh
   docker compose --profile image-search up -d --build --no-deps image-delivery
   ```

   The worker mounts the account session alongside the importer; the normal inbox
   server still has no account session. Keep the worker running for result buttons
   to work. When redeploying manually, rebuild/recreate it alongside the inbox.
   The worker loads credentials into memory and closes the SQLite session file
   before connecting, so background delivery does not block an account import.
   Reimport the updated n8n workflow to poll every five seconds for responsive buttons.

   In your private bot conversation, send:

   ```text
   /search A meme with a cat dressed like a factory worker with text
   ```

Only the owner can search; other allowlisted contributors cannot access the index.
Search retrieves up to twenty distinct candidate images by semantic similarity,
then uses a strict AI check of their stored descriptions against every explicitly
requested subject and constraint. Unsupported or uncertain candidates are rejected;
there is no fallback that fills the list with unrelated pictures. If none qualify,
the bot replies **No matching images found in your indexed library** with no buttons.
Matching still depends on the accuracy of the stored descriptions and can miss
valid pictures whose important details were omitted.

Search returns a compact match count with up to five result rows. Each row has a
**Show image** button followed by **Description**. Descriptions and internal message
references are hidden from the default view. Description opens a popup when the
text fits Telegram's 200-character alert limit; longer descriptions are sent as
chat messages only when explicitly requested. Text comes from the existing index,
without another AI call, and remains restricted to the owner. **Show image** retrieves the original
through your account and delivers it to your private bot chat. Retrieval rechecks
access, protection, expiry, and whether the original image still exists. Images
are downloaded/uploaded in memory and discarded afterward. The delivered copy
remains in your bot conversation on Telegram. Image documents are sent as documents.
Showing a result makes no AI calls. Identical search prompts reuse a cached query
embedding and a cached strict match decision. A new query makes one embedding
request and one text-only matching request; changes to the candidate set invalidate
the matching decision. Neither step downloads the source images again. Both search
requests time out after 20 seconds each. Each polling pass handles at
most one owner's search, retaining later updates for the next pass.
Repeated clicks while an image is queued/sending are coalesced, and replayed
callback updates do not resend it. Failed/interrupted uploads need a new click;
there is no automatic resend after an uncertain outcome. A new click can duplicate
an image if Telegram accepted a previous upload whose response was lost.

Slow searches, graph/account commands, graph discovery, and note/video processing
show a working 👀 reaction on the original message and a typing indicator. Reactions
are cleared when processing finishes, including errors. Image retrieval shows an
upload indicator. Typing/upload statuses refresh every four seconds; unsupported
reactions or Telegram notification failures do not stop the underlying task.
The command-line importer continues to print per-image progress in its terminal.

Rerun the importer to backfill more images or pick up newer ones. `--before <id>`
and `--after <id>` restrict the scan to an exclusive message-ID range. There is no
background account listener or scheduler. Scanning and vector ranking are linear,
appropriate for a personal library. Deleted originals are not reconciled: descriptions
remain in the index and links may stop working. Back up the index as private data;
copy the account session separately only if you deliberately want to keep that
account authorized on another host.

## Access through a static IP without a domain

```sh
python3 scripts/deploy-lightsail.py --profile ctd --public-ip
python3 scripts/smoke-public.py
```

This attaches a dedicated Lightsail static IP and starts the Caddy Compose profile. The IP costs nothing extra while attached; release it after deleting the instance to avoid detached-IP charges. Deployment records the address in `.secrets/lightsail.json` and `.env`.

- Attachment downloads: `https://<PUBLIC_IP>/media/<reference>`.
- Native Logseq DB clients provide the interface; no web client is hosted.
- Logseq **Sync Server URL**: `https://<PUBLIC_IP>:8443`.
- n8n and inbox administration stay behind the SSH tunnel.

Attachment downloads prompt for username **`training`** and **`MEDIA_PASSWORD`** from `.env`. The inbox authenticates these requests behind the HTTPS proxy. Keep this password when migrating so existing links continue to work. To change it, update `.env` and recreate the inbox container. The sync endpoint uses Logseq's login token and account allowlist.

Configure the sync URL once in each native client. The default HTTPS root and former web-client asset paths return 404; port 443 stays available for attachment links already stored in your notes.

Caddy requests a publicly trusted Let's Encrypt IP certificate with the `shortlived` profile and renews it automatically. Preserve `data/caddy/`. Port 80 is globally accessible for HTTP certificate validation; 443 and 8443 accept connections from any IPv4 when using the hardened `selfhost6` image with a configured owner. Sync then requires an allowed, verified Logseq account. Older images retain IP restrictions. SSH remains restricted to the deploying network. Restart the tunnel after first attaching the static IP because the SSH destination changes.

Keep any old browser graph until its notes are synced or exported. Removing the hosted static app does not erase browser-local data, but you need a client/export to access unsynced notes. Confirm a test note syncs between the two native clients.

## Sync account allowlist

The `selfhost6` worker enforces an account allowlist after the existing JWT/PAT verification. Unlisted accounts cannot create graphs or use authenticated sync/API routes. Verified email is required. PATs are checked against their owner's stored identity; caller-supplied email never establishes identity. The unverified-JWT fallback is disabled in this image.

Setup creates `.secrets/sync-policy.env` with a random management credential and an empty `TRAINING_OWNER_EMAIL`. Set the latter to the owner's exact Logseq account email before deployment. Keep this file private and retain it during migration. The owner is always allowed and cannot be removed through the bot. Additional emails are stored in the sync worker's persistent database.

From the configured owner's private Telegram chat:

```text
/sync list
/sync allow person@example.com
/sync remove person@example.com
```

Commands are case-insensitive for email addresses and safe to repeat. Other Telegram users cannot manage the list, including allowed training contributors. Adding an email permits server use, including creating personal graphs; it does not invite that account to an existing graph or allow it to use the Telegram bot. Invite graph members separately in Logseq.

Removal rejects subsequent requests and closes that user's existing sync sockets. It does not erase graph memberships or copies already downloaded to their devices. Requests already in progress may finish. Previously issued signed asset links remain valid until their short expiry (up to five minutes).

The management endpoint `/training/allowlist` requires a separate secret and is blocked on public port 8443, as are upstream admin routes. The bot reaches it over the private Compose network. Attachment downloads still require Basic auth. Public health, preflight, and API metadata endpoints do not grant graph access. Authentication is not protection against traffic floods; keep the worker updated and retain backups.

After replacing `deploy/Caddyfile`, recreate Caddy with `docker compose up -d --force-recreate --no-deps caddy`; a file bind mount can retain the previous inode after an archive deployment. The deployment script does this automatically.

## Connect Logseq DB

This draft uses the community-packaged **sync worker**, not the older Node sync adapter. Their storage formats differ. Logseq collaboration and the worker deployment remain experimental.

1. In each native Logseq DB client's advanced settings, set **Sync Server URL** to `https://<PUBLIC_IP>:8443` for public access, or `http://localhost:8787` through an SSH tunnel. Both people must connect to the same server.
2. Sign in and create a synced DB graph. Invite your brother through the graph's sharing controls. Test both accounts editing the same graph before relying on it.
3. Set `LOGSEQ_GRAPH_ID` in `.env` to the uploaded encrypted graph UUID. In Settings → Personal access tokens, create a **Write** token scoped to this graph and save it in `.secrets/logseq-token`. Never paste it into chat.
4. Run `python3 scripts/unlock-graph-key.py` locally. It reads the owner's encrypted key packages through the deployment SSH connection and prompts for the Logseq encryption password without echoing it. It saves only this graph's AES key in `.secrets/logseq-encryption.json`, bound to the graph UUID. The password and decrypted account private key are never saved. Requires local Node.js and the existing Lightsail SSH configuration.
5. Redeploy. The bot encrypts page titles, names, and blocks using the native graph API. Missing credentials leave submissions queued; the bot never falls back to plaintext graph writes. Successful delivery creates `<video title> — AI pending · <reference>` pages (or **AI not processed** when AI is off), containing the original links/text. Completed AI results rename the same page to **AI processed**. Failed/skipped analysis is labeled **AI not processed**; the reference keeps repeated submissions distinct. Ciphertext and deterministic block UUIDs are persisted before writes so retries reuse them.

Tokens expire; replace the token and redeploy when required. The worker still uses external Cognito login. Giving the bot a graph key means the bot and its VPS can read that graph; keep the VPS and `.secrets/` trusted. Original Telegram submissions are also retained unencrypted in the inbox. Graph-key rotation requires updating the key and reviewing pending encrypted payloads before resuming delivery.

### Self-hosted token authorization

The pinned upstream worker checks hosted RTC subscription status when creating tokens, which returns 403 for this self-hosted setup. `deploy/logseq-selfhost-tokens.patch` skips only that subscription check when `LOGSEQ_SELF_HOSTED=true`; verified login, graph membership, token scopes, and expiry checks remain enabled.

```sh
python3 scripts/build-sync.py
# Set LOGSEQ_SYNC_IMAGE=training-hub-logseq-sync:69da7e-selfhost6 in .env
python3 scripts/deploy-lightsail.py --profile ctd
```

The build also applies `deploy/logseq-proxy-origin.patch`: snapshot and temporary asset URLs preserve HTTPS terminated by the local reverse proxy. Direct HTTP tunnel URLs stay HTTP. The worker port must remain private so forwarded protocol headers come from the trusted proxy. Focused upstream ClojureScript checks cover HTTPS URLs, verified email policy, PAT ownership, management authorization, and add/remove behavior during the build. `deploy/logseq-journal-checklists.patch` adds pinned journal dates and retry-safe native tasks. `deploy/logseq-boolean-checklists.patch` adds initially unchecked boolean items, preserves toggles on retry, and encrypts property labels on E2EE graphs. `deploy/logseq-account-allowlist.patch` and `deploy/sync_allowlist.cljs` enforce the account restriction; migration `0008_training_allowlist.sql` adds its persistent table.

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

All persistent application data is in `data/`; credentials are in `.env` and `.secrets/` (token and graph encryption key). The sync worker stores its local database, objects, and attachments under `data/logseq`. Native Logseq clients also hold local graph state: confirm sync is complete and export a graph backup from the client before moving.

1. Stop Compose on the old host: `docker compose stop`.
2. Make a restricted archive of `data/`, `.env`, `.secrets/logseq-token`, `.secrets/logseq-encryption.json`, `.secrets/gemini-api-key`, `.secrets/sync-policy.env`, and `.secrets/logseq-graphs.json`. Copy it off the VPS; it contains secrets and unencrypted inbox notes/AI results. Preserve `data/inbox/inbox.sqlite`, including graph selections, checklist drafts, queued checklists, AI caches, and quota counters. Keep a previous backup as well.
3. Copy this repository's deployment files and the archive to the new Ubuntu host. For the patched worker, transfer `docker save training-hub-logseq-sync:69da7e-selfhost6` output and restore with `docker load`, or rebuild using `scripts/build-sync.py`. Restore data ownership to UID/GID 1000 and secret permissions to 600, install Docker/Compose, then run `docker compose up -d --build` with the same images.
4. Change your SSH tunnel's destination to the Hetzner IP. Keeping the same localhost ports preserves the sync URL and attachment links. Keep the old host stopped so only one Telegram poller runs.
5. Verify notes, attachments, and a fresh Telegram submission before deleting AWS resources. Never use `docker compose down -v` or remove `data/` as a migration step.

With public-IP access, the AWS IP cannot move to Hetzner. Update `PUBLIC_IP`, `MEDIA_BASE_URL`, client sync URLs, and existing attachment links for the new address; issue a new certificate and test graph synchronization before cutover. The localhost tunnel approach above avoids changing those client URLs.

Sources: [Logseq DB](https://github.com/logseq/docs/blob/master/db-version.md), [self-hosted worker](https://github.com/yshalsager/logseq-selfhost/tree/master/images/sync-worker), [n8n Community Edition](https://docs.n8n.io/hosting/community-edition-features/), [Lightsail pricing](https://aws.amazon.com/lightsail/pricing/).
