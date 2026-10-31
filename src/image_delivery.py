"""Deliver owner-requested indexed images without persisting their bytes."""
import asyncio
import json
import os
import secrets
from urllib.request import Request, urlopen

if __package__:
    from .activity import telegram_activity
    from .image_search import ImageIndex, image_mime
    from .import_images import ImageBuffer, find_chat
    from .inbox import request_json
else:
    from activity import telegram_activity
    from image_search import ImageIndex, image_mime
    from import_images import ImageBuffer, find_chat
    from inbox import request_json


def send_image(bot_token, owner_id, data, caption, document=False):
    mime = image_mime(data)
    boundary = secrets.token_hex(24)
    parts = []
    for name, value in (("chat_id", str(owner_id)), ("caption", caption[:1000])):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    field = "document" if document else "photo"
    extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="image.{extension}"\r\n'
                 f'Content-Type: {mime}\r\n\r\n'.encode() + data + f'\r\n--{boundary}--\r\n'.encode())
    request = Request(f"https://api.telegram.org/bot{bot_token}/" + ("sendDocument" if document else "sendPhoto"),
                      data=b"".join(parts), headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urlopen(request, timeout=30) as response:
        raw = response.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024 or not json.loads(raw).get("ok"):
        raise ValueError("Telegram image delivery failed")


async def fetch_image(client, job):
    entity = await find_chat(client, job["chat_id"])
    if getattr(entity, "noforwards", False):
        raise ValueError("Source chat is protected")
    message = await client.get_messages(entity, ids=job["message_id"])
    if (not message or getattr(message, "sticker", None) or getattr(message, "noforwards", False) or getattr(message, "ttl_period", None)
            or getattr(getattr(message, "media", None), "ttl_seconds", None)):
        raise ValueError("Source image is unavailable or protected")
    document = message.document
    if not message.photo and not (document and document.mime_type in ("image/jpeg", "image/png", "image/webp")):
        raise ValueError("Source message no longer contains a supported image")
    with ImageBuffer() as buffer:
        await message.download_media(file=buffer)
        data = buffer.getvalue()
    image_mime(data)
    title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or "Image"
    caption = f"{' '.join(title.split())[:200]} · message {job['message_id']}"
    return data, caption, bool(document)


async def deliver_image(client, index, job, bot_token):
    try:
        def notify(method, payload):
            return request_json(f"https://api.telegram.org/bot{bot_token}/{method}", payload)
        with telegram_activity(notify, job["owner_id"], action="upload_photo"):
            data, caption, document = await fetch_image(client, job)
            await asyncio.to_thread(send_image, bot_token, job["owner_id"], data, caption, document)
    except Exception:
        index.finish_image(job["token"], "failed")
        raise
    index.finish_image(job["token"], "done")


async def main():
    from telethon import TelegramClient
    from telethon.sessions import SQLiteSession, StringSession

    owner = int(os.environ.get("TELEGRAM_ADMIN_USER_ID", "0"))
    allowed = {int(v) for v in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if v.strip()}
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if os.environ.get("IMAGE_SEARCH_ENABLED") != "true" or not owner or owner not in allowed or not token:
        raise ValueError("Owner-only image search must be configured and enabled")
    index = ImageIndex(os.environ["DATA_DIR"])
    # Keep the reader in memory so its long-lived connection cannot lock the importer's session file.
    source_session = SQLiteSession(os.environ["TELEGRAM_SESSION_FILE"])
    try:
        session = StringSession(StringSession.save(source_session))
    finally:
        source_session.close()
    client = TelegramClient(session, int(os.environ["TELEGRAM_API_ID"]),
                            os.environ["TELEGRAM_API_HASH"], receive_updates=False,
                            request_retries=0, flood_sleep_threshold=0)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise ValueError("Complete account login before starting image delivery")
        me = await client.get_me()
        if me.bot or me.id != owner:
            raise ValueError("Account session belongs to a different owner")
        index.recover_images(owner)
        print("Owner-only image delivery ready.", flush=True)
        while True:
            job = index.claim_image(owner)
            if job:
                try:
                    await deliver_image(client, index, job, token)
                    print("Image delivered.", flush=True)
                except Exception as error:
                    print(f"Image delivery failed ({type(error).__name__}).", flush=True)
                    try:
                        await asyncio.to_thread(request_json, f"https://api.telegram.org/bot{token}/sendMessage",
                                                {"chat_id": owner, "text": "Couldn’t open that image. It may be unavailable; tap Show image to retry."})
                    except Exception:
                        pass
            await asyncio.sleep(1)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    os.umask(0o077)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        raise SystemExit(f"Image delivery stopped ({type(error).__name__}); check account session and configuration.") from None
