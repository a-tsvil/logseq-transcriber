"""Register Telegram's command menus without sending chat messages or polling updates."""
import os
import tempfile
from urllib.error import HTTPError, URLError

from inbox import Inbox


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as data:
        inbox = Inbox(os.environ.get("DATA_DIR", data), os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""),
                      bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                      admin_id=os.environ.get("TELEGRAM_ADMIN_USER_ID", ""))
        try:
            count = inbox.configure_commands()
            print(f"Telegram command menus configured for {count} allowed private chats." if count
                  else "Command menu setup skipped: bot token or allowed users are not configured.")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            raise SystemExit(f"Telegram menu setup failed: {type(error).__name__} (HTTP {getattr(error, 'code', 'n/a')}).") from None
        finally:
            inbox.db.close()
