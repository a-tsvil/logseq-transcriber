"""Private Telegram inbox with optional, bounded Gemini analysis."""
import base64
from contextlib import nullcontext
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen
import uuid
from zoneinfo import ZoneInfo

if __package__:
    from .activity import telegram_activity
    from .analysis import Analyzer, RETRY_STATES, render_analysis
    from .image_search import ImageIndex, render_matches
else:
    from activity import telegram_activity
    from analysis import Analyzer, RETRY_STATES, render_analysis
    from image_search import ImageIndex, render_matches

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
                 ai_enabled=False, ai_key_file="", text_model="gemini-3.5-flash-lite", video_model="gemini-3.5-flash-lite",
                 admin_id="", allowlist_token="", graph_name="", graphs_file="", logseq_users="", image_search_enabled=False):
        self.logseq_users = json.loads(logseq_users or "{}")
        if (not isinstance(self.logseq_users, dict)
                or not all(isinstance(k, str) and k.isdigit() and isinstance(v, str)
                           and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", v) for k, v in self.logseq_users.items())):
            raise ValueError("TELEGRAM_LOGSEQ_USERS must map Telegram IDs to verified Logseq emails")
        self.logseq_users = {k: v.lower() for k, v in self.logseq_users.items()}
        self.graph_name = graph_name.strip()
        self.zone = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))
        self.extra_graphs = json.loads(Path(graphs_file).read_text()) if graphs_file else []
        if not isinstance(self.extra_graphs, list):
            raise ValueError("Graph configuration must be a list")
        ids, names = {graph_id.casefold()}, {(self.graph_name or graph_id).casefold()} if graph_id else set()
        for graph in self.extra_graphs:
            if (not isinstance(graph, dict) or set(graph) != {"id", "name", "token", "key"}
                    or not all(isinstance(graph.get(k), str) and graph[k].strip() for k in ("id", "name", "token"))
                    or not (graph["key"] is None or isinstance(graph["key"], str))):
                raise ValueError("Each configured graph needs id, name, token, and key")
            graph["id"] = str(uuid.UUID(graph["id"]))
            graph["name"] = graph["name"].strip()
            if (graph["id"] in ids or graph["id"] in names or graph["name"].casefold() in names
                    or graph["name"].casefold() in ids
                    or (graph["key"] is not None and len(base64.b64decode(graph["key"], validate=True)) != 32)):
                raise ValueError("Duplicate graph or invalid graph key")
            ids.add(graph["id"])
            names.add(graph["name"].casefold())

        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        for folder in ("media", "exports"):
            (self.data / folder).mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.data / "inbox.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), offset INTEGER NOT NULL)")
        self.db.execute("INSERT OR IGNORE INTO state VALUES (1, 0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY, record TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, attempted INTEGER NOT NULL DEFAULT 0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS user_graphs (user_id INTEGER PRIMARY KEY, graph_id TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS checklist_drafts (user_id INTEGER PRIMARY KEY, draft TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS cooking_requests (user_id INTEGER PRIMARY KEY)")
        self.db.execute("CREATE TABLE IF NOT EXISTS bot_users (user_id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                        "status TEXT NOT NULL, requested_at INTEGER NOT NULL DEFAULT (unixepoch()))")
        self.db.execute("CREATE TABLE IF NOT EXISTS bot_user_graphs (user_id INTEGER NOT NULL, graph_id TEXT NOT NULL, "
                        "PRIMARY KEY(user_id,graph_id))")
        # Pin older queued submissions before an operator changes the default graph.
        self.db.execute("UPDATE submissions SET record=json_set(record,'$.graph_id',COALESCE(json_extract(record,'$.encrypted_payload.graph_id'),?)) "
                        "WHERE done=0 AND json_extract(record,'$.graph_id') IS NULL AND ?!=''", (graph_id, graph_id))
        self.db.commit()
        self.allowed = {int(value) for value in allowed_ids.split(",") if value.strip()}
        if self.allowed:
            self.allowed.update(row[0] for row in self.db.execute("SELECT user_id FROM bot_users WHERE status='approved'"))
        self.allowed.difference_update(row[0] for row in self.db.execute("SELECT user_id FROM bot_users WHERE status='denied'")
                                       if str(row[0]) != str(admin_id))
        self.bot_token = bot_token
        self.graph_id = graph_id
        self.api_url = api_url.rstrip("/")
        self.token_file = Path(token_file) if token_file else None
        self.encryption_file = Path(encryption_file) if encryption_file else None
        self.ai = Analyzer(self.db, self.data, ai_enabled, ai_key_file, text_model, video_model)
        self.admin_id = int(admin_id) if admin_id else None
        self.allowlist_token = allowlist_token
        self.image_search_enabled = image_search_enabled

    def search_images(self, text, user_id):
        self.image_keyboard = None
        if not self.admin_id or user_id != self.admin_id:
            return "Image search is available only to the bot owner."
        if not self.image_search_enabled:
            return "Image search is disabled. Set IMAGE_SEARCH_ENABLED=true after importing selected chats."
        parts = text.split(maxsplit=1)
        query = parts[1].strip() if len(parts) == 2 else ""
        if not 1 <= len(query) <= 500:
            return "Use /search followed by a description of up to 500 characters."
        try:
            index = ImageIndex(self.data, self.ai.key_file, self.ai.text_model)
            matches = index.search(user_id, query)
            self.image_keyboard = index.keyboard(user_id, matches)
            return render_matches(matches)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            return "Image search failed. Check the Gemini key, provider quota, and index configuration; retry later."

    def activity(self, chat_id, message_id=None, action="typing"):
        return telegram_activity(self.telegram, chat_id, message_id, action) if self.bot_token else nullcontext()

    def registration_panel(self):
        rows = self.db.execute("SELECT user_id,name,status FROM bot_users ORDER BY status='pending' DESC,requested_at DESC,user_id LIMIT 10").fetchall()
        payload = {"chat_id": self.admin_id, "text": "No access requests or registered posting users."}
        if rows:
            payload["text"] = "Posting users and access requests:\n" + "\n".join(
                f"{n}. {name[:80]} · ID {user_id} · {status}" for n, (user_id, name, status) in enumerate(rows, 1))
            buttons = []
            for n, (user_id, _, status) in enumerate(rows, 1):
                granted = {row[0] for row in self.db.execute("SELECT graph_id FROM bot_user_graphs WHERE user_id=?", (user_id,))}
                for graph in self.graph_catalog():
                    action = "drop" if graph["id"] in granted else "allow"
                    label = ("Remove" if action == "drop" else "Approve") + f" {n} → {graph['name'][:35]}"
                    buttons.append([{"text": label, "callback_data": f"user:{action}:{user_id}:{graph['id']}"}])
                buttons.append([{"text": f"Disable {n}" if status == "approved" else f"Deny {n}", "callback_data": f"user:deny:{user_id}"}])
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return payload

    def request_registration(self, sender):
        if not self.admin_id or self.admin_id not in self.allowed:
            return "Registration is not configured. Please contact the bot owner."
        row = self.db.execute("SELECT status FROM bot_users WHERE user_id=?", (sender["id"],)).fetchone()
        if row:
            return ("Your access request is waiting for the bot owner's approval." if row[0] == "pending"
                    else "Access has not been granted. Please contact the bot owner.")
        name = " ".join(str(sender.get(k, "")) for k in ("first_name", "last_name")).strip()
        if sender.get("username"):
            name += " (@" + sender["username"] + ")"
        name = " ".join(name.split())[:200] or str(sender["id"])
        with self.db:
            self.db.execute("INSERT INTO bot_users(user_id,name,status) VALUES (?,?,'pending')", (sender["id"], name))
        if self.bot_token:
            try:
                self.telegram("sendMessage", self.registration_panel())
            except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                pass
        return "Access requested. The bot owner needs to approve you before you can use the bot."

    def registration_callback(self, callback):
        chat = callback.get("message", {}).get("chat", {})
        sender = callback.get("from", {}).get("id")
        match = re.fullmatch(r"user:(allow|drop|deny):([1-9]\d{0,15})(?::([0-9a-f-]{36}))?", callback.get("data", ""))
        reply = "Only the bot owner can approve access requests."
        notify_user = None
        if self.admin_id and sender == self.admin_id and sender in self.allowed and chat.get("type") == "private" and chat.get("id") == sender and match:
            user_id = int(match[2])
            row = self.db.execute("SELECT status FROM bot_users WHERE user_id=?", (user_id,)).fetchone()
            reply = "This access request has already been handled or is unavailable."
            if row and user_id != self.admin_id:
                if match[1] == "deny" and row[0] != "denied":
                    with self.db:
                        self.db.execute("UPDATE bot_users SET status='denied' WHERE user_id=?", (user_id,))
                        self.db.execute("DELETE FROM bot_user_graphs WHERE user_id=?", (user_id,))
                        self.db.execute("DELETE FROM user_graphs WHERE user_id=?", (user_id,))
                    self.allowed.discard(user_id)
                    reply = "User access disabled."
                elif match[1] in ("allow", "drop") and match[3]:
                    try:
                        with self.activity(sender, callback.get("message", {}).get("message_id")):
                            graph = self.find_graph(match[3], self.admin_id)
                    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                        graph = None
                    reply = "Unable to verify graph access. Refresh /users and try again."
                    if graph and graph["enabled"]:
                        existing = self.db.execute("SELECT 1 FROM bot_user_graphs WHERE user_id=? AND graph_id=?", (user_id, graph["id"])).fetchone()
                        if match[1] == "allow" and not existing:
                            with self.db:
                                self.db.execute("UPDATE bot_users SET status='approved' WHERE user_id=?", (user_id,))
                                self.db.execute("INSERT INTO bot_user_graphs VALUES (?,?)", (user_id, graph["id"]))
                                self.db.execute("INSERT OR IGNORE INTO user_graphs VALUES (?,?)", (user_id, graph["id"]))
                                selected = self.selected_graph(user_id)
                                if not self.db.execute("SELECT 1 FROM bot_user_graphs WHERE user_id=? AND graph_id=?", (user_id, selected)).fetchone():
                                    self.db.execute("UPDATE user_graphs SET graph_id=? WHERE user_id=?", (graph["id"], user_id))
                            self.allowed.add(user_id)
                            notify_user = user_id
                            reply = "Posting access granted to " + graph["name"] + "."
                        elif match[1] == "drop" and existing:
                            with self.db:
                                self.db.execute("DELETE FROM bot_user_graphs WHERE user_id=? AND graph_id=?", (user_id, graph["id"]))
                                if self.selected_graph(user_id) == graph["id"]:
                                    remaining = self.db.execute("SELECT graph_id FROM bot_user_graphs WHERE user_id=? ORDER BY graph_id LIMIT 1", (user_id,)).fetchone()
                                    if remaining:
                                        self.db.execute("UPDATE user_graphs SET graph_id=? WHERE user_id=?", (remaining[0], user_id))
                                    else:
                                        self.db.execute("DELETE FROM user_graphs WHERE user_id=?", (user_id,))
                            reply = "Posting access removed from " + graph["name"] + "."
                        else:
                            reply = "That graph access is already configured."
        if self.bot_token:
            try:
                self.telegram("answerCallbackQuery", {"callback_query_id": callback["id"], "text": reply})
            except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                pass
            if notify_user:
                try:
                    self.telegram("setMyCommands", {"scope": {"type": "chat", "chat_id": notify_user}, "commands": self.commands(notify_user)})
                    self.telegram("setChatMenuButton", {"chat_id": notify_user, "menu_button": {"type": "commands"}})
                    self.telegram("sendMessage", {"chat_id": notify_user, "text": reply + " Send /select_graph or /help to get started."})
                except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                    pass

    def image_callback(self, callback):
        sender = callback.get("from", {}).get("id")
        chat = callback.get("message", {}).get("chat", {})
        data = callback.get("data", "")
        reply = "Image search is available only to the bot owner in a private chat."
        description = None
        show_alert = False
        if (self.image_search_enabled and self.admin_id and sender == self.admin_id and sender in self.allowed
                and chat.get("type") == "private" and chat.get("id") == sender
                and isinstance(data, str) and data.startswith(("image:", "desc:")) and len(data) <= 64):
            index = ImageIndex(self.data, self.ai.key_file, self.ai.text_model)
            if data.startswith("image:"):
                reply = index.queue_image(data[6:], sender, callback["id"])
            else:
                description = index.image_description(data[5:], sender)
                reply = "This description is unavailable. Run /search again."
                if description:
                    show_alert = len(description.encode("utf-16-le")) // 2 <= 200
                    reply = description if show_alert else "Showing description."
        if self.bot_token:
            try:
                self.telegram("answerCallbackQuery", {"callback_query_id": callback["id"], "text": reply,
                                                      "show_alert": show_alert})
            except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                # A delayed/expired callback must not undo an already queued image request.
                pass
            if description and not show_alert:
                with self.activity(sender):
                    for offset in range(0, len(description), 2000):
                        try:
                            self.telegram("sendMessage", {"chat_id": sender, "text": description[offset:offset + 2000],
                                                          "link_preview_options": {"is_disabled": True}})
                        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                            break

    def graph_catalog(self, user_id=None):
        default = [{"id": self.graph_id, "name": self.graph_name or self.graph_id}] if self.graph_id else []
        configured = default + self.extra_graphs
        if user_id is None:
            return configured
        if self.registered_user(user_id):
            row = self.db.execute("SELECT status FROM bot_users WHERE user_id=?", (user_id,)).fetchone()
            if row[0] != "approved" or not self.admin_id:
                return []
            grants = {r[0] for r in self.db.execute("SELECT graph_id FROM bot_user_graphs WHERE user_id=?", (user_id,))}
            if not grants:
                return []
            authorized = {g["id"] for g in self.graph_catalog(self.admin_id)}
            return [{"id": g["id"], "name": g["name"], "enabled": True}
                    for g in configured if g["id"] in grants and g["id"] in authorized]
        ready = {g["id"] for g in configured}
        if not self.logseq_users:
            return [{"id": g["id"], "name": g["name"], "enabled": True} for g in configured]
        email = self.logseq_users.get(str(user_id))
        if not email:
            raise ValueError("Your Telegram account is not linked to Logseq. Ask the bot owner to configure it.")
        if not self.allowlist_token:
            raise ValueError("Shared graph discovery is not configured on the bot.")
        response = request_json(self.api_url + "/training/graphs?email=" + quote(email, safe=""),
                                headers={"Authorization": "Bearer " + self.allowlist_token})
        rows = response.get("graphs") if isinstance(response, dict) else None
        if (not isinstance(rows, list)
                or not all(isinstance(g, dict) and isinstance(g.get("id"), str)
                           and isinstance(g.get("name"), str) and g["name"].strip() for g in rows)):
            raise ValueError("Shared graph discovery returned invalid metadata.")
        return [{"id": g["id"], "name": g["name"], "enabled": g["id"] in ready} for g in rows]

    def registered_user(self, user_id):
        return user_id != self.admin_id and bool(self.db.execute("SELECT 1 FROM bot_users WHERE user_id=?", (user_id,)).fetchone())

    def selected_graph(self, user_id):
        row = self.db.execute("SELECT graph_id FROM user_graphs WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else self.graph_id

    def find_graph(self, value, user_id=None):
        catalog = self.graph_catalog(user_id)
        by_id = next((g for g in catalog if g["id"].casefold() == value.casefold()), None)
        if by_id:
            return by_id
        matches = [g for g in catalog if g["name"].casefold() == value.casefold()]
        if len(matches) > 1:
            raise ValueError("Several graphs have that name. Use /select_graph to choose one.")
        return matches[0] if matches else None

    def graph_command(self, text, user_id):
        self.graph_keyboard = None
        try:
            return self._graph_command(text, user_id)
        except (HTTPError, URLError, TimeoutError, OSError):
            return "Unable to refresh shared graphs. Please try again later."
        except ValueError as error:
            return str(error)

    def _graph_command(self, text, user_id):
        try:
            args = shlex.split(text)
        except ValueError:
            return 'Use /select_graph, then tap a graph.'
        current = self.selected_graph(user_id)
        if args in (["/graphs"], ["/select_graph"], ["/select-graph"]):
            graphs = self.graph_catalog(user_id)
            selected = next((graph for graph in graphs if graph["id"] == current), None)
            lines = []
            buttons = []
            for graph in graphs:
                duplicate = sum(g["name"].casefold() == graph["name"].casefold() for g in graphs) > 1
                label = graph["name"] + (f" [{graph['id']}]" if duplicate else "")
                button_label = label
                label += " (selected)" if graph["id"] == current else ""
                label += " — ready" if graph["enabled"] else " — bot setup required"
                lines.append(label)
                if graph["enabled"]:
                    buttons.append([{"text": ("✓ " if graph["id"] == current else "") + button_label,
                                     "callback_data": f"graph:{user_id}:{graph['id']}"}])
            self.graph_keyboard = {"inline_keyboard": buttons}
            return ("Current graph: " + (selected["name"] if selected else "unavailable; choose a graph below")
                    + "\n\nAvailable Logseq graphs:\n" + ("\n".join(lines) or "No graphs available to you.")
                    + ("\n\nTap a graph to select it." if buttons else ""))
        if args == ["/graph"]:
            graph = self.find_graph(current, user_id)
            return "Selected graph: " + (graph["name"] if graph else "no longer available; use /select_graph")
        if len(args) == 2 and args[0] == "/graph":
            graph = self.find_graph(args[1], user_id)
            if not graph:
                return "Graph not available to you. Use /select_graph to choose an available graph."
            if not graph["enabled"]:
                return "Graph found, but bot setup is required: a scoped token, plus an encryption key for encrypted graphs."
            self.db.execute("INSERT INTO user_graphs VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET graph_id=excluded.graph_id",
                            (user_id, graph["id"]))
            return "Selected graph: " + graph["name"]
        return 'Use /select_graph, then tap a graph.'

    def graph_callback(self, callback):
        sender = callback.get("from", {}).get("id")
        message = callback.get("message", {})
        chat = message.get("chat", {})
        match = re.fullmatch(r"graph:([1-9]\d{0,15}):([0-9a-f-]{36})", callback.get("data", ""))
        reply = "Open /select_graph in your private chat with the bot."
        if (match and sender in self.allowed and int(match[1]) == sender
                and chat.get("type") == "private" and chat.get("id") == sender):
            with self.activity(sender, message.get("message_id")):
                with self.db:
                    reply = self.graph_command("/graph " + match[2], sender)
                text = self.graph_command("/select_graph", sender)
                if self.bot_token:
                    try:
                        self.telegram("editMessageText", {"chat_id": sender, "message_id": message["message_id"],
                                                         "text": text, "reply_markup": self.graph_keyboard or {"inline_keyboard": []}})
                    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                        pass
        if self.bot_token:
            try:
                self.telegram("answerCallbackQuery", {"callback_query_id": callback["id"], "text": reply[:200]})
            except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                pass

    def graph_credentials(self, record):
        graph_id = record.get("graph_id") or record.get("encrypted_payload", {}).get("graph_id") or self.graph_id
        if ((self.logseq_users or self.registered_user(record.get("submitted_by")))
                and (record.get("submitted_by") is None or not self.find_graph(graph_id, record["submitted_by"]))):
            raise ValueError("Graph is no longer available to this submitter; submission remains queued")
        if graph_id == self.graph_id:
            token = self.token_file.read_text().strip() if self.token_file and self.token_file.exists() else ""
            key = json.loads(self.encryption_file.read_text()) if self.encryption_file and self.encryption_file.exists() and self.encryption_file.stat().st_size else None
        else:
            graph = next((g for g in self.extra_graphs if g["id"] == graph_id), None)
            if not graph:
                raise ValueError("Selected graph is no longer configured; submission remains queued")
            token, key = graph["token"], {"graph_id": graph_id, "key": graph["key"]}
        if key is not None and (not isinstance(key, dict) or "key" not in key or key.get("graph_id") != graph_id
                                or (key.get("key") is not None and (not isinstance(key["key"], str)
                                    or len(base64.b64decode(key["key"], validate=True)) != 32))):
            raise ValueError("Graph encryption key does not match configuration")
        return graph_id, token, key

    def encode_texts(self, base, headers, key, texts):
        if key["key"] is None:
            return texts
        response = request_json(base + "/e2ee/encrypt", {"key": key["key"], "texts": texts}, headers)
        encoded = response.get("texts") if isinstance(response, dict) else None
        if (not isinstance(encoded, list) or len(encoded) != len(texts)
                or not all(isinstance(t, str) and t for t in encoded)):
            raise ValueError("Invalid encryption response")
        return encoded

    def parse_checklist(self, text, user_id):
        header, _, body = text.partition("\n")
        try:
            args = shlex.split(header)
        except ValueError:
            args = []
        items = [line.strip() for line in body.splitlines() if line.strip()]
        if not 1 <= len(args) <= 2 or args[0] != "/checklist" or not 1 <= len(items) <= 100:
            raise ValueError('Usage: /checklist ["Graph name"] followed by 1–100 items, one per line.')
        graph = self.find_graph(args[1] if len(args) == 2 else self.selected_graph(user_id), user_id)
        if not graph:
            raise ValueError("Graph not configured for the bot. Use /select_graph to choose an available graph.")
        if not graph["enabled"]:
            raise ValueError("Graph found, but bot setup is required: a scoped token, plus an encryption key for encrypted graphs.")
        return graph, items

    def checklist_draft(self, message, user_id):
        text = message.get("text", "")
        command = text.split(maxsplit=1)[:1]
        row = self.db.execute("SELECT draft FROM checklist_drafts WHERE user_id=?", (user_id,)).fetchone()
        starting = command == ["/checklist"] and "\n" not in text.rstrip()
        if not row and not starting and command not in (["/done"], ["/cancel"]):
            return None, None
        if command == ["/cancel"] and text.strip() == "/cancel":
            self.db.execute("DELETE FROM checklist_drafts WHERE user_id=?", (user_id,))
            return None, "Checklist draft discarded." if row else "No active checklist. Start one with /checklist [name]."
        if not row:
            if not starting:
                return None, "No active checklist. Start one with /checklist [name]."
            if any(message.get(k) for k in ("photo", "video", "document", "animation", "audio", "voice", "sticker", "video_note")):
                raise ValueError("Start /checklist as a text message without an attachment.")
            title = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""
            if len(title) > 160:
                raise ValueError("Checklist names can contain up to 160 characters.")
            graph = self.find_graph(self.selected_graph(user_id), user_id)
            if not graph or not graph["enabled"]:
                raise ValueError("Selected graph is unavailable. Use /select_graph to choose a ready graph.")
            local = datetime.fromtimestamp(message["date"], self.zone)
            draft = {"graph_id": graph["id"], "items": [], "date": message["date"],
                     "title": title or "Checklist · " + local.strftime("%H:%M")}
            reply = f"Checklist started: {draft['title']}\nGraph: {graph['name']}\nSend items in separate messages or one per line. /done saves; /cancel discards."
        else:
            draft = json.loads(row[0])
            if text.strip() == "/done":
                if not draft["items"]:
                    raise ValueError("The checklist is empty. Send items first, or /cancel.")
                return draft, None
            if command and command[0].startswith("/"):
                raise ValueError("A checklist is already active. Send items, /done to save, or /cancel to discard.")
            if not text or any(message.get(k) for k in ("photo", "video", "document", "animation", "audio", "voice", "sticker", "video_note")):
                raise ValueError("Send checklist items as text. /done saves; /cancel discards.")
            items = [line.strip() for line in text.splitlines() if line.strip()]
            if not items or len(draft["items"]) + len(items) > 100:
                raise ValueError("A checklist accepts 1–100 nonblank items. /done saves the current items.")
            draft["items"].extend(items)
            reply = f"Added {len(items)} item(s); {len(draft['items'])} total. /done saves; /cancel discards."
        self.db.execute("INSERT INTO checklist_drafts VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET draft=excluded.draft",
                        (user_id, json.dumps(draft)))
        return None, reply

    def cooking_input(self, message, user_id):
        text = message.get("text", message.get("caption", ""))
        command = text.split(maxsplit=1)[:1]
        pending = self.db.execute("SELECT 1 FROM cooking_requests WHERE user_id=?", (user_id,)).fetchone()
        attachment = message.get("video") or message.get("document") or message.get("animation")
        prompt = "Send your cooking video link or attach a video. Use /cancel to cancel."
        if pending and text.strip() == "/cancel":
            self.db.execute("DELETE FROM cooking_requests WHERE user_id=?", (user_id,))
            return False, "Cooking request cancelled."
        if command == ["/cook"]:
            if self.db.execute("SELECT 1 FROM checklist_drafts WHERE user_id=?", (user_id,)).fetchone():
                return False, "A checklist is active. Use /done to save it or /cancel before starting a cooking request."
            if text.strip() == "/cook" and not attachment:
                self.db.execute("INSERT OR IGNORE INTO cooking_requests VALUES (?)", (user_id,))
                return False, prompt
            return True, None
        if pending:
            if command and command[0].startswith("/"):
                return False, prompt
            urls = re.findall(r"https?://[^\s<>]+", text)
            if not (any(description_url(url) for url in urls)
                    or attachment and attachment.get("mime_type") in ("video/mp4", "video/webm")):
                return False, prompt
            return True, None
        return False, None

    def sync_accounts(self, text, sender_id):
        if sender_id != self.admin_id or not self.allowlist_token or not self.api_url:
            return "Only the configured bot owner can manage sync accounts."
        parts = text.strip().split()
        if parts == ["/sync", "list"]:
            payload, method = None, "GET"
        elif len(parts) == 3 and parts[1] in ("allow", "remove"):
            email = parts[2].lower()
            if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                return "Invalid email address."
            payload, method = {"email": email}, "PUT" if parts[1] == "allow" else "DELETE"
        else:
            return "Usage: /sync list | /sync allow <email> | /sync remove <email>"
        try:
            result = request_json(self.api_url + "/training/allowlist", payload,
                                  {"Authorization": "Bearer " + self.allowlist_token}, method=method)
        except HTTPError as error:
            status = error.code
            error.close()
            return "The owner account cannot be changed." if status == 400 else "Sync account update failed; retry later."
        except (URLError, TimeoutError, OSError, ValueError):
            return "Sync account update failed; retry later."
        if method == "GET":
            return "Allowed sync accounts:\n" + "\n".join([result["owner"] + " (owner)", *result["emails"]])
        return ("Sync allowed for " + email + ". Graph access must be granted separately." if method == "PUT"
                else "Sync access removed for " + email + ". Existing sync sessions closed; downloaded copies remain on their devices.")

    def approve_analysis(self, text, user_id=None):
        match = re.fullmatch(r"/analyze\s+(\d+)", text.strip())
        if not match:
            return "Usage: /analyze <reference>"
        row = self.db.execute("SELECT record FROM submissions WHERE id=?", (int(match[1]),)).fetchone()
        if not row:
            return "Unknown reference."
        record = json.loads(row[0])
        if self.registered_user(user_id) and record.get("submitted_by") != user_id:
            return "Reference not available to your account."
        graph_id = record.get("graph_id") or record.get("encrypted_payload", {}).get("graph_id") or self.graph_id
        if self.logseq_users and (user_id is None or not self.find_graph(graph_id, user_id)):
            return "Reference not available to your Logseq account."
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

    def commands(self, user_id):
        commands = [
            {"command": "help", "description": "Show available commands and examples"},
            {"command": "select_graph", "description": "Show the current graph and select one using buttons"},
            {"command": "checklist", "description": "Start a checklist: /checklist [name]"},
            {"command": "done", "description": "Save the active checklist"},
            {"command": "cancel", "description": "Cancel the cooking request or checklist draft"},
            {"command": "analyze", "description": "Approve queued video analysis: /analyze reference"},
            {"command": "cook", "description": "Extract a recipe: select, then send a video or link"},
        ]
        if user_id == self.admin_id:
            commands.append({"command": "users", "description": "Review and approve bot access requests"})
            commands.append({"command": "search", "description": "Find indexed images: /search cat in factory overalls"})
            commands.append({"command": "sync", "description": "Manage sync accounts (owner only): /sync list"})
        return commands

    def help_text(self, user_id):
        text = "Available commands:\n" + "\n".join(f"/{c['command']} — {c['description']}" for c in self.commands(user_id))
        text += ('\n\nExample checklist:\n/checklist "Graph name"\nBuy oats\nPrepare workout'
                 '\n\nOmit the graph name to use your selected graph. Or send /checklist [name] alone, then items in separate messages, then /done to save. /cancel discards the draft. Select a graph before starting. Other commands remain available while drafting. You can also send a video link or plain note outside checklist mode.')
        text += "\n\nCooking: select /cook, then send a cooking video link or attachment. /cancel exits. Or send /cook https://youtu.be/example directly, or attach a video with /cook in its caption. Extracts ingredients and detailed steps; missing quantities are estimated and labeled when possible. Requires AI analysis to be enabled."
        if user_id == self.admin_id:
            text += "\n\nAccount management:\n/sync list\n/sync allow person@example.com\n/sync remove person@example.com"
        return text

    def configure_commands(self):
        if not self.bot_token or not self.allowed:
            return 0
        # Keep menus scoped to allowed private chats; permissions remain enforced by handlers.
        for scope in ("default", "all_private_chats"):
            self.telegram("setMyCommands", {"scope": {"type": scope}, "commands": []})
        for user_id in sorted(self.allowed):
            self.telegram("setMyCommands", {"scope": {"type": "chat", "chat_id": user_id},
                                            "commands": self.commands(user_id)})
            self.telegram("setChatMenuButton", {"chat_id": user_id, "menu_button": {"type": "commands"}})
        return len(self.allowed)

    def remember(self, updates):
        searched = False
        with self.db:
            for update in updates:
                update_id = update["update_id"]
                if update_id < self.db.execute("SELECT offset FROM state WHERE id=1").fetchone()[0]:
                    continue
                if "callback_query" in update:
                    callback = update["callback_query"]
                    if str(callback.get("data", "")).startswith("user:"):
                        self.registration_callback(callback)
                    elif str(callback.get("data", "")).startswith("graph:"):
                        self.graph_callback(callback)
                    else:
                        self.image_callback(callback)
                    self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))
                    continue
                message = update.get("message", {})
                sender = message.get("from", {})
                if (message.get("chat", {}).get("type") == "private" and message["chat"].get("id") == sender.get("id")
                        and not sender.get("is_bot") and sender.get("id") not in self.allowed
                        and message.get("text", "").split(maxsplit=1)[:1] == ["/start"]):
                    reply = self.request_registration(sender)
                    if self.bot_token:
                        self.telegram("sendMessage", {"chat_id": sender["id"], "text": reply})
                    self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))
                    continue
                if sender.get("id") in self.allowed and message.get("chat", {}).get("type") == "private":
                    text = message.get("text", message.get("caption", ""))
                    command = text.split(maxsplit=1)[:1]
                    if command == ["/users"]:
                        if self.bot_token:
                            payload = (self.registration_panel() if sender["id"] == self.admin_id
                                       else {"chat_id": sender["id"], "text": "Only the bot owner can review access requests."})
                            self.telegram("sendMessage", payload)
                        self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))
                        continue
                    if command == ["/search"]:
                        # Bound embedding I/O per polling pass; leave later updates unacknowledged.
                        if sender["id"] == self.admin_id and self.image_search_enabled:
                            if searched:
                                break
                            searched = True
                        with self.activity(message["chat"]["id"], message.get("message_id")):
                            reply = self.search_images(text, sender["id"])
                            if self.bot_token:
                                payload = {"chat_id": message["chat"]["id"], "text": reply,
                                           "link_preview_options": {"is_disabled": True}}
                                if getattr(self, "image_keyboard", None):
                                    payload["reply_markup"] = self.image_keyboard
                                self.telegram("sendMessage", payload)
                        self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))
                        continue
                    if command in (["/help"], ["/start"]):
                        if self.bot_token:
                            self.telegram("sendMessage", {"chat_id": message["chat"]["id"], "text": self.help_text(sender["id"])})
                        self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))
                        continue
                    if command in (["/select_graph"], ["/select-graph"], ["/graph"], ["/graphs"]):
                        with self.activity(message["chat"]["id"], message.get("message_id")):
                            reply = self.graph_command(text, sender["id"])
                        if self.bot_token:
                            payload = {"chat_id": message["chat"]["id"], "text": reply}
                            if self.graph_keyboard is not None:
                                payload["reply_markup"] = self.graph_keyboard
                            self.telegram("sendMessage", payload)
                        self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))
                        continue
                    if text.split(maxsplit=1)[:1] == ["/sync"]:
                        with self.activity(message["chat"]["id"], message.get("message_id")):
                            reply = self.sync_accounts(text, sender["id"])
                        if self.bot_token:
                            self.telegram("sendMessage", {"chat_id": message["chat"]["id"], "text": reply})
                        self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))
                        continue
                    if text.split(maxsplit=1)[:1] == ["/analyze"]:
                        reply = self.approve_analysis(text, sender["id"])
                        if self.bot_token:
                            self.telegram("sendMessage", {"chat_id": message["chat"]["id"], "text": reply})
                        self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))
                        continue
                    attachment = message.get("video") or message.get("document") or message.get("animation")
                    checklist = command == ["/checklist"]
                    if self.logseq_users and str(sender["id"]) not in self.logseq_users and not self.registered_user(sender["id"]):
                        if self.bot_token:
                            self.telegram("sendMessage", {"chat_id": message["chat"]["id"],
                                                          "text": "Your Telegram account is not linked to Logseq. Ask the bot owner to configure it."})
                        self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))
                        continue
                    cooking, reply = self.cooking_input(message, sender["id"])
                    if reply is not None:
                        if self.bot_token:
                            self.telegram("sendMessage", {"chat_id": message["chat"]["id"], "text": reply})
                        self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))
                        continue
                    try:
                        with self.activity(message["chat"]["id"], message.get("message_id")) if checklist and self.logseq_users else nullcontext():
                            draft, reply = self.checklist_draft(message, sender["id"])
                    except ValueError as error:
                        draft, reply = None, str(error)
                    if reply is not None:
                        if reply and self.bot_token:
                            self.telegram("sendMessage", {"chat_id": sender["id"], "text": reply})
                        self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))
                        continue
                    checklist = checklist or draft is not None
                    graph_id = self.selected_graph(sender["id"])
                    with self.activity(message["chat"]["id"], message.get("message_id")) if self.logseq_users else nullcontext():
                        graph = None if checklist else self.find_graph(graph_id, sender["id"])
                    if not checklist and self.registered_user(sender["id"]) and not graph:
                        if self.bot_token:
                            self.telegram("sendMessage", {"chat_id": sender["id"], "text": "No approved graph is selected. Use /select_graph or ask the owner to grant posting access."})
                        self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))
                        continue
                    items = None
                    if checklist:
                        try:
                            if attachment:
                                raise ValueError("Send /checklist as a text message without an attachment.")
                            with self.activity(message["chat"]["id"], message.get("message_id")) if self.logseq_users else nullcontext():
                                if draft:
                                    graph = self.find_graph(draft["graph_id"], sender["id"])
                                    if not graph or not graph["enabled"]:
                                        raise ValueError("This checklist's graph is no longer available. Draft retained; ask the owner to restore access, or /cancel.")
                                    items = draft["items"]
                                else:
                                    graph, items = self.parse_checklist(text, sender["id"])
                            graph_id = graph["id"]
                        except ValueError as error:
                            if self.bot_token:
                                self.telegram("sendMessage", {"chat_id": message["chat"]["id"], "text": str(error)})
                            self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))
                            continue
                    if text or attachment:
                        record = {
                            "id": update_id, "chat_id": message["chat"]["id"],
                            "message_id": message["message_id"], "submitted_by": sender["id"],
                            "created_at": datetime.fromtimestamp(message["date"], timezone.utc).isoformat(),
                            "original_text": text, "source_urls": re.findall(r"https?://[^\s<>]+", text),
                            "attachment": attachment, "media_status": "pending" if attachment else "none",
                            "analysis": {"status": "pending", "transcript": None, "exercises": []},
                            "publish_status": "pending", "acknowledged": False,
                            "graph_id": graph_id, "graph_name": graph["name"] if graph else graph_id,
                        }
                        if cooking:
                            record["analysis"]["type"] = "cooking"
                        if checklist:
                            local = datetime.fromtimestamp(draft["date"] if draft else message["date"], self.zone)
                            record.update(kind="checklist", items=items, journal_day=int(local.strftime("%Y%m%d")),
                                          journal_date=local.date().isoformat(), checklist_title=draft["title"] if draft else "Checklist · " + local.strftime("%H:%M"),
                                          source_urls=[], analysis={"status": "not_requested", "transcript": None, "exercises": []})
                        self.db.execute("INSERT OR IGNORE INTO submissions (id, record) VALUES (?, ?)", (update_id, json.dumps(record)))
                        if cooking:
                            self.db.execute("DELETE FROM cooking_requests WHERE user_id=?", (sender["id"],))
                        if draft:
                            self.db.execute("DELETE FROM checklist_drafts WHERE user_id=?", (sender["id"],))
                self.db.execute("UPDATE state SET offset=MAX(offset, ?) WHERE id=1", (update_id + 1,))

    def pending(self):
        if self.bot_token and self.allowed:
            offset = self.db.execute("SELECT offset FROM state WHERE id=1").fetchone()[0]
            self.remember(self.telegram("getUpdates", {"offset": offset, "timeout": 0, "limit": 20,
                                                       "allowed_updates": ["message", "callback_query"]}))
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
        if record.get("kind") == "checklist":
            return self.publish_checklist(record)
        graph_id, token, key = self.graph_credentials(record)
        if not graph_id or not token:
            record["publish_status"] = "waiting_for_logseq"
            return
        if key is None:
            record["publish_status"] = "waiting_for_logseq_key"
            return
        base = f"{self.api_url}/api/v1/graphs/{quote(graph_id, safe='')}"
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
            encrypted = self.encode_texts(base, headers, key, [title, title.lower(), *lines])
            record["encrypted_payload"] = {
                "graph_id": graph_id, "page": {"title": encrypted[0], "name": encrypted[1]},
                "label": title,
                "blocks": [{"uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"training-hub:{record['id']}:{index}")), "title": text}
                           for index, text in enumerate(encrypted[2:])],
            }
            # Persist randomized ciphertext before writing, so page creation can be retried safely.
            self.save(record)
        prepared = record["encrypted_payload"]
        if prepared["graph_id"] != graph_id:
            raise ValueError("Prepared submission belongs to another graph")
        if not record.get("page_id"):
            page = request_json(base + "/pages", prepared["page"], headers)
            record["page_id"] = page["uuid"]
            record["page_title"] = prepared.get("label", f"Training inbox - {record['id']}")
            self.save(record)
        request_json(base + f"/blocks/{quote(record['page_id'], safe='')}/children", {"position": "append", "blocks": prepared["blocks"]}, headers)
        record["publish_status"] = "published"

    def publish_checklist(self, record):
        graph_id, token, key = self.graph_credentials(record)
        if not token or key is None:
            record["publish_status"] = "waiting_for_logseq" if not token else "waiting_for_logseq_key"
            return
        if not request_json(self.api_url + "/health").get("boolean-checklists"):
            raise ValueError("Sync worker must be upgraded for boolean checklists")
        base = f"{self.api_url}/api/v1/graphs/{quote(graph_id, safe='')}"
        headers = {"Authorization": "Bearer " + token}
        if not record.get("encrypted_payload"):
            plain = [record["checklist_title"], *record["items"], "Checked", "checked"]
            texts = self.encode_texts(base, headers, key, plain)
            identity = lambda index: str(uuid.uuid5(uuid.NAMESPACE_URL, f"training-hub:{record['id']}:checklist:{index}"))
            record["encrypted_payload"] = {"graph_id": graph_id, "journal-day": record["journal_day"],
                                           "checkbox-property": {"title": texts[-2], "name": texts[-1]},
                                           "blocks": [{"uuid": identity(0), "title": texts[0],
                                                       "children": [{"uuid": identity(i), "title": title, "checkbox": True}
                                                                    for i, title in enumerate(texts[1:-2], 1)]}]}
            self.save(record)
        prepared = record["encrypted_payload"]
        if prepared["graph_id"] != graph_id or prepared["journal-day"] != record["journal_day"]:
            raise ValueError("Prepared checklist belongs to another graph or date")
        payload = {k: v for k, v in prepared.items() if k != "graph_id"}
        response = request_json(base + "/capture", payload, headers)
        record["page_id"] = response["page-id"]
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
        graph_id, token, key = self.graph_credentials(record)
        if not token or key is None:
            raise ValueError("Selected graph credentials are missing")
        if record["encrypted_payload"]["graph_id"] != graph_id:
            raise ValueError("Prepared submission belongs to another graph")
        base = f"{self.api_url}/api/v1/graphs/{quote(graph_id, safe='')}"
        headers = {"Authorization": "Bearer " + token}
        if record.get("page_rename", {}).get("label") != title:
            if key.get("graph_id") != graph_id:
                raise ValueError("Graph encryption key does not match configuration")
            texts = self.encode_texts(base, headers, key, [title, title.lower()])
            record["page_rename"] = {"label": title, "page": {"title": texts[0], "name": texts[1]}}
            self.save(record)
        request_json(base + f"/pages/{quote(record['page_id'], safe='')}", record["page_rename"]["page"], headers, method="PATCH")
        record["page_title"] = title
        record.pop("page_rename", None)

    def publish_analysis(self, record):
        analysis = record["analysis"]
        if analysis.get("published") or analysis["status"] != "completed":
            return
        graph_id, token, key = self.graph_credentials(record)
        if not token or key is None:
            raise ValueError("Selected graph credentials are missing")
        if record["encrypted_payload"]["graph_id"] != graph_id:
            raise ValueError("Prepared submission belongs to another graph")
        base = f"{self.api_url}/api/v1/graphs/{quote(graph_id, safe='')}"
        headers = {"Authorization": f"Bearer {token}"}
        if not analysis.get("encrypted_title"):
            if key.get("graph_id") != graph_id or record["encrypted_payload"]["graph_id"] != graph_id:
                raise ValueError("Graph encryption key does not match configuration")
            texts = self.encode_texts(base, headers, key, [render_analysis(analysis)])
            analysis["encrypted_title"] = texts[0]
            self.save(record)
        block = {"uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"training-hub:{record['id']}:analysis:v1")), "title": analysis["encrypted_title"]}
        request_json(base + f"/blocks/{quote(record['page_id'], safe='')}/children", {"position": "append", "blocks": [block]}, headers)
        analysis["published"] = True

    def analyze(self, record):
        _, token, key = self.graph_credentials(record)
        if not token or key is None:
            raise ValueError("Selected graph credentials are missing")
        if record["analysis"]["status"] in RETRY_STATES:
            record["analysis"] = self.ai.step(record)
            self.save(record)
        self.publish_analysis(record)
        self.rename_page(record)
        self.publish_journal_link(record)
        analysis = record["analysis"]
        if analysis["status"] in NOTICE_STATES and not analysis.get("notified"):
            if self.bot_token:
                self.telegram("sendMessage", {"chat_id": record["chat_id"], "text": f"{analysis['reason']} Reference: {record['id']}"})
            analysis["notified"] = True

    def needs_journal_link(self, record):
        return (record["analysis"]["status"] == "completed" and record["analysis"].get("published")
                and (any(description_url(url) for url in record.get("source_urls", []))
                     or (record.get("attachment") or {}).get("mime_type") in ("video/mp4", "video/webm")))

    def publish_journal_link(self, record):
        if not self.needs_journal_link(record) or record.get("journal_link", {}).get("published"):
            return
        graph_id, token, key = self.graph_credentials(record)
        if not token or key is None or record["encrypted_payload"]["graph_id"] != graph_id:
            raise ValueError("Selected graph credentials are missing or do not match the prepared submission")
        base = f"{self.api_url}/api/v1/graphs/{quote(graph_id, safe='')}"
        headers = {"Authorization": "Bearer " + token}
        if not record.get("journal_link"):
            day = int(datetime.now(self.zone).strftime("%Y%m%d"))
            title = self.encode_texts(base, headers, key, [f"[[{record['page_id']}]]"])[0]
            record["journal_link"] = {"graph_id": graph_id, "journal-day": day,
                                      "blocks": [{"uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"training-hub:{record['id']}:journal:v1")),
                                                  "title": title}]}
            self.save(record)
        prepared = record["journal_link"]
        if prepared["graph_id"] != graph_id:
            raise ValueError("Prepared journal link belongs to another graph")
        response = request_json(base + "/capture", {"journal-day": prepared["journal-day"], "blocks": prepared["blocks"]}, headers)
        prepared.update(published=True, page_id=response["page-id"])

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
        with self.activity(record["chat_id"], record["message_id"]):
            return self._capture(identifier, record, was_published)

    def _capture(self, identifier, record, was_published):
        with self.db:
            self.db.execute("UPDATE submissions SET attempted=unixepoch() WHERE id=?", (identifier,))
        # Save before external calls so outages cannot discard a submission.
        self.save(record)
        try:
            if record.get("kind") != "checklist":
                self.download(record)
                self.describe(record)
            self.publish(record)
            if was_published and self.ai.enabled and record.get("kind") != "checklist":
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
            if record.get("kind") == "checklist":
                destination = f"{record['graph_name']} / journal {record['journal_date']}"
                status = (f"Checklist saved: {len(record['items'])} items in {destination}." if record["publish_status"] == "published"
                          else f"Checklist saved in the inbox; delivery to {destination} is pending.")
                ai_status = ""
            self.telegram("sendMessage", {"chat_id": record["chat_id"], "text": f"{status} {ai_status} Reference: {identifier}"})
            record["acknowledged"] = True
        done = record["publish_status"] == "published" and record["media_status"] != "pending" and record["acknowledged"]
        if self.ai.enabled and record.get("kind") != "checklist":
            done = done and record["analysis"]["status"] not in RETRY_STATES
            done = done and record.get("page_title") == self.page_title(record)
            if record["analysis"]["status"] == "completed":
                done = done and record["analysis"].get("published", False)
                if self.needs_journal_link(record):
                    done = done and record.get("journal_link", {}).get("published", False)
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
                         os.environ.get("GEMINI_VIDEO_MODEL", "gemini-3.5-flash-lite"),
                         os.environ.get("TELEGRAM_ADMIN_USER_ID", ""), os.environ.get("TRAINING_ALLOWLIST_TOKEN", ""),
                         os.environ.get("LOGSEQ_GRAPH_NAME", ""), os.environ.get("LOGSEQ_GRAPHS_FILE", ""),
                         os.environ.get("TELEGRAM_LOGSEQ_USERS", ""),
                         os.environ.get("IMAGE_SEARCH_ENABLED", "false").lower() == "true")
    print("Training inbox listening; AI " + ("enabled." if server.inbox.ai.enabled else "disabled."))
    server.serve_forever()
