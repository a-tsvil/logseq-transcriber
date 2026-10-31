"""Explicit, bounded image import using the owner's Telegram user account."""
import argparse
import asyncio
import getpass
import io
import os
from pathlib import Path

if __package__:
    from .image_search import ImageIndex, MAX_IMAGE_BYTES, image_mime
else:
    from image_search import ImageIndex, MAX_IMAGE_BYTES, image_mime


class ImageBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds 10 MiB")
        return super().write(data)


async def login_account(client, use_qr=False):
    if await client.is_user_authorized():
        return
    if not use_qr:
        await client.start()
        return
    import qrcode
    from telethon.errors import SessionPasswordNeededError

    login = await client.qr_login()
    qr = qrcode.QRCode(border=4)
    qr.add_data(login.url)
    qr.make(fit=True)
    print("In Telegram on your phone: Settings > Devices > Link Desktop Device. Scan this QR code.", flush=True)
    qr.print_ascii(invert=True)
    try:
        await login.wait()
    except SessionPasswordNeededError:
        await client.sign_in(password=getpass.getpass("Telegram two-step verification password: "))
    except asyncio.TimeoutError:
        print("QR code expired. Rerun with --qr-login to generate a new code.", flush=True)
        raise


async def find_chat(client, chat_id):
    # A fresh session needs dialog access hashes before numeric private chat IDs can resolve.
    entity = None
    async for dialog in client.iter_dialogs():
        if dialog.id == chat_id:
            entity = dialog.entity
            break
    if entity is None:
        raise ValueError("Selected chat is not in this account's dialogs")
    return entity


async def import_chat(client, index, owner_id, chat_id, max_images=20, before=0, after=0, retry_failed=False):
    me = await client.get_me()
    if me.bot or me.id != owner_id:
        raise ValueError("Log in with the configured bot owner's user account")
    entity = await find_chat(client, chat_id)
    if getattr(entity, "noforwards", False):
        raise ValueError("Importing protected chats is not supported")
    title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(chat_id)
    counts = {"indexed": 0, "skipped": 0, "needs_review": 0}
    # shortcut: batches do not reconcile source deletions, add reconciliation before automatic syncing.
    async for message in client.iter_messages(entity, limit=None, max_id=before, min_id=after, wait_time=1):
        if getattr(message, "sticker", None):
            index.exclude_sticker(owner_id, chat_id, message.id)
            counts["skipped"] += 1
            continue
        if (getattr(message, "noforwards", False) or getattr(message, "ttl_period", None)
                or getattr(getattr(message, "media", None), "ttl_seconds", None)):
            counts["skipped"] += 1
            continue
        document = message.document
        photo = message.photo
        if not photo and not (document and document.mime_type in ("image/jpeg", "image/png", "image/webp")):
            continue
        if index.seen(owner_id, chat_id, message.id, retry_failed):
            continue
        if document and document.size > MAX_IMAGE_BYTES:
            counts["skipped"] += 1
            continue
        try:
            with ImageBuffer() as buffer:
                await message.download_media(file=buffer)
                data = buffer.getvalue()
            image_mime(data)
        except ValueError:
            counts["skipped"] += 1
            continue
        state = index.add(owner_id, chat_id, message.id, title, data, retry_failed)
        del data
        counts["indexed" if state == "ready" else "needs_review"] += 1
        print(f"Chat {chat_id}, message {message.id}: {state}", flush=True)
        if counts["indexed"] + counts["needs_review"] >= max_images:
            break
        # Pace paid calls rather than burst through provider per-minute limits.
        await asyncio.sleep(6)
    return counts


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--chat", type=int, help="One explicitly selected numeric Telegram chat ID")
    selection.add_argument("--list-chats", action="store_true", help="List account chat names and numeric IDs without indexing")
    parser.add_argument("--qr-login", action="store_true", help="Authorize from Telegram's Devices screen instead of requesting a code")
    parser.add_argument("--consent-confirmed", action="store_true",
                        help="Confirm all relevant users explicitly consent to AI indexing of this selected chat")
    parser.add_argument("--max-images", type=int, default=20, help="Maximum new image attempts in this run (default 20)")
    parser.add_argument("--before", type=int, default=0, help="Only messages before this ID (exclusive)")
    parser.add_argument("--after", type=int, default=0, help="Only messages after this ID (exclusive)")
    parser.add_argument("--retry-failed", action="store_true", help="Explicitly repeat failed/interrupted paid requests")
    args = parser.parse_args(argv)
    if args.chat is not None and not args.consent_confirmed:
        parser.error("--consent-confirmed is required for the specifically selected chat")
    if (args.chat == 0 or not 1 <= args.max_images <= 1000 or min(args.before, args.after) < 0
            or (args.before and args.after >= args.before)):
        parser.error("Use a nonzero chat ID, 1–1000 images, and a valid message ID range")
    return args


async def main(args):
    from telethon import TelegramClient

    owner_id = int(os.environ.get("TELEGRAM_ADMIN_USER_ID", "0"))
    allowed = {int(value) for value in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if value.strip()}
    if not owner_id or owner_id not in allowed:
        raise ValueError("Configure TELEGRAM_ADMIN_USER_ID and include it in TELEGRAM_ALLOWED_USER_IDS")
    api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    if api_id <= 0 or not api_hash:
        raise ValueError("Configure TELEGRAM_API_ID and TELEGRAM_API_HASH from my.telegram.org")
    index = None
    if not args.list_chats:
        data = Path(os.environ.get("DATA_DIR", "/data"))
        index = ImageIndex(data, os.environ.get("GEMINI_KEY_FILE", ""),
                           os.environ.get("GEMINI_TEXT_MODEL", "gemini-3.5-flash-lite"))
        index.key()
    session = Path(os.environ.get("TELEGRAM_SESSION_FILE", "/account-secrets/telegram-index.session"))
    if not session.parent.is_dir():
        raise ValueError("Mount the private account session directory before importing")
    client = TelegramClient(str(session), api_id, api_hash, request_retries=0, flood_sleep_threshold=0,
                            receive_updates=args.qr_login)
    await client.connect()
    try:
        await login_account(client, args.qr_login)
        if args.qr_login:
            await client.set_receive_updates(False)
        if args.list_chats:
            me = await client.get_me()
            if me.bot or me.id != owner_id:
                raise ValueError("Log in with the configured bot owner's user account")
            async for dialog in client.iter_dialogs():
                print(f"{dialog.id}\t{dialog.name}")
            return
        counts = await import_chat(client, index, owner_id, args.chat, args.max_images,
                                   args.before, args.after, args.retry_failed)
    finally:
        await client.disconnect()
    print(f"Done: {counts['indexed']} indexed references; {counts['skipped']} images skipped; "
          f"{counts['needs_review']} references need explicit review/retry.")


if __name__ == "__main__":
    os.umask(0o077)
    args = parse_args()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        raise SystemExit("Import interrupted. Completed images are retained; uncertain requests need --retry-failed.") from None
    except Exception as error:
        # Telegram/provider exceptions may contain credentials or private source content.
        status = f", HTTP {error.code}" if hasattr(error, "code") and type(error.code) is int else ""
        raise SystemExit(f"Import stopped ({type(error).__name__}{status}). Check configuration/access/quota. "
                         "Completed images are retained; failed requests need --retry-failed.") from None
