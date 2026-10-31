import json
import base64
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from src.inbox import Inbox, MAX_MEDIA, description_url, fetch_description


def update(identifier=1, sender=101, text="Squats: 3 sets. https://youtu.be/example", attachment=None, chat_type="private"):
    message = {"message_id": identifier, "date": 1700000000, "from": {"id": sender},
               "chat": {"id": sender, "type": chat_type}, "text": text}
    if attachment:
        message["video"] = attachment
    return {"update_id": identifier, "message": message}


class InboxTests(unittest.TestCase):
    def setUp(self):
        activity = patch("src.inbox.telegram_activity", side_effect=lambda *args, **kwargs: nullcontext())
        activity.start()
        self.addCleanup(activity.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.inbox = Inbox(self.temporary.name, "101,202")
        description_patch = patch("src.inbox.fetch_description", return_value={"status": "empty"})
        self.description_fetch = description_patch.start()
        self.addCleanup(description_patch.stop)

    def tearDown(self):
        self.inbox.db.close()
        self.temporary.cleanup()

    def record(self, identifier=1):
        row = self.inbox.db.execute("SELECT record FROM submissions WHERE id=?", (identifier,)).fetchone()
        return json.loads(row[0])

    def test_only_allowlisted_private_messages_are_saved(self):
        self.inbox.remember([update(1), update(2, sender=999), update(3, chat_type="group")])
        self.assertEqual([item["id"] for item in self.inbox.pending()["submissions"]], [1])
        self.assertEqual(self.inbox.db.execute("SELECT offset FROM state").fetchone()[0], 4)

    def test_empty_allowlist_rejects_everyone(self):
        self.inbox.allowed.clear()
        self.inbox.remember([update()])
        self.assertEqual(self.inbox.pending()["submissions"], [])

    def test_cooking_links_and_video_captions_select_cooking_without_changing_workouts(self):
        video = update(2, text="", attachment={"file_id": "video", "mime_type": "video/mp4"})
        video["message"].pop("text")
        video["message"]["caption"] = "/cook"
        self.inbox.remember([update(1, text="/cook https://youtu.be/example"), video, update(3),
                             update(4, sender=999, text="/cook https://youtu.be/example")])
        for identifier in (1, 2):
            self.assertEqual(self.record(identifier)["analysis"]["type"], "cooking")
        self.assertEqual(self.record(1)["source_urls"], ["https://youtu.be/example"])
        self.assertEqual(self.record(2)["attachment"]["file_id"], "video")
        self.assertNotIn("type", self.record(3)["analysis"])
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 3)
        self.assertIn("cook", [c["command"] for c in self.inbox.commands(202)])
        self.assertIn("/cook https://", self.inbox.help_text(202))

    def test_bare_cook_command_waits_for_next_video_link_without_saving_a_note(self):
        self.inbox.bot_token = "test-only"
        with patch.object(self.inbox, "telegram", return_value=True) as telegram:
            self.inbox.remember([update(text="/cook")])
        self.assertIn("/cancel", telegram.call_args.args[1]["text"])
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.inbox.bot_token = ""
        self.inbox.remember([update(2, text="https://youtu.be/BaW_jenozKc"), update(3)])
        self.assertEqual(self.record(2)["analysis"]["type"], "cooking")
        self.assertEqual(self.record(2)["original_text"], "https://youtu.be/BaW_jenozKc")
        self.assertNotIn("type", self.record(3)["analysis"])
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM cooking_requests").fetchone()[0], 0)

    def test_cooking_wait_survives_restart_and_accepts_video_attachment(self):
        self.inbox.remember([update(1, text="/cook")])
        self.inbox.db.close()
        self.inbox = Inbox(self.temporary.name, "101,202")
        self.inbox.remember([update(2, text="", attachment={"file_id": "video", "mime_type": "video/mp4"})])
        self.assertEqual(self.record(2)["analysis"]["type"], "cooking")
        self.assertEqual(self.record(2)["attachment"]["file_id"], "video")

    def test_cooking_wait_is_private_per_user_and_cancel_does_not_save_note(self):
        self.inbox.remember([update(1, sender=999, text="/cook"), update(2, chat_type="group", text="/cook")])
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM cooking_requests").fetchone()[0], 0)
        self.inbox.remember([update(3, text="/cook"), update(4, sender=202),
                             update(5, sender=999, text="/cancel"), update(6, chat_type="group", text="/cancel")])
        self.assertNotIn("type", self.record(4)["analysis"])
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM cooking_requests").fetchone()[0], 1)
        self.inbox.remember([update(7, text="/cancel"), update(8)])
        self.assertNotIn("type", self.record(8)["analysis"])
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM cooking_requests").fetchone()[0], 0)
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 2)

    def test_cooking_wait_keeps_invalid_input_and_commands_from_becoming_notes(self):
        self.inbox.remember([update(1, text="/cook"), update(2, text="not a video"),
                             update(3, text="https://example.com/"), update(4, text="/help"),
                             update(5, text="/unknown"), update(6, text="/checklist"),
                             update(7, text="file", attachment={"file_id": "document", "mime_type": "application/pdf"})])
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM checklist_drafts").fetchone()[0], 0)
        self.inbox.remember([update(8, text="/cook https://youtu.be/BaW_jenozKc"), update(9, text="https://youtu.be/BaW_jenozKc")])
        self.assertEqual(self.record(8)["analysis"]["type"], "cooking")
        self.assertNotIn("type", self.record(9)["analysis"])
        self.inbox.remember([update(1, text="/cook"), update(8, text="/cook https://youtu.be/BaW_jenozKc")])
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM cooking_requests").fetchone()[0], 0)

    def test_cooking_wait_is_retained_when_graph_access_is_unavailable(self):
        self.inbox.remember([update(1, text="/cook")])
        with patch.object(self.inbox, "registered_user", return_value=True), patch.object(self.inbox, "find_graph", return_value=None):
            self.inbox.remember([update(2, text="https://youtu.be/BaW_jenozKc")])
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM cooking_requests").fetchone()[0], 1)

    def test_help_and_start_are_private_commands_and_never_exported(self):
        self.inbox.admin_id = 101
        self.inbox.bot_token = "test-only"
        with patch.object(self.inbox, "telegram", return_value=True) as telegram:
            self.inbox.remember([update(1, text="/help"), update(2, sender=202, text="/start"),
                                 update(3, sender=999, text="/help"), update(4, chat_type="group", text="/help")])
        self.assertEqual(telegram.call_count, 2)
        self.assertIn("/sync allow", telegram.call_args_list[0].args[1]["text"])
        self.assertNotIn("/sync", telegram.call_args_list[1].args[1]["text"])
        self.assertIn('/checklist "Graph name"\n', telegram.call_args_list[1].args[1]["text"])
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.assertEqual(self.inbox.db.execute("SELECT offset FROM state").fetchone()[0], 5)

    def test_command_menu_scopes_hide_owner_management_from_contributors(self):
        self.inbox.admin_id = 101
        self.inbox.bot_token = "test-only"
        with patch.object(self.inbox, "telegram", return_value=True) as telegram:
            self.assertEqual(self.inbox.configure_commands(), 2)
        registrations = [call.args[1] for call in telegram.call_args_list if call.args[0] == "setMyCommands"]
        self.assertEqual([row["commands"] for row in registrations[:2]], [[], []])
        owner, contributor = registrations[2:]
        self.assertEqual(owner["scope"], {"type": "chat", "chat_id": 101})
        self.assertEqual(contributor["scope"], {"type": "chat", "chat_id": 202})
        self.assertIn("sync", [c["command"] for c in owner["commands"]])
        self.assertNotIn("sync", [c["command"] for c in contributor["commands"]])
        for row in (owner, contributor):
            for command in row["commands"]:
                self.assertRegex(command["command"], r"^[a-z0-9_]{1,32}$")
                self.assertTrue(1 <= len(command["description"]) <= 256)
        self.assertEqual(sum(call.args[0] == "setChatMenuButton" for call in telegram.call_args_list), 2)
        self.assertFalse(any(call.args[0] in ("sendMessage", "getUpdates") for call in telegram.call_args_list))

    def test_command_registration_skips_an_unconfigured_bot(self):
        with patch.object(self.inbox, "telegram") as telegram:
            self.assertEqual(self.inbox.configure_commands(), 0)
            self.inbox.bot_token = "test-only"
            self.inbox.allowed.clear()
            self.assertEqual(self.inbox.configure_commands(), 0)
            telegram.assert_not_called()

    def test_sync_commands_are_owner_only_and_not_exported(self):
        self.inbox.admin_id = 101
        self.inbox.allowlist_token = "policy-secret"
        self.inbox.api_url = "http://logseq-sync:8787"
        with patch("src.inbox.request_json", return_value={"ok": True}) as request:
            self.inbox.remember([update(1, sender=202, text="/sync allow intruder@example.com"),
                                 update(2, text="/sync allow Brother@Example.com"),
                                 update(3, text="/sync allow invalid")])
        request.assert_called_once_with("http://logseq-sync:8787/training/allowlist", {"email": "brother@example.com"},
                                        {"Authorization": "Bearer policy-secret"}, method="PUT")
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.assertEqual(self.inbox.db.execute("SELECT offset FROM state").fetchone()[0], 4)

    def test_sync_commands_list_remove_and_fail_without_leaking_secrets(self):
        self.inbox.admin_id = 101
        self.inbox.allowlist_token = "policy-secret"
        self.inbox.api_url = "http://logseq-sync:8787"
        with patch("src.inbox.request_json", return_value={"owner": "owner@example.com", "emails": ["brother@example.com"]}):
            self.assertIn("owner@example.com (owner)", self.inbox.sync_accounts("/sync list", 101))
        with patch("src.inbox.request_json", return_value={"ok": True}) as request:
            self.assertIn("removed", self.inbox.sync_accounts("/sync remove brother@example.com", 101))
            self.assertEqual(request.call_args.kwargs["method"], "DELETE")
        with patch("src.inbox.request_json", side_effect=OSError("policy-secret")):
            self.assertEqual(self.inbox.sync_accounts("/sync allow brother@example.com", 101), "Sync account update failed; retry later.")
        with patch("src.inbox.request_json") as request:
            self.assertIn("Only", self.inbox.sync_accounts("/sync list", 202))
            request.assert_not_called()

    def test_duplicate_delivery_preserves_one_record(self):
        self.inbox.remember([update(), update()])
        self.assertEqual(len(self.inbox.pending()["submissions"]), 1)

    def test_stub_preserves_original_without_inventing_transcript(self):
        self.inbox.remember([update()])
        result = self.inbox.capture(1)
        record = self.record()
        self.assertEqual(result["publish_status"], "waiting_for_logseq")
        self.assertEqual(record["original_text"], update()["message"]["text"])
        self.assertEqual(record["analysis"], {"status": "pending", "transcript": None, "exercises": []})
        self.assertEqual(json.loads((Path(self.temporary.name) / "exports/1.json").read_text()), record)

    def test_pending_logseq_does_not_starve_new_submissions(self):
        self.inbox.remember([update(i) for i in range(1, 13)])
        for item in self.inbox.pending()["submissions"]:
            self.inbox.capture(item["id"])
        self.assertEqual([item["id"] for item in self.inbox.pending()["submissions"]][:2], [11, 12])

    def test_large_attachment_is_flagged_without_download(self):
        self.inbox.remember([update(attachment={"file_id": "file", "file_size": MAX_MEDIA + 1})])
        with patch.object(self.inbox, "telegram") as telegram:
            self.inbox.capture(1)
        telegram.assert_not_called()
        self.assertEqual(self.record()["media_status"], "too_large")

    def configure_logseq(self):
        self.inbox.graph_id = "test-graph"
        self.inbox.api_url = "http://logseq-sync:8787"
        self.inbox.token_file = Path(self.temporary.name) / "token"
        self.inbox.token_file.write_text("test-access-token")
        self.inbox.encryption_file = Path(self.temporary.name) / "encryption.json"
        self.inbox.encryption_file.write_text(json.dumps({"graph_id": "test-graph", "key": base64.b64encode(b"k" * 32).decode()}))

    def encrypt_response(self, _url, payload, _headers):
        return {"texts": ["ciphertext:" + str(index) for index, _ in enumerate(payload["texts"])]}

    def test_publish_retry_reuses_page_and_block_ids(self):
        self.configure_logseq()
        self.inbox.remember([update()])
        sent = []

        def fake_request(url, payload, headers):
            self.assertEqual(headers["Authorization"], "Bearer test-access-token")
            if url.endswith("/e2ee/encrypt"):
                return self.encrypt_response(url, payload, headers)
            if url.endswith("/pages"):
                return {"uuid": "page-uuid"}
            sent.append(payload)
            if len(sent) == 1:
                error = HTTPError(url, 503, "unavailable", None, None)
                error.close()
                raise error
            return {"blocks": payload["blocks"]}

        with patch("src.inbox.request_json", side_effect=fake_request) as request:
            self.inbox.capture(1)
            self.assertEqual(self.record()["page_id"], "page-uuid")
            self.assertIn("last_error", self.record())
            self.inbox.capture(1)
            self.assertEqual(request.call_count, 4)
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(self.record()["publish_status"], "published")
        self.assertNotIn("last_error", self.record())

    def test_acknowledgement_is_sent_only_once_while_waiting(self):
        self.inbox.bot_token = "test-token"
        self.inbox.remember([update()])
        with patch.object(self.inbox, "telegram", return_value={}) as telegram:
            self.inbox.capture(1)
            self.inbox.capture(1)
        self.assertEqual(telegram.call_count, 1)

    def test_unknown_or_invalid_capture_does_not_create_records(self):
        for identifier in (1, "1", True, "../../etc/passwd"):
            with self.assertRaises(ValueError):
                self.inbox.capture(identifier)
        self.assertEqual(self.inbox.pending()["submissions"], [])

    def test_ack_failure_preserves_publication_for_retry(self):
        self.configure_logseq()
        self.inbox.bot_token = "test-token"
        self.inbox.remember([update()])
        def respond(url, payload, headers, method=None):
            if url.endswith("/e2ee/encrypt"):
                return self.encrypt_response(url, payload, headers)
            return {"uuid": "page-uuid"} if url.endswith("/pages") else {"blocks": []}

        with patch("src.inbox.request_json", side_effect=respond):
            with patch.object(self.inbox, "telegram", side_effect=TimeoutError):
                with self.assertRaises(TimeoutError):
                    self.inbox.capture(1)
        self.assertEqual(self.record()["publish_status"], "published")
        with patch("src.inbox.request_json") as publish:
            with patch.object(self.inbox, "telegram", return_value={}):
                self.inbox.capture(1)
        publish.assert_not_called()
        self.assertEqual(self.inbox.db.execute("SELECT done FROM submissions").fetchone()[0], 1)

    def test_missing_or_wrong_graph_key_never_writes_plaintext(self):
        self.configure_logseq()
        self.inbox.remember([update()])
        self.inbox.encryption_file.write_text("")
        with patch("src.inbox.request_json") as request:
            self.inbox.capture(1)
            self.assertEqual(self.record()["publish_status"], "waiting_for_logseq_key")
            self.inbox.encryption_file.write_text(json.dumps({"graph_id": "other-graph", "key": base64.b64encode(b"k" * 32).decode()}))
            self.inbox.capture(1)
            self.assertIn("ValueError", self.record()["last_error"])
        request.assert_not_called()

    def test_page_timeout_reuses_ciphertext_and_only_encrypts_once(self):
        self.configure_logseq()
        self.inbox.remember([update()])
        pages = []
        encryption_calls = []

        def respond(url, payload, headers, method=None):
            if url.endswith("/e2ee/encrypt"):
                encryption_calls.append(payload)
                self.assertIn(update()["message"]["text"], payload["texts"][5])
                return self.encrypt_response(url, payload, headers)
            self.assertNotIn("https://youtu.be", json.dumps(payload))
            if url.endswith("/pages"):
                pages.append(payload)
                if len(pages) == 1:
                    raise TimeoutError()
                return {"uuid": "page-uuid"}
            return {"blocks": []}

        with patch("src.inbox.request_json", side_effect=respond):
            self.inbox.capture(1)
            self.inbox.capture(1)
        self.assertEqual(len(encryption_calls), 1)
        self.assertEqual(pages[0], pages[1])
        self.assertEqual(self.record()["publish_status"], "published")

    def test_invalid_encryption_response_does_not_create_a_page(self):
        self.configure_logseq()
        self.inbox.remember([update()])
        with patch("src.inbox.request_json", return_value={"texts": []}) as request:
            self.inbox.capture(1)
        self.assertEqual(request.call_count, 1)
        self.assertFalse(self.record().get("page_id"))
        self.assertIn("ValueError", self.record()["last_error"])

    def test_caption_is_preserved_encrypted_and_not_refetched_on_publish_retry(self):
        self.configure_logseq()
        self.inbox.remember([update(text="https://www.instagram.com/reel/ABC123/?igsh=tracking")])
        self.description_fetch.return_value = {"status": "copied", "text": "Squats: 3 × 10\nRest 60 seconds."}
        plaintext = []

        def respond(url, payload, headers, method=None):
            if url.endswith("/e2ee/encrypt"):
                plaintext.extend(payload["texts"])
                return self.encrypt_response(url, payload, headers)
            if url.endswith("/pages"):
                return {"uuid": "page-uuid"}
            raise TimeoutError()

        with patch("src.inbox.request_json", side_effect=respond):
            self.inbox.capture(1)
            self.inbox.capture(1)
        self.description_fetch.assert_called_once()
        self.assertEqual(self.description_fetch.call_args.args[0], "https://www.instagram.com/reel/ABC123/")
        self.assertTrue(any("Squats: 3 × 10\nRest 60 seconds." in line for line in plaintext))
        self.assertEqual(self.record()["descriptions"][0]["status"], "copied")
        self.assertEqual(self.record()["analysis"]["exercises"], [])

    def test_failed_caption_fetch_still_publishes_original_link(self):
        self.configure_logseq()
        self.inbox.remember([update(text="https://youtu.be/BaW_jenozKc")])
        self.description_fetch.return_value = {"status": "unavailable"}
        plaintext = []

        def respond(url, payload, headers):
            if url.endswith("/e2ee/encrypt"):
                plaintext.extend(payload["texts"])
                return self.encrypt_response(url, payload, headers)
            return {"uuid": "page-uuid"} if url.endswith("/pages") else {}

        with patch("src.inbox.request_json", side_effect=respond):
            self.inbox.capture(1)
        self.assertEqual(self.record()["publish_status"], "published")
        self.assertTrue(any("unavailable; original link retained" in line for line in plaintext))
        self.assertIn("https://youtu.be/BaW_jenozKc", plaintext)

    def test_duplicate_links_share_fetch_and_time_budget_limits_following_links(self):
        first = "https://www.instagram.com/reel/ABC/"
        second = "https://youtu.be/BaW_jenozKc"
        self.inbox.remember([update(text=f"{first} {first} {second} https://example.com")])
        with patch("src.inbox.time.monotonic", side_effect=[0, 0, 0, 21]):
            self.inbox.capture(1)
        self.description_fetch.assert_called_once()
        self.assertEqual([d["status"] for d in self.record()["descriptions"]], ["empty", "unavailable", "unsupported"])

    def test_ai_runs_after_link_publication_and_append_retry_never_repeats_model(self):
        from tests.test_analysis import result
        self.configure_logseq()
        self.inbox.ai.enabled = True
        self.inbox.ai.key_file = Path(self.temporary.name) / "gemini-key"
        self.inbox.ai.key_file.write_text("FAKE_TEST_KEY")
        self.inbox.bot_token = "fake-bot"
        self.inbox.remember([update()])
        ai_blocks = []
        analysis_timeout = True

        def respond(url, payload, headers, method=None):
            nonlocal analysis_timeout
            if method == "PATCH":
                return {}
            if url.endswith("/e2ee/encrypt"):
                return self.encrypt_response(url, payload, headers)
            if url.endswith("/pages"):
                return {"uuid": "page-uuid"}
            if len(payload["blocks"]) == 1:
                ai_blocks.append(payload)
                if analysis_timeout:
                    analysis_timeout = False
                    raise TimeoutError()
            return {}

        with patch.object(self.inbox, "telegram", return_value={}), patch("src.inbox.request_json", side_effect=respond), patch("src.analysis.generate", return_value=result()) as api:
            self.inbox.capture(1)
            api.assert_not_called()
            self.assertEqual(self.record()["publish_status"], "published")
            self.inbox.capture(1)
            self.assertEqual(api.call_count, 1)
            self.assertFalse(self.record()["analysis"].get("published", False))
            self.inbox.capture(1)
            self.assertEqual(api.call_count, 1)
        self.assertEqual(ai_blocks[0], ai_blocks[1])
        self.assertTrue(self.record()["analysis"]["published"])
        self.assertIn("AI processed", self.record()["page_title"])
        self.assertEqual(self.inbox.db.execute("SELECT done FROM submissions").fetchone()[0], 1)

    def test_analyzed_video_links_to_current_journal_and_retries_without_duplicates_or_ai(self):
        from tests.test_analysis import result
        self.configure_logseq()
        self.inbox.ai.enabled = True
        self.inbox.ai.key_file = Path(self.temporary.name) / "gemini-key"
        self.inbox.ai.key_file.write_text("FAKE_TEST_KEY")
        self.inbox.bot_token = "fake-bot"
        self.inbox.remember([update(text="https://youtu.be/BaW_jenozKc")])
        captures, plaintext = [], []

        def respond(url, payload, headers, method=None):
            if url.endswith("/e2ee/encrypt"):
                plaintext.extend(payload["texts"])
                return self.encrypt_response(url, payload, headers)
            if url.endswith("/pages"):
                return {"uuid": "video-page"}
            if url.endswith("/capture"):
                captures.append(payload)
                if len(captures) == 1:
                    raise TimeoutError("Response lost after journal write")
                return {"page-id": "journal-page"}
            return {}

        with patch.object(self.inbox, "telegram", return_value={}), patch("src.inbox.request_json", side_effect=respond), \
                patch("src.analysis.generate", return_value=result()) as ai, patch("src.inbox.datetime") as clock:
            clock.now.return_value = datetime.fromisoformat("2026-11-01T00:30:00+01:00")
            self.inbox.capture(1)
            self.assertFalse(captures)
            self.inbox.capture(1)
            self.assertEqual(self.inbox.db.execute("SELECT done FROM submissions").fetchone()[0], 0)
            prepared = self.record()["journal_link"]
            self.assertEqual(prepared["journal-day"], 20261101)
            clock.now.assert_called_once_with(self.inbox.zone)
            clock.now.return_value = datetime.fromisoformat("2026-11-02T00:30:00+01:00")
            self.inbox.capture(1)
            self.assertEqual(self.inbox.capture(1)["status"], "already_processed")
            self.assertEqual(ai.call_count, 1)
        self.assertEqual(captures[0], captures[1])
        self.assertEqual(plaintext.count("[[video-page]]"), 1)
        self.assertNotIn("video-page", json.dumps(captures))
        self.assertTrue(self.record()["journal_link"]["published"])
        self.assertEqual(self.record()["page_id"], "video-page")
        self.assertEqual(self.record()["journal_link"]["page_id"], "journal-page")
        self.assertEqual(self.inbox.db.execute("SELECT done FROM submissions").fetchone()[0], 1)

    def test_journal_links_apply_to_cooking_and_workouts_but_not_unanalyzed_or_plain_notes(self):
        item = {"analysis": {"status": "completed", "published": True}, "source_urls": ["https://youtu.be/BaW_jenozKc"]}
        self.assertTrue(self.inbox.needs_journal_link(item))
        item["analysis"]["type"] = "cooking"
        self.assertTrue(self.inbox.needs_journal_link(item))
        item["source_urls"] = []
        self.assertFalse(self.inbox.needs_journal_link(item))
        item["attachment"] = {"mime_type": "video/mp4"}
        self.assertTrue(self.inbox.needs_journal_link(item))
        for analysis in ({"status": "completed"}, {"status": "pending"}, {"status": "needs_review"}):
            item["analysis"] = analysis
            with patch("src.inbox.request_json") as request:
                self.inbox.publish_journal_link(item)
            request.assert_not_called()

    def test_journal_link_refuses_prepared_content_from_another_graph(self):
        self.configure_logseq()
        item = {"id": 1, "graph_id": self.inbox.graph_id, "page_id": "video-page",
                "analysis": {"status": "completed", "published": True},
                "source_urls": ["https://youtu.be/BaW_jenozKc"],
                "encrypted_payload": {"graph_id": "other-graph"}}
        with patch("src.inbox.request_json") as request, self.assertRaises(ValueError):
            self.inbox.publish_journal_link(item)
        request.assert_not_called()
        item["encrypted_payload"]["graph_id"] = self.inbox.graph_id
        item["journal_link"] = {"graph_id": "other-graph"}
        with patch("src.inbox.request_json") as request, self.assertRaises(ValueError):
            self.inbox.publish_journal_link(item)
        request.assert_not_called()

    def test_only_allowlisted_private_command_can_approve_existing_video(self):
        self.inbox.remember([update()])
        item = self.record()
        item["analysis"] = {"status": "needs_approval", "reason": "Video is 300 seconds.", "notified": True}
        self.inbox.save(item, done=True)
        self.inbox.remember([update(2, sender=999, text="/analyze 1"), update(3, sender=202, text="/analyze 1", chat_type="group")])
        self.assertEqual(self.record()["analysis"]["status"], "needs_approval")
        self.inbox.remember([update(4, sender=202, text="/analyze 1")])
        self.assertTrue(self.record()["analysis"]["approved"])
        self.assertEqual(self.record()["analysis"]["status"], "pending_video")
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 1)
        self.assertEqual(self.inbox.db.execute("SELECT done FROM submissions").fetchone()[0], 0)

    def test_quota_wait_does_not_prevent_saving_note_and_is_not_polled_until_due(self):
        self.configure_logseq()
        self.inbox.ai.enabled = True
        self.inbox.bot_token = "fake-bot"
        self.inbox.remember([update()])

        def respond(url, payload, headers):
            if url.endswith("/e2ee/encrypt"):
                return self.encrypt_response(url, payload, headers)
            return {"uuid": "page-uuid"} if url.endswith("/pages") else {}

        with patch.object(self.inbox, "telegram", return_value=[]), patch("src.inbox.request_json", side_effect=respond):
            self.inbox.capture(1)
            self.inbox.capture(1)
            self.assertEqual(self.record()["publish_status"], "published")
            self.assertEqual(self.record()["analysis"]["status"], "waiting_for_key")
            self.assertEqual(self.inbox.pending()["submissions"], [])

    def test_failed_approval_notice_is_retried_without_repeating_analysis(self):
        self.configure_logseq()
        self.inbox.ai.enabled = True
        self.inbox.bot_token = "fake-bot"
        self.inbox.remember([update()])
        calls = 0

        def telegram(method, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TimeoutError()
            return {}

        def respond(url, payload, headers):
            if url.endswith("/e2ee/encrypt"):
                return self.encrypt_response(url, payload, headers)
            return {"uuid": "page-uuid"} if url.endswith("/pages") else {}

        with patch.object(self.inbox, "telegram", side_effect=telegram), patch("src.inbox.request_json", side_effect=respond), patch.object(self.inbox.ai, "step", return_value={"status": "needs_approval", "reason": "Approve video."}) as step:
            self.inbox.capture(1)
            self.inbox.capture(1)
            self.assertEqual(self.inbox.db.execute("SELECT done FROM submissions").fetchone()[0], 0)
            self.inbox.capture(1)
        self.assertEqual(step.call_count, 1)
        self.assertEqual(calls, 3)
        self.assertTrue(self.record()["analysis"]["notified"])

    def test_page_name_uses_title_and_only_marks_saved_results_processed(self):
        self.inbox.remember([update()])
        item = self.record()
        item["descriptions"] = [{"title": "  Quick\nworkout  ", "status": "empty"}]
        self.assertEqual(self.inbox.page_title(item), "Quick workout — AI not processed · 1")
        self.inbox.ai.enabled = True
        self.assertIn("AI pending", self.inbox.page_title(item))
        item["analysis"].update(status="completed")
        self.assertIn("AI pending", self.inbox.page_title(item))
        item["analysis"]["published"] = True
        self.assertIn("AI processed", self.inbox.page_title(item))
        item["analysis"] = {"status": "needs_review"}
        self.assertIn("AI not processed", self.inbox.page_title(item))

    def test_page_rename_timeout_reuses_ciphertext_and_keeps_the_submission_pending(self):
        self.configure_logseq()
        self.inbox.ai.enabled = True
        self.inbox.remember([update()])
        item = self.record()
        item.update(page_id="existing-page", page_title="Old name", publish_status="published", acknowledged=True,
                    encrypted_payload={"graph_id": self.inbox.graph_id})
        item["descriptions"] = [{"title": "Quick workout", "status": "empty"}]
        item["analysis"] = {"status": "completed", "published": True}
        self.inbox.save(item)
        patches = []

        def respond(url, payload, headers, method=None):
            if method == "PATCH":
                patches.append(payload)
                self.assertEqual(url.rsplit("/", 1)[1], "existing-page")
                if len(patches) == 1:
                    raise TimeoutError()
                return {}
            self.assertEqual(url.rsplit("/", 1)[1], "encrypt")
            return {"texts": ["encrypted-title", "encrypted-name"]}

        with patch("src.inbox.request_json", side_effect=respond) as request:
            self.inbox.capture(1)
            self.assertEqual(self.inbox.db.execute("SELECT done FROM submissions").fetchone()[0], 0)
            self.inbox.capture(1)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(patches[0], patches[1])
        self.assertEqual(self.record()["page_title"], "Quick workout — AI processed · 1")
        self.assertEqual(self.inbox.db.execute("SELECT done FROM submissions").fetchone()[0], 1)


class DescriptionTests(unittest.TestCase):
    def test_metadata_keeps_video_title_when_caption_is_empty(self):
        output = json.dumps({"description": None, "title": "Quick workout", "duration": 25})
        with patch("src.inbox.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=output)):
            self.assertEqual(fetch_description("https://www.youtube.com/watch?v=BaW_jenozKc", 5),
                             {"status": "empty", "title": "Quick workout", "duration_seconds": 25})
    def test_only_supported_post_urls_are_canonicalized(self):
        self.assertEqual(description_url("https://youtu.be/BaW_jenozKc?t=12"), "https://www.youtube.com/watch?v=BaW_jenozKc")
        self.assertEqual(description_url("https://m.youtube.com/shorts/BaW_jenozKc"), "https://www.youtube.com/watch?v=BaW_jenozKc")
        self.assertEqual(description_url("https://www.instagram.com/p/ABC_-123/?igsh=secret"), "https://www.instagram.com/p/ABC_-123/")
        for url in ("http://127.0.0.1/", "http://169.254.169.254/", "file:///etc/passwd",
                    "https://youtube.com.evil.test/watch?v=BaW_jenozKc", "https://youtube.com@evil.test/",
                    "https://youtube.com:8443/watch?v=BaW_jenozKc", "https://youtube.com/redirect?q=http://localhost",
                    "https://www.instagram.com/accounts/login/", "https://youtube.com/watch?v=../../secret"):
            with self.subTest(url=url):
                self.assertIsNone(description_url(url))

    def test_caption_json_preserves_newlines_and_deduplicates_carousel(self):
        text = "Exercise one\nExercise two 🏋"
        item = json.dumps({"description": text, "duration": 30})
        result = subprocess.CompletedProcess([], 0, stdout=item + "\n" + item, stderr="")
        with patch("src.inbox.subprocess.run", return_value=result) as run:
            self.assertEqual(fetch_description("https://www.instagram.com/p/ABC/", 5), {"status": "copied", "text": text, "video_count": 2})
        self.assertIn("--skip-download", run.call_args.args[0])
        self.assertIn("--ignore-config", run.call_args.args[0])

    def test_blocked_empty_invalid_and_timeout_results_do_not_escape(self):
        for failure in (subprocess.TimeoutExpired(["yt_dlp"], 5), subprocess.CalledProcessError(1, ["yt_dlp"]), OSError()):
            with patch("src.inbox.subprocess.run", side_effect=failure):
                self.assertEqual(fetch_description("https://www.instagram.com/p/ABC/", 5), {"status": "unavailable"})
        for output, expected in (("null", "empty"), ('""', "empty"), ("garbage", "unavailable")):
            with patch("src.inbox.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=output)):
                self.assertEqual(fetch_description("https://www.instagram.com/p/ABC/", 5)["status"], expected)


if __name__ == "__main__":
    unittest.main()
