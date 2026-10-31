import io
from contextlib import nullcontext
import json
import asyncio
import os
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError

from src.image_delivery import deliver_image, fetch_image, main, send_image
from src.image_search import DIMENSIONS, ImageIndex
from src.inbox import Inbox


class ImageButtonTests(unittest.TestCase):
    def setUp(self):
        activity = patch("src.inbox.telegram_activity", side_effect=lambda *args, **kwargs: nullcontext())
        self.activity = activity.start()
        self.addCleanup(activity.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = Path(self.temp.name) / "key"
        self.key.write_text("TEST_KEY")
        self.index = ImageIndex(self.temp.name, self.key)
        self.match = {"chat_id": -452019584, "message_id": 123, "chat_title": "Memes", "description": "Cat", "link": None}
        with patch("src.image_search.generate", return_value={"description": "Cat", "text": ""}), \
                patch("src.image_search.embed", return_value=[1] + [0] * (DIMENSIONS - 1)):
            self.index.add(101, self.match["chat_id"], self.match["message_id"], "Memes", b"\xff\xd8\xffcat")
        self.keyboard = self.index.keyboard(101, [self.match])
        self.token = self.keyboard["inline_keyboard"][0][0]["callback_data"][6:]

    def test_buttons_are_stable_opaque_and_fit_telegram_callback_limit(self):
        self.assertEqual(self.keyboard, self.index.keyboard(101, [self.match]))
        button = self.keyboard["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "Show image 1")
        self.assertLessEqual(len(button["callback_data"].encode()), 64)
        self.assertNotIn(str(self.match["chat_id"]), button["callback_data"])
        description = self.keyboard["inline_keyboard"][0][1]
        self.assertEqual(description["text"], "Description")
        self.assertEqual(description["callback_data"], "desc:" + self.token)
        self.assertIsNone(self.index.keyboard(101, []))

    def test_queue_requires_matching_owner_and_deduplicates_callback_replays(self):
        self.assertIn("unavailable", self.index.queue_image(self.token, 202, "one"))
        self.assertIsNone(self.index.claim_image(101))
        self.assertIn("Fetching", self.index.queue_image(self.token, 101, "one"))
        self.assertIn("already", self.index.queue_image(self.token, 101, "two"))
        self.assertIsNone(self.index.claim_image(202))
        job = self.index.claim_image(101)
        self.assertEqual((job["owner_id"], job["chat_id"], job["message_id"]), (101, -452019584, 123))
        self.assertIsNone(self.index.claim_image(101))
        self.index.finish_image(self.token, "done")
        self.index.queue_image(self.token, 101, "one")
        self.assertIsNone(self.index.claim_image(101))
        self.index.queue_image(self.token, 101, "three")
        self.assertIsNotNone(self.index.claim_image(101))

    def test_interrupted_delivery_needs_a_new_click_instead_of_automatic_resend(self):
        self.index.queue_image(self.token, 101, "one")
        self.index.claim_image(101)
        self.index.recover_images(101)
        self.assertIsNone(self.index.claim_image(101))
        self.index.queue_image(self.token, 101, "two")
        self.assertIsNotNone(self.index.claim_image(101))

    def test_worker_cannot_claim_messages_absent_from_the_owner_index(self):
        keyboard = self.index.keyboard(101, [{**self.match, "message_id": 999}])
        token = keyboard["inline_keyboard"][0][0]["callback_data"][6:]
        self.index.queue_image(token, 101, "fake")
        self.assertIsNone(self.index.claim_image(101))

    def callback(self, sender=101, chat=101, chat_type="private", data=None, identifier="callback"):
        return {"id": identifier, "from": {"id": sender}, "message": {"chat": {"id": chat, "type": chat_type}},
                "data": "image:" + self.token if data is None else data}

    def inbox(self):
        inbox = Inbox(self.temp.name, "101,202", admin_id="101", bot_token="TEST_BOT", image_search_enabled=True,
                      ai_key_file=str(self.key))
        self.addCleanup(inbox.db.close)
        return inbox

    def test_callbacks_are_owner_only_private_and_not_archived_as_notes(self):
        inbox = self.inbox()
        invalid = [self.callback(sender=202), self.callback(chat=202), self.callback(chat_type="group"),
                   self.callback(data="image:unknown"), self.callback(data="other:123")]
        with patch.object(inbox, "telegram"):
            inbox.remember([{"update_id": n, "callback_query": callback} for n, callback in enumerate(invalid, 1)])
        self.assertIsNone(self.index.claim_image(101))
        with patch.object(inbox, "telegram") as telegram:
            inbox.remember([{"update_id": 10, "callback_query": self.callback()}])
        self.assertIsNotNone(self.index.claim_image(101))
        self.assertEqual(telegram.call_args.args[0], "answerCallbackQuery")
        self.assertEqual(inbox.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.assertEqual(inbox.db.execute("SELECT offset FROM state").fetchone()[0], 11)

    def test_expired_callback_ack_does_not_block_queue_or_advance_replays_twice(self):
        inbox = self.inbox()
        error = HTTPError("test", 400, "Expired", {}, None)
        self.addCleanup(error.close)
        with patch.object(inbox, "telegram", side_effect=error):
            inbox.remember([{"update_id": 1, "callback_query": self.callback()}])
        self.assertIsNotNone(self.index.claim_image(101))
        self.assertEqual(inbox.db.execute("SELECT offset FROM state").fetchone()[0], 2)

    def test_search_attaches_real_buttons_and_disabling_search_rejects_existing_buttons(self):
        inbox = self.inbox()
        message = {"message_id": 1, "from": {"id": 101}, "chat": {"id": 101, "type": "private"}, "text": "/search cat"}
        with patch("src.image_search.embed", return_value=[1] + [0] * (DIMENSIONS - 1)), \
                patch("src.image_search.strict_matches", return_value=[0]), \
                patch.object(inbox, "telegram") as telegram:
            inbox.remember([{"update_id": 1, "message": message}])
        self.assertEqual(telegram.call_args.args[1]["reply_markup"], self.keyboard)
        inbox.image_search_enabled = False
        with patch.object(inbox, "telegram"):
            inbox.remember([{"update_id": 2, "callback_query": self.callback()}])
        self.assertIsNone(self.index.claim_image(101))

    def test_short_description_opens_owner_only_popup_without_fetching_image(self):
        inbox = self.inbox()
        with patch.object(inbox, "telegram") as telegram:
            inbox.image_callback(self.callback(data="desc:" + self.token))
        self.assertEqual(telegram.call_args.args, ("answerCallbackQuery", {"callback_query_id": "callback", "text": "Cat", "show_alert": True}))
        self.assertIsNone(self.index.claim_image(101))
        with patch.object(inbox, "telegram") as telegram:
            inbox.image_callback(self.callback(sender=202, chat=202, data="desc:" + self.token))
        self.assertNotIn("Cat", telegram.call_args.args[1]["text"])

    def test_long_description_is_full_text_on_demand_with_telegram_safe_chunks(self):
        text = "🐱" * 1800 + "\nText: " + "x" * 2400
        with self.index.connect() as db:
            db.execute("UPDATE image_assets SET description=?", (text,))
        inbox = self.inbox()
        with patch.object(inbox, "telegram") as telegram:
            inbox.image_callback(self.callback(data="desc:" + self.token))
        chunks = [call.args[1] for call in telegram.call_args_list if call.args[0] == "sendMessage"]
        self.assertEqual("".join(row["text"] for row in chunks), text)
        self.assertTrue(all(row["chat_id"] == 101 and len(row["text"].encode("utf-16-le")) // 2 <= 4096 for row in chunks))
        self.assertIsNone(self.index.claim_image(101))

    def test_excluded_sticker_description_is_not_disclosed(self):
        self.index.exclude_sticker(101, -452019584, 123)
        self.assertIsNone(self.index.image_description(self.token, 101))

    def test_search_enables_activity_on_the_original_message(self):
        inbox = self.inbox()
        message = {"message_id": 55, "from": {"id": 101}, "chat": {"id": 101, "type": "private"}, "text": "/search cat"}
        with patch.object(inbox, "search_images", return_value="No matching images"), patch.object(inbox, "telegram"):
            inbox.remember([{"update_id": 1, "message": message}])
        self.assertEqual(self.activity.call_args.args[1:3], (101, 55))


class ImageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        activity = patch("src.image_delivery.telegram_activity", side_effect=lambda *args, **kwargs: nullcontext())
        self.activity = activity.start()
        self.addCleanup(activity.stop)
        self.job = {"token": "TEST", "owner_id": 101, "chat_id": -452019584, "message_id": 123}
        self.entity = SimpleNamespace(title="Memes")
        async def dialogs():
            yield SimpleNamespace(id=-452019584, entity=self.entity)
        async def download(file):
            file.write(b"\xff\xd8\xffcat")
        self.message = SimpleNamespace(photo=True, document=None, download_media=AsyncMock(side_effect=download))
        self.client = SimpleNamespace(iter_dialogs=Mock(side_effect=dialogs), get_messages=AsyncMock(return_value=self.message))

    async def test_image_is_fetched_in_memory_and_sent_only_to_requesting_owner(self):
        index = Mock()
        with patch("src.image_delivery.send_image") as send:
            await deliver_image(self.client, index, self.job, "TEST_BOT")
        self.client.get_messages.assert_awaited_once_with(self.entity, ids=123)
        send.assert_called_once_with("TEST_BOT", 101, b"\xff\xd8\xffcat", "Memes · message 123", False)
        buffer = self.message.download_media.call_args.kwargs["file"]
        self.assertIsInstance(buffer, io.BytesIO)
        self.assertTrue(buffer.closed)
        index.finish_image.assert_called_once_with("TEST", "done")

    async def test_deleted_protected_or_expiring_messages_are_never_downloaded(self):
        self.client.get_messages.return_value = None
        with self.assertRaises(ValueError):
            await fetch_image(self.client, self.job)
        self.client.get_messages.return_value = self.message
        for attribute, value in (("sticker", True), ("noforwards", True), ("ttl_period", 30), ("media", SimpleNamespace(ttl_seconds=30))):
            setattr(self.message, attribute, value)
            with self.assertRaises(ValueError):
                await fetch_image(self.client, self.job)
            delattr(self.message, attribute)
        self.entity.noforwards = True
        with self.assertRaises(ValueError):
            await fetch_image(self.client, self.job)
        self.message.download_media.assert_not_awaited()

    async def test_uncertain_upload_is_failed_without_automatic_resend(self):
        index = Mock()
        with patch("src.image_delivery.send_image", side_effect=TimeoutError) as send:
            with self.assertRaises(TimeoutError):
                await deliver_image(self.client, index, self.job, "TEST_BOT")
        self.assertEqual(send.call_count, 1)
        index.finish_image.assert_called_once_with("TEST", "failed")

    def test_multipart_upload_uses_expected_private_destination_and_image_field(self):
        for document in (False, True):
            with patch("src.image_delivery.urlopen", return_value=io.BytesIO(b'{"ok":true}')) as request:
                send_image("TEST_BOT", 101, b"\xff\xd8\xffcat", "Memes", document=document)
            req = request.call_args.args[0]
            self.assertTrue(req.full_url.endswith("sendDocument" if document else "sendPhoto"))
            self.assertIn(b'name="chat_id"\r\n\r\n101\r\n', req.data)
            self.assertIn(b"\xff\xd8\xffcat", req.data)
            self.assertIn(b'name="document"' if document else b'name="photo"', req.data)

    async def test_worker_closes_disk_session_and_connects_using_only_in_memory_credentials(self):
        disk = Mock()
        memory = Mock()
        string_session = Mock(return_value=memory)
        string_session.save.return_value = "TEST_SESSION"
        sessions = SimpleNamespace(SQLiteSession=Mock(return_value=disk), StringSession=string_session)
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
                                 get_me=AsyncMock(return_value=SimpleNamespace(id=101, bot=False)))
        constructor = Mock(return_value=client)
        index = Mock()
        index.claim_image.return_value = None
        environment = {"TELEGRAM_ADMIN_USER_ID": "101", "TELEGRAM_ALLOWED_USER_IDS": "101", "TELEGRAM_BOT_TOKEN": "TEST_BOT",
                       "TELEGRAM_SESSION_FILE": "test.session", "TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH": "TEST_HASH",
                       "DATA_DIR": "/tmp", "IMAGE_SEARCH_ENABLED": "true"}
        modules = {"telethon": SimpleNamespace(TelegramClient=constructor, sessions=sessions), "telethon.sessions": sessions}
        with patch.dict(os.environ, environment), patch.dict(sys.modules, modules), \
                patch("src.image_delivery.ImageIndex", return_value=index), patch("builtins.print"), \
                patch("src.image_delivery.asyncio.sleep", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await main()
        disk.close.assert_called_once()
        string_session.save.assert_called_once_with(disk)
        self.assertIs(constructor.call_args.args[0], memory)
        client.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
