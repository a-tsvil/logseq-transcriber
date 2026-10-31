import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.import_images import ImageBuffer, import_chat, login_account, main, parse_args
from src.image_search import MAX_IMAGE_BYTES


def image(identifier, data=b"\xff\xd8\xffcat", **attributes):
    async def download(file):
        file.write(data)
    return SimpleNamespace(id=identifier, document=None, photo=True, download_media=AsyncMock(side_effect=download),
                           **attributes)


class ImportImagesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sleep = patch("src.import_images.asyncio.sleep", new_callable=AsyncMock)
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        self.index = Mock()
        self.index.seen.return_value = False
        self.index.add.return_value = "ready"
        self.entity = SimpleNamespace(id=123, title="Memes")
        async def dialogs():
            yield SimpleNamespace(id=-1000000000123, entity=self.entity)
        self.client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=101, bot=False)),
                                      iter_dialogs=Mock(side_effect=dialogs))

    def messages(self, messages):
        async def iterate(*args, **kwargs):
            for message in messages:
                yield message
        self.client.iter_messages = Mock(side_effect=iterate)

    async def test_only_selected_chat_downloads_in_memory_and_batch_limit_is_enforced(self):
        first, second = image(1), image(2)
        self.messages([first, second])
        with patch("builtins.print"):
            result = await import_chat(self.client, self.index, 101, -1000000000123, max_images=1, before=50, after=2)
        self.assertEqual(result["indexed"], 1)
        self.client.iter_dialogs.assert_called_once_with()
        self.client.iter_messages.assert_called_once_with(self.entity, limit=None, max_id=50, min_id=2, wait_time=1)
        self.index.add.assert_called_once_with(101, -1000000000123, 1, "Memes", b"\xff\xd8\xffcat", False)
        self.assertIsInstance(first.download_media.call_args.kwargs["file"], io.BytesIO)
        self.assertTrue(first.download_media.call_args.kwargs["file"].closed)
        second.download_media.assert_not_awaited()

    async def test_wrong_account_and_protected_chat_fail_before_reading_history(self):
        for me in (SimpleNamespace(id=202, bot=False), SimpleNamespace(id=101, bot=True)):
            self.client.get_me.return_value = me
            with self.assertRaises(ValueError):
                await import_chat(self.client, self.index, 101, -1000000000123)
        self.client.iter_dialogs.assert_not_called()
        self.client.get_me.return_value = SimpleNamespace(id=101, bot=False)
        self.entity.noforwards = True
        with self.assertRaisesRegex(ValueError, "protected"):
            await import_chat(self.client, self.index, 101, -1000000000123)

    async def test_existing_images_are_not_downloaded_again(self):
        message = image(1)
        self.messages([message])
        self.index.seen.return_value = True
        self.assertEqual((await import_chat(self.client, self.index, 101, -1000000000123))["indexed"], 0)
        message.download_media.assert_not_awaited()

    async def test_telegram_stickers_are_excluded_before_any_download_or_ai_call(self):
        sticker = image(1, sticker=True)
        sticker.photo = None
        sticker.document = SimpleNamespace(mime_type="image/webp", size=100)
        self.messages([sticker])
        counts = await import_chat(self.client, self.index, 101, -1000000000123)
        self.assertEqual(counts["indexed"], 0)
        sticker.download_media.assert_not_awaited()
        self.index.add.assert_not_called()
        self.index.exclude_sticker.assert_called_once_with(101, -1000000000123, 1)

    async def test_regular_webp_image_documents_are_still_indexed(self):
        message = image(1, data=b"RIFF1234WEBPimage")
        message.photo = None
        message.document = SimpleNamespace(mime_type="image/webp", size=17)
        self.messages([message])
        with patch("builtins.print"):
            counts = await import_chat(self.client, self.index, 101, -1000000000123)
        self.assertEqual(counts["indexed"], 1)
        self.index.add.assert_called_once()
        self.index.exclude_sticker.assert_not_called()

    async def test_unsupported_oversized_and_protected_images_are_skipped(self):
        protected = image(1, noforwards=True)
        large = image(2)
        large.document = SimpleNamespace(mime_type="image/png", size=MAX_IMAGE_BYTES + 1)
        large.photo = None
        invalid = image(3, data=b"not an image")
        nonimage = image(4)
        nonimage.photo = None
        temporary = image(5, media=SimpleNamespace(ttl_seconds=30))
        self.messages([protected, large, invalid, nonimage, temporary])
        result = await import_chat(self.client, self.index, 101, -1000000000123)
        self.assertEqual(result, {"indexed": 0, "skipped": 4, "needs_review": 0})
        protected.download_media.assert_not_awaited()
        large.download_media.assert_not_awaited()
        nonimage.download_media.assert_not_awaited()
        temporary.download_media.assert_not_awaited()
        self.index.add.assert_not_called()
        with ImageBuffer() as buffer:
            with self.assertRaisesRegex(ValueError, "exceeds"):
                buffer.write(b"x" * (MAX_IMAGE_BYTES + 1))
            self.assertEqual(buffer.getvalue(), b"")

    async def test_ai_failure_stops_batch_without_automatic_retry(self):
        first, second = image(1), image(2)
        self.messages([first, second])
        self.index.add.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            await import_chat(self.client, self.index, 101, -1000000000123)
        self.index.add.assert_called_once()
        second.download_media.assert_not_awaited()

    async def test_failed_cached_asset_is_reported_for_review_instead_of_indexed(self):
        self.messages([image(1)])
        self.index.add.return_value = "failed"
        with patch("builtins.print"):
            counts = await import_chat(self.client, self.index, 101, -1000000000123)
        self.assertEqual(counts, {"indexed": 0, "skipped": 0, "needs_review": 1})

    async def test_missing_chat_cannot_read_any_history(self):
        self.messages([])
        with self.assertRaisesRegex(ValueError, "not in"):
            await import_chat(self.client, self.index, 101, -1000000000999)
        self.client.iter_messages.assert_not_called()

    def test_import_requires_explicit_chat_consent_and_bounded_arguments(self):
        for arguments in (["--chat", "-1000000000123"], ["--chat", "0", "--consent-confirmed"],
                          ["--chat", "-1000000000123", "--consent-confirmed", "--max-images", "0"],
                          ["--chat", "-1000000000123", "--consent-confirmed", "--before", "5", "--after", "10"]):
            with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                parse_args(arguments)
        self.assertEqual(parse_args(["--chat", "-1000000000123", "--consent-confirmed"]).max_images, 20)
        self.assertTrue(parse_args(["--list-chats"]).list_chats)
        self.assertTrue(parse_args(["--list-chats", "--qr-login"]).qr_login)


class AccountLoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.password_error = type("SessionPasswordNeededError", (Exception,), {})
        self.login = SimpleNamespace(url="tg://login?token=TEST_ONLY", wait=AsyncMock())
        self.client = SimpleNamespace(is_user_authorized=AsyncMock(return_value=False), start=AsyncMock(),
                                      qr_login=AsyncMock(return_value=self.login), sign_in=AsyncMock())
        self.qr = Mock()
        errors = SimpleNamespace(SessionPasswordNeededError=self.password_error)
        modules = {"telethon": SimpleNamespace(errors=errors), "telethon.errors": errors,
                   "qrcode": SimpleNamespace(QRCode=Mock(return_value=self.qr))}
        patcher = patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_existing_session_does_not_request_another_code_or_qr(self):
        self.client.is_user_authorized.return_value = True
        await login_account(self.client, use_qr=True)
        self.client.qr_login.assert_not_awaited()
        self.client.start.assert_not_awaited()

    async def test_phone_login_remains_available(self):
        await login_account(self.client)
        self.client.start.assert_awaited_once()
        self.client.qr_login.assert_not_awaited()

    async def test_qr_login_waits_for_scan_without_requesting_a_phone_code(self):
        with patch("builtins.print"):
            await login_account(self.client, use_qr=True)
        self.qr.add_data.assert_called_once_with(self.login.url)
        self.qr.print_ascii.assert_called_once_with(invert=True)
        self.login.wait.assert_awaited_once()
        self.client.start.assert_not_awaited()
        self.client.sign_in.assert_not_awaited()

    async def test_qr_two_step_password_is_read_privately_from_terminal(self):
        self.login.wait.side_effect = self.password_error
        with patch("builtins.print") as output, patch("src.import_images.getpass.getpass", return_value="TEST_PASSWORD") as prompt:
            await login_account(self.client, use_qr=True)
        prompt.assert_called_once()
        self.client.sign_in.assert_awaited_once_with(password="TEST_PASSWORD")
        self.assertNotIn("TEST_PASSWORD", str(output.call_args_list))

    async def test_expired_qr_reports_expiry_without_retrying_login(self):
        self.login.wait.side_effect = TimeoutError
        with patch("builtins.print") as output, self.assertRaises(TimeoutError):
            await login_account(self.client, use_qr=True)
        self.assertIn("expired", output.call_args.args[0])
        self.client.qr_login.assert_awaited_once()

    async def test_qr_main_receives_login_updates_then_disables_them_and_disconnects_on_wrong_owner(self):
        self.client.connect = AsyncMock()
        self.client.disconnect = AsyncMock()
        self.client.set_receive_updates = AsyncMock()
        self.client.is_user_authorized.return_value = True
        self.client.get_me = AsyncMock(return_value=SimpleNamespace(id=202, bot=False))
        constructor = Mock(return_value=self.client)
        with tempfile.TemporaryDirectory() as directory:
            environment = {"TELEGRAM_ADMIN_USER_ID": "101", "TELEGRAM_ALLOWED_USER_IDS": "101", "TELEGRAM_API_ID": "12345",
                           "TELEGRAM_API_HASH": "test", "TELEGRAM_SESSION_FILE": str(Path(directory) / "test.session")}
            with patch.dict(os.environ, environment), patch.dict(sys.modules, {"telethon": SimpleNamespace(TelegramClient=constructor)}):
                with self.assertRaisesRegex(ValueError, "owner"):
                    await main(parse_args(["--list-chats", "--qr-login"]))
        self.assertTrue(constructor.call_args.kwargs["receive_updates"])
        self.client.set_receive_updates.assert_awaited_once_with(False)
        self.client.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
