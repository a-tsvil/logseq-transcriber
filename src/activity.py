"""Best-effort Telegram activity feedback around slow operations."""
from contextlib import contextmanager
from threading import Event, Thread
from urllib.error import HTTPError, URLError


@contextmanager
def telegram_activity(send, chat_id, message_id=None, action="typing"):
    stopped = Event()

    def notify(method, payload):
        try:
            send(method, payload)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            pass

    def refresh():
        reaction = {"chat_id": chat_id, "message_id": message_id}
        if message_id is not None:
            notify("setMessageReaction", {**reaction, "reaction": [{"type": "emoji", "emoji": "👀"}]})
        try:
            while not stopped.is_set():
                notify("sendChatAction", {"chat_id": chat_id, "action": action})
                stopped.wait(4)
        finally:
            if message_id is not None:
                notify("setMessageReaction", {**reaction, "reaction": []})

    thread = Thread(target=refresh, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)
