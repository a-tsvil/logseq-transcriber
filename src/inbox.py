"""Private Telegram inbox with optional, bounded Gemini analysis."""
import base64
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen
import uuid

if __package__:
    from .analysis import Analyzer, RETRY_STATES, render_analysis
else:
    from analysis import Analyzer, RETRY_STATES, render_analysis

MAX_MEDIA = 20 * 1024 * 1024
NOTICE_STATES = {"needs_approval", "too_long", "unknown_duration", "multiple_videos", "needs_review", "video_unavailable"}


def description_url(value):
    """Only pass canonical post/video URLs to the extractor, never arbitrary hosts."""
    try:
        url = urlsplit(value)
        if url.scheme not in ("http", "https") or url.username or url.password or url.port:
            return None
        host = (url.hostname or "").lower()
        if host in ("instagram.com", "www.instagram.com"):
            if re.fullmatch(r"/(?:p|reels?|tv|share/(?:reel|p))/[A-Za-z0-9_-]+/?", url.path):
                return "https://www.instagram.com" + url.path.rstrip("/") + "/"
        if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"):
            if host.endswith("youtu.be"):
                identifier = url.path.lstrip("/")
            elif url.path == "/watch":
                identifier = parse_qs(url.query).get("v", [""])[0]
            else:
                match = re.fullmatch(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]+)/?", url.path)
                identifier = match[1] if match else ""
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
                return "https://www.youtube.com/watch?v=" + identifier
    except ValueError:
        pass
    return None


def fetch_description(url, timeout):
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-plugin-dirs", "--skip-download",
             "--no-playlist", "--quiet", "--no-warnings", "--socket-timeout", "5", "--retries", "0",
             "--extractor-retries", "0", "--ignore-no-formats-error", "--output-na-placeholder", "",
             "--print", "%(.{description,duration,title})j", "--", url],
            capture_output=True, text=True, timeout=timeout, check=True)
        # A carousel can emit the same caption for each video.
        items = [json.loads(line) for line in result.stdout.splitlines()]
        captions = list(dict.fromkeys(item["description"].strip() for item in items
                                     if isinstance(item, dict) and isinstance(item.get("description"), str) and item["description"].strip()))
        text = "\n\n".join(captions)
        description = {"status": "copied", "text": text} if text else {"status": "empty"}
        titles = [item["title"].strip() for item in items if isinstance(item, dict) and isinstance(item.get("title"), str) and item["title"].strip()]
        if titles:
            description["title"] = titles[0]
        if len(items) == 1 and isinstance(items[0], dict):
            duration = items[0].get("duration")
            description["duration_seconds"] = duration if type(duration) in (int, float) and math.isfinite(duration) and duration > 0 else None
        elif len(items) > 1:
            description["video_count"] = len(items)
        return description
    except (subprocess.SubprocessError, OSError, ValueError):
        return {"status": "unavailable"}


def request_json(url, payload=None, headers=None, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", **(headers or {})}
    with urlopen(Request(url, data=data, headers=headers, method=method), timeout=30) as response:
        return json.load(response)


class Inbox:
    def __init__(self, data, allowed_ids, bot_token="", graph_id="", api_url="", token_file="", encryption_file="",
                 ai_enabled=False, ai_key_file="", text_model="gemini-3.5-flash-lite", video_model="gemini-3.5-flash-lite"):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        for folder in ("media", "exports"):
            (self.data / folder).mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.data / "inbox.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), offset INTEGER NOT NULL)")
        self.db.execute("INSERT OR IGNORE INTO state VALUES (1, 0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY, record TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, attempted INTEGER NOT NULL DEFAULT 0)")
        self.db.commit()
        self.allowed = {int(value) for value in allowed_ids.split(",") if value.strip()}
        self.bot_token = bot_token
        self.graph_id = graph_id
        self.api_url = api_url.rstrip("/")
        self.token_file = Path(token_file) if token_file else None
        self.encryption_file = Path(encryption_file) if encryption_file else None
        self.ai = Analyzer(self.db, self.data, ai_enabled, ai_key_file, text_model, video_model)

    def approve_analysis(self, text):
        match = re.fullmatch(r"/analyze\s+(\d+)", text.strip())
        if not match:
            return "Usage: /analyze <reference>"
        row = self.db.execute("SELECT record FROM submissions WHERE id=?", (int(match[1]),)).fetchone()
        if not row:
            return "Unknown reference."
        record = json.loads(row[0])
        if record["analysis"]["status"] != "needs_approval":
            return "This submission is not waiting for video approval."
        record["analysis"].update(approved=True, status="pending_video")
        record["analysis"].pop("notified", None)
        self.save(record)
        return f"Video analysis approved and queued. Reference: {record['id']}"

    def telegram(self, method, payload):
        result = request_json(f"https://api.telegram.org/bot{self.bot_token}/{method}", payload)
        if not result.get("ok"):
            raise ValueError("Telegram request failed")
        return result["result"]

    def remember(self, updates):
        with self.db:
            for update in updates:
                update_id = update["update_id"]
                message = update.get("message", {})
                sender = message.get("from", {})
                if sender.get("id") in self.allowed and message.get("chat", {}).get("type") == "private":
                    text = message.get("text", message.get("caption", ""))
                    if text.split(maxsplit=1)[:1] == ["/analyze"]:
                        reply = self.approve_analysis(text)
                        if self.bot_token:
                            self.telegram("sendMessage", {"chat_id": message["chat"]["id"], "text": reply})
                        self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))
                        continue
                    attachment = message.get("video") or message.get("document") or message.get("animation")
                    if text or attachment:
                        record = {
                            "id": update_id, "chat_id": message["chat"]["id"],
                            "message_id": message["message_id"], "submitted_by": sender["id"],
                            "created_at": datetime.fromtimestamp(message["date"], timezone.utc).isoformat(),
                            "original_text": text, "source_urls": re.findall(r"https?://[^\s<>]+", text),
                            "attachment": attachment, "media_status": "pending" if attachment else "none",
                            "analysis": {"status": "pending", "transcript": None, "exercises": []},
                            "publish_status": "pending", "acknowledged": False,
                        }
                        self.db.execute("INSERT OR IGNORE INTO submissions (id, record) VALUES (?, ?)", (update_id, json.dumps(record)))
                self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))

    def pending(self):
        if self.bot_token and self.allowed:
            offset = self.db.execute("SELECT offset FROM state WHERE id=1").fetchone()[0]
            self.remember(self.telegram("getUpdates", {"offset": offset, "timeout": 0, "limit": 20, "allowed_updates": ["message"]}))
        rows = self.db.execute("SELECT record FROM submissions WHERE done=0 AND COALESCE(json_extract(record,'$.analysis.retry_after'),0)<=unixepoch() ORDER BY attempted, id LIMIT 10").fetchall()
        return {"submissions": [json.loads(row[0]) for row in rows]}

    def save(self, record, done=False):
        text = json.dumps(record, ensure_ascii=False, indent=2)
        with self.db:
            self.db.execute("UPDATE submissions SET record=?, done=? WHERE id=?", (text, int(done), record["id"]))
        target = self.data / "exports" / f"{record['id']}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(text)
        temporary.replace(target)

    def download(self, record):
        attachment = record["attachment"]
        if not attachment or record["media_status"] != "pending":
            return
        # shortcut: Telegram's hosted Bot API caps downloads at 20 MB; use its local server for larger uploads.
        if attachment.get("file_size", 0) > MAX_MEDIA:
            record["media_status"] = "too_large"
            return
        remote = self.telegram("getFile", {"file_id": attachment["file_id"]})
        path = remote["file_path"]
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("Invalid Telegram file path")
        temporary = self.data / "media" / f"{record['id']}.tmp"
        try:
            url = f"https://api.telegram.org/file/bot{self.bot_token}/{quote(path, safe='/')}"
            with urlopen(url, timeout=60) as response, temporary.open("wb") as output:
                total = 0
                while chunk := response.read(64 * 1024):
                    total += len(chunk)
                    if total > MAX_MEDIA:
                        record["media_status"] = "too_large"
                        return
                    output.write(chunk)
            temporary.replace(self.data / "media" / str(record["id"]))
            record["media_status"] = "saved"
        finally:
            temporary.unlink(missing_ok=True)

    def publish(self, record):
        if record["publish_status"] == "published":
            return
        token = self.token_file.read_text().strip() if self.token_file and self.token_file.exists() else ""
        if not self.graph_id or not token:
            record["publish_status"] = "waiting_for_logseq"
            return
        if not self.encryption_file or not self.encryption_file.exists() or not self.encryption_file.stat().st_size:
            record["publish_status"] = "waiting_for_logseq_key"
            return
        key = json.loads(self.encryption_file.read_text())
        if (not isinstance(key, dict) or key.get("graph_id") != self.graph_id
                or not isinstance(key.get("key"), str) or len(base64.b64decode(key["key"], validate=True)) != 32):
            raise ValueError("Graph encryption key does not match configuration")
        base = f"{self.api_url}/api/v1/graphs/{quote(self.graph_id, safe='')}"
        headers = {"Authorization": f"Bearer {token}"}
        if not record.get("encrypted_payload"):
            title = self.page_title(record)
            lines = ["[[Training inbox]]", f"Submitted: {record['created_at']} by {record['submitted_by']}",
                     "AI analysis: queued; results will be appended when available." if self.ai.enabled else "AI analysis: disabled.",
                     "Original text: " + (record["original_text"] or "No caption or text supplied.")]
            if record["source_urls"]:
                lines.append("\n".join(record["source_urls"]))
            if record["media_status"] == "saved":
                media_url = os.environ.get("MEDIA_BASE_URL", "http://localhost:8001").rstrip("/")
                lines.append(f"[Original attachment]({media_url}/media/{record['id']})")
            elif record["media_status"] == "too_large":
                lines.append("Attachment exceeds Telegram's 20 MB download limit; upload a smaller file or share a source link.")
            captions = []
            for description in record.get("descriptions", []):
                if description["status"] == "copied":
                    captions.append(f"Post description ({description['url']}):\n{description['text']}")
                elif description["status"] != "unsupported":
                    captions.append(f"Post description ({description['url']}): {description['status']}; original link retained.")
            if captions:
                lines.append("\n\n".join(captions))
            response = request_json(base + "/e2ee/encrypt", {"key": key["key"], "texts": [title, title.lower(), *lines]}, headers)
            encrypted = response.get("texts") if isinstance(response, dict) else None
            if not isinstance(encrypted, list) or len(encrypted) != len(lines) + 2 or not all(isinstance(value, str) and value for value in encrypted):
                raise ValueError("Invalid encryption response")
            record["encrypted_payload"] = {
                "graph_id": self.graph_id, "page": {"title": encrypted[0], "name": encrypted[1]},
                "label": title,
                "blocks": [{"uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"training-hub:{record['id']}:{index}")), "title": text}
                           for index, text in enumerate(encrypted[2:])],
            }
            # Persist randomized ciphertext before writing, so page creation can be retried safely.
            self.save(record)
        prepared = record["encrypted_payload"]
        if prepared["graph_id"] != self.graph_id:
            raise ValueError("Prepared submission belongs to another graph")
        if not record.get("page_id"):
            page = request_json(base + "/pages", prepared["page"], headers)
            record["page_id"] = page["uuid"]
            record["page_title"] = prepared.get("label", f"Training inbox - {record['id']}")
            self.save(record)
        request_json(base + f"/blocks/{quote(record['page_id'], safe='')}/children", {"position": "append", "blocks": prepared["blocks"]}, headers)
        record["publish_status"] = "published"

    def page_title(self, record):
        title = next((d["title"] for d in record.get("descriptions", []) if d.get("title")), None)
        title = title or (record.get("attachment") or {}).get("file_name") or "Training inbox"
        title = re.sub(r"\s+", " ", title).strip()[:160]
        analysis = record["analysis"]
        status = "AI not processed"
        if analysis["status"] == "completed" and analysis.get("published"):
            status = "AI processed"
        elif self.ai.enabled and (analysis["status"] in RETRY_STATES or analysis["status"] == "needs_approval" or analysis["status"] == "completed"):
            status = "AI pending"
        return f"{title} — {status} · {record['id']}"

    def rename_page(self, record):
        title = self.page_title(record)
        if record.get("page_title") == title:
            return
        if record["encrypted_payload"]["graph_id"] != self.graph_id:
            raise ValueError("Prepared submission belongs to another graph")
        base = f"{self.api_url}/api/v1/graphs/{quote(self.graph_id, safe='')}"
        headers = {"Authorization": "Bearer " + self.token_file.read_text().strip()}
        if record.get("page_rename", {}).get("label") != title:
            key = json.loads(self.encryption_file.read_text())
            if key.get("graph_id") != self.graph_id:
                raise ValueError("Graph encryption key does not match configuration")
            response = request_json(base + "/e2ee/encrypt", {"key": key["key"], "texts": [title, title.lower()]}, headers)
            texts = response.get("texts") if isinstance(response, dict) else None
            if not isinstance(texts, list) or len(texts) != 2 or not all(isinstance(t, str) and t for t in texts):
                raise ValueError("Invalid title encryption response")
            record["page_rename"] = {"label": title, "page": {"title": texts[0], "name": texts[1]}}
            self.save(record)
        request_json(base + f"/pages/{quote(record['page_id'], safe='')}", record["page_rename"]["page"], headers, method="PATCH")
        record["page_title"] = title
        record.pop("page_rename", None)

    def publish_analysis(self, record):
        analysis = record["analysis"]
        if analysis.get("published") or analysis["status"] != "completed":
            return
        if record["encrypted_payload"]["graph_id"] != self.graph_id:
            raise ValueError("Prepared submission belongs to another graph")
        base = f"{self.api_url}/api/v1/graphs/{quote(self.graph_id, safe='')}"
        token = self.token_file.read_text().strip()
        headers = {"Authorization": f"Bearer {token}"}
        if not analysis.get("encrypted_title"):
            key = json.loads(self.encryption_file.read_text())
            if key.get("graph_id") != self.graph_id or record["encrypted_payload"]["graph_id"] != self.graph_id:
                raise ValueError("Graph encryption key does not match configuration")
            response = request_json(base + "/e2ee/encrypt", {"key": key["key"], "texts": [render_analysis(analysis)]}, headers)
            texts = response.get("texts") if isinstance(response, dict) else None
            if not isinstance(texts, list) or len(texts) != 1 or not isinstance(texts[0], str) or not texts[0]:
                raise ValueError("Invalid analysis encryption response")
            analysis["encrypted_title"] = texts[0]
            self.save(record)
        block = {"uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"training-hub:{record['id']}:analysis:v1")), "title": analysis["encrypted_title"]}
        request_json(base + f"/blocks/{quote(record['page_id'], safe='')}/children", {"position": "append", "blocks": [block]}, headers)
        analysis["published"] = True

    def analyze(self, record):
        if record["analysis"]["status"] in RETRY_STATES:
            record["analysis"] = self.ai.step(record)
            self.save(record)
        self.publish_analysis(record)
        self.rename_page(record)
        analysis = record["analysis"]
        if analysis["status"] in NOTICE_STATES and not analysis.get("notified"):
            if self.bot_token:
                self.telegram("sendMessage", {"chat_id": record["chat_id"], "text": f"{analysis['reason']} Reference: {record['id']}"})
            analysis["notified"] = True

    def describe(self, record):
        if "descriptions" in record or record.get("encrypted_payload"):
            return
        deadline = time.monotonic() + 20
        descriptions = []
        for original in dict.fromkeys(record["source_urls"]):
            url = description_url(original)
            if not url:
                result = {"status": "unsupported"}
            elif time.monotonic() >= deadline:
                result = {"status": "unavailable"}
            else:
                result = fetch_description(url, timeout=max(0.1, deadline - time.monotonic()))
            descriptions.append({"url": url or original, **result})
        record["descriptions"] = descriptions
        self.save(record)

    def capture(self, identifier):
        if type(identifier) is not int:
            raise ValueError("id must be an integer")
        row = self.db.execute("SELECT record, done FROM submissions WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("Unknown submission")
        record = json.loads(row[0])
        was_published = record["publish_status"] == "published"
        if row[1]:
            return {"id": identifier, "status": "already_processed"}
        with self.db:
            self.db.execute("UPDATE submissions SET attempted=unixepoch() WHERE id=?", (identifier,))
        # Save before external calls so outages cannot discard a submission.
        self.save(record)
        try:
            self.download(record)
            self.describe(record)
            self.publish(record)
            if was_published and self.ai.enabled:
                self.analyze(record)
            record.pop("last_error", None)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError) as error:
            # Exception messages may contain a Telegram token in the request URL.
            record["last_error"] = f"{type(error).__name__} (HTTP {getattr(error, 'code', 'n/a')})"
        self.save(record)
        if not record["acknowledged"] and self.bot_token:
            status = "Saved in Logseq." if record["publish_status"] == "published" else "Saved in the inbox; Logseq delivery is pending."
            if record["media_status"] == "too_large":
                status += " Attachment exceeds 20 MB; please send a smaller file or source link."
            if record.get("last_error") and record["media_status"] == "pending":
                status += " Attachment download is pending; keep the original."
            ai_status = "AI analysis is queued." if self.ai.enabled else "AI analysis is disabled."
            self.telegram("sendMessage", {"chat_id": record["chat_id"], "text": f"{status} {ai_status} Reference: {identifier}"})
            record["acknowledged"] = True
        done = record["publish_status"] == "published" and record["media_status"] != "pending" and record["acknowledged"]
        if self.ai.enabled:
            done = done and record["analysis"]["status"] not in RETRY_STATES
            done = done and record.get("page_title") == self.page_title(record)
            if record["analysis"]["status"] == "completed":
                done = done and record["analysis"].get("published", False)
            if record["analysis"]["status"] in NOTICE_STATES:
                done = done and record["analysis"].get("notified", False)
        self.save(record, done=done)
        return {"id": identifier, "publish_status": record["publish_status"], "media_status": record["media_status"], "analysis_status": record["analysis"]["status"]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def respond(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        token = os.environ.get("BRIDGE_TOKEN", "")
        return bool(token) and hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token)

    def do_GET(self):
        if self.path == "/health":
            return self.respond(200, {"ok": True, "ai": "enabled" if self.server.inbox.ai.enabled else "disabled"})
        if re.fullmatch(r"/media/\d+", self.path):
            password = os.environ.get("MEDIA_PASSWORD", "")
            expected = "Basic " + base64.b64encode(f"training:{password}".encode()).decode()
            if not password or not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Training attachments"')
                self.end_headers()
                return
            target = self.server.inbox.data / "media" / self.path.rsplit("/", 1)[1]
            if not target.is_file():
                return self.respond(404, {"error": "Attachment not found"})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="training-{target.name}"')
            self.send_header("Content-Length", str(target.stat().st_size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with target.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    self.wfile.write(chunk)
            return
        if not self.authorized():
            return self.respond(401, {"error": "Unauthorized"})
        if self.path != "/pending":
            return self.respond(404, {"error": "Not found"})
        try:
            self.respond(200, self.server.inbox.pending())
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            self.respond(502, {"error": type(error).__name__})

    def do_POST(self):
        if not self.authorized():
            return self.respond(401, {"error": "Unauthorized"})
        if self.path != "/capture":
            return self.respond(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                return self.respond(413, {"error": "Invalid request size"})
            payload = json.loads(self.rfile.read(length))
            self.respond(200, self.server.inbox.capture(payload["id"]))
        except (ValueError, KeyError, TypeError):
            self.respond(400, {"error": "Invalid submission"})
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            self.respond(502, {"error": type(error).__name__})


if __name__ == "__main__":
    os.umask(0o077)
    server = HTTPServer(("0.0.0.0", 8080), Handler)
    server.inbox = Inbox(os.environ.get("DATA_DIR", "/data"), os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""),
                         os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("LOGSEQ_GRAPH_ID", ""),
                         os.environ.get("LOGSEQ_API_URL", ""), os.environ.get("LOGSEQ_TOKEN_FILE", ""),
                         os.environ.get("LOGSEQ_ENCRYPTION_FILE", ""), os.environ.get("AI_ENABLED", "false").lower() == "true",
                         os.environ.get("GEMINI_KEY_FILE", ""), os.environ.get("GEMINI_TEXT_MODEL", "gemini-3.5-flash-lite"),
                         os.environ.get("GEMINI_VIDEO_MODEL", "gemini-3.5-flash-lite"))
    print("Training inbox listening; AI " + ("enabled." if server.inbox.ai.enabled else "disabled."))
    server.serve_forever()
