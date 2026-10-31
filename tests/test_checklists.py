import base64
from contextlib import nullcontext
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.inbox import Inbox
from test_inbox import update


class ChecklistTests(unittest.TestCase):
    def setUp(self):
        activity = patch("src.inbox.telegram_activity", side_effect=lambda *args, **kwargs: nullcontext())
        activity.start()
        self.addCleanup(activity.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.key = base64.b64encode(b"k" * 32).decode()
        self.primary = "11111111-1111-4111-8111-111111111111"
        self.other = "22222222-2222-4222-8222-222222222222"
        (self.root / "token").write_text("primary-token")
        (self.root / "key.json").write_text(json.dumps({"graph_id": self.primary, "key": self.key}))
        self.catalog = self.root / "graphs.json"
        self.catalog.write_text(json.dumps([{"id": self.other, "name": "Other graph", "token": "other-token", "key": self.key}]))
        self.box = Inbox(self.root, "101,202", graph_id=self.primary, graph_name="Training", api_url="http://sync",
                         token_file=self.root / "token", encryption_file=self.root / "key.json", graphs_file=self.catalog)

    def tearDown(self):
        self.box.db.close()
        self.temporary.cleanup()

    def record(self, identifier):
        return json.loads(self.box.db.execute("SELECT record FROM submissions WHERE id=?", (identifier,)).fetchone()[0])

    def test_graph_selection_is_per_user_and_explicit_override_does_not_change_it(self):
        self.box.remember([update(1, text='/graph "Other graph"'),
                           update(2, text="/checklist\nItem A\n\nItem B"),
                           update(3, sender=202, text="/checklist\nBrother's item"),
                           update(4, text='/checklist "Training"\nOne-off destination')])
        self.assertEqual(self.record(2)["graph_id"], self.other)
        self.assertEqual(self.record(2)["items"], ["Item A", "Item B"])
        self.assertEqual(self.record(3)["graph_id"], self.primary)
        self.assertEqual(self.record(4)["graph_id"], self.primary)
        self.assertEqual(self.box.selected_graph(101), self.other)
        self.assertIn("Other graph (selected)", self.box.graph_command("/graphs", 101))
        self.box.remember([update(5, text="/graph Training")])
        self.assertEqual(self.record(2)["graph_id"], self.other)
        restored = Inbox(self.root, "101,202", graph_id=self.primary, graph_name="Training", graphs_file=self.catalog)
        try:
            self.assertEqual(restored.selected_graph(101), self.primary)
        finally:
            restored.db.close()

    def test_invalid_commands_are_not_saved_or_allowed_to_fall_back(self):
        inputs = ['/checklist "Unknown"\nSecret', '/checklist "unclosed\nItem', '/checklist',
                  '/checklist\n' + '\n'.join('Item' for _ in range(101)), '/graph Unknown']
        self.box.remember([update(i, text=text) for i, text in enumerate(inputs, 1)])
        self.box.remember([update(20, sender=999, text="/checklist\nItem"),
                           update(21, chat_type="group", text="/checklist\nItem"),
                           update(22, text="/checklist\nItem", attachment={"file_id": "video"})])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.assertEqual(self.box.selected_graph(101), self.primary)

    def test_conversation_collects_lines_and_saves_one_named_checklist_on_done(self):
        self.box.remember([update(1, text="/checklist Weekly shopping"),
                           update(2, text="Milk"), update(3, text="Bread\n\n Eggs ")])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.box.remember([update(4, text="/done")])
        record = self.record(4)
        self.assertEqual(record["items"], ["Milk", "Bread", "Eggs"])
        self.assertEqual(record["checklist_title"], "Weekly shopping")
        self.assertEqual(record["kind"], "checklist")
        self.assertEqual(record["analysis"]["status"], "not_requested")
        self.assertEqual(record["source_urls"], [])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM checklist_drafts").fetchone()[0], 0)
        self.box.remember([update(5, text="Ordinary note")])
        self.assertNotIn("kind", self.record(5))

    def test_cooking_request_cannot_interrupt_an_active_checklist(self):
        self.box.remember([update(1, text="/checklist"), update(2, text="Milk"),
                           update(3, text="/cook"), update(4, text="/cook https://youtu.be/example"),
                           update(5, text="/done")])
        self.assertEqual(self.record(5)["items"], ["Milk"])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM cooking_requests").fetchone()[0], 0)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 1)

    def test_drafts_persist_per_user_and_pin_original_graph_and_date(self):
        started = update(1, text="/checklist")
        started["message"]["date"] = int(datetime.fromisoformat("2026-10-28T23:30:00+00:00").timestamp())
        self.box.remember([started, update(2, text="Owner item"),
                           update(3, sender=202, text="/checklist Other list"),
                           update(4, sender=202, text="Other user's item"),
                           update(5, text='/graph "Other graph"')])
        self.box.db.close()
        self.box = Inbox(self.root, "101,202", graph_id=self.primary, graph_name="Training", graphs_file=self.catalog)
        self.box.remember([update(6, text="/done"), update(7, sender=202, text="/done")])
        self.assertEqual(self.record(6)["graph_id"], self.primary)
        self.assertEqual(self.record(6)["journal_day"], 20261029)
        self.assertEqual(self.record(6)["items"], ["Owner item"])
        self.assertEqual(self.record(7)["items"], ["Other user's item"])
        self.assertEqual(self.record(7)["checklist_title"], "Other list")

    def test_cancel_and_empty_done_do_not_create_notes_or_discard_items_accidentally(self):
        self.box.remember([update(1, text="/done"), update(2, text="/cancel"),
                           update(3, text="/checklist"), update(4, text="/done")])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM checklist_drafts").fetchone()[0], 1)
        self.box.remember([update(5, text="Milk"), update(6, text="/checklist Replacement"),
                           update(7, text="/checklist\nDo not replace"), update(8, text="/cancel")])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM checklist_drafts").fetchone()[0], 0)

    def test_replayed_updates_do_not_duplicate_items_or_reopen_finished_draft(self):
        start, item, done = update(1, text="/checklist"), update(2, text="Milk"), update(3, text="/done")
        self.box.remember([start, item, item])
        self.box.remember([start, done, done, start])
        self.box.remember([start, item, done])
        self.assertEqual(self.record(3)["items"], ["Milk"])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM checklist_drafts").fetchone()[0], 0)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 1)

    def test_draft_rejects_media_blank_items_and_overflow_without_losing_saved_items(self):
        self.box.remember([update(1, text="/checklist"), update(2, text="Milk"),
                           update(3, text="Caption", attachment={"file_id": "video"}),
                           update(4, text="   "), update(5, text="\n".join(["Eggs"] * 100)),
                           update(6, text="/done")])
        self.assertEqual(self.record(6)["items"], ["Milk"])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 1)
        self.box.remember([update(7, text="/checklist"), update(8, text="\n".join(["Eggs"] * 100)),
                           update(9, text="Overflow"), update(10, text="/done")])
        self.assertEqual(len(self.record(10)["items"]), 100)

    def test_draft_done_rechecks_access_and_does_not_fall_back_to_another_graph(self):
        self.box.remember([update(1, text='/graph "Other graph"'), update(2, text="/checklist"),
                           update(3, text="Milk")])
        self.box.extra_graphs.clear()
        self.box.remember([update(4, text="/done")])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        draft = json.loads(self.box.db.execute("SELECT draft FROM checklist_drafts").fetchone()[0])
        self.assertEqual(draft["graph_id"], self.other)
        self.assertEqual(draft["items"], ["Milk"])

    def test_unauthorized_and_group_messages_cannot_start_or_append_drafts(self):
        self.box.remember([update(1, sender=999, text="/checklist"),
                           update(2, chat_type="group", text="/checklist"),
                           update(3, text="/checklist"), update(4, sender=999, text="Bad item"),
                           update(5, chat_type="group", text="Group item"), update(6, text="Good item"),
                           update(7, text="/done")])
        self.assertEqual(self.record(7)["items"], ["Good item"])

    def graph_choice(self, identifier, graph=None, user=101, bound_user=None, chat_type="private", chat_id=None):
        return {"update_id": identifier, "callback_query": {
            "id": "choice-" + str(identifier), "from": {"id": user},
            "data": f"graph:{user if bound_user is None else bound_user}:{graph or self.other}",
            "message": {"message_id": 50, "chat": {"id": user if chat_id is None else chat_id, "type": chat_type}}}}

    def test_graph_picker_menu_and_typed_alias_show_current_graph_and_buttons(self):
        self.box.bot_token = "test-token"
        with patch.object(self.box, "telegram") as telegram:
            self.box.remember([update(1, text="/select_graph"), update(2, text="/select-graph")])
        panels = [call.args[1] for call in telegram.call_args_list if call.args[0] == "sendMessage"]
        self.assertEqual(len(panels), 2)
        for panel in panels:
            self.assertIn("Current graph: Training", panel["text"])
            buttons = panel["reply_markup"]["inline_keyboard"]
            self.assertEqual(len(buttons), 2)
            self.assertTrue(buttons[0][0]["text"].startswith("✓ "))
            self.assertEqual(buttons[1][0]["callback_data"], f"graph:101:{self.other}")
            self.assertTrue(all(len(button["callback_data"].encode()) <= 64 for row in buttons for button in row))
        commands = {command["command"] for command in self.box.commands(101)}
        self.assertIn("select_graph", commands)
        self.assertFalse(commands & {"graph", "graphs", "select-graph"})
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)

    def test_graph_button_updates_picker_persists_selection_and_keeps_draft_destination(self):
        self.box.remember([update(1, text="/checklist"), update(2, text="Milk")])
        self.box.bot_token = "test-token"
        with patch.object(self.box, "telegram") as telegram:
            self.box.remember([self.graph_choice(3), update(4, text="/done"), update(5, text="Ordinary note")])
        panel = next(call.args[1] for call in telegram.call_args_list if call.args[0] == "editMessageText")
        self.assertIn("Current graph: Other graph", panel["text"])
        self.assertTrue(panel["reply_markup"]["inline_keyboard"][1][0]["text"].startswith("✓ "))
        self.assertEqual(self.box.selected_graph(101), self.other)
        self.assertEqual(self.box.selected_graph(202), self.primary)
        self.assertEqual(self.record(4)["graph_id"], self.primary)
        self.assertEqual(self.record(5)["graph_id"], self.other)
        restored = Inbox(self.root, "101,202", graph_id=self.primary, graph_name="Training", graphs_file=self.catalog)
        try:
            self.assertEqual(restored.selected_graph(101), self.other)
        finally:
            restored.db.close()

    def test_graph_callbacks_reject_other_users_groups_and_mismatched_private_chats(self):
        self.box.remember([self.graph_choice(1, user=999), self.graph_choice(2, bound_user=202),
                           self.graph_choice(3, chat_type="group"), self.graph_choice(4, chat_id=202)])
        self.assertEqual(self.box.selected_graph(101), self.primary)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM user_graphs").fetchone()[0], 0)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)

    def test_graph_picker_excludes_setup_required_buttons_and_disambiguates_names(self):
        self.box.logseq_users = {"101": "owner@example.com"}
        self.box.allowlist_token = "management-secret"
        unconfigured = "33333333-3333-4333-8333-333333333333"
        graphs = {"graphs": [{"id": self.primary, "name": "Same name"},
                              {"id": self.other, "name": "Same name"},
                              {"id": unconfigured, "name": "Needs setup"}]}
        with patch("src.inbox.request_json", return_value=graphs):
            text = self.box.graph_command("/select_graph", 101)
            self.assertIn("Needs setup — bot setup required", text)
            buttons = self.box.graph_keyboard["inline_keyboard"]
            self.assertEqual(len(buttons), 2)
            self.assertIn(self.primary, buttons[0][0]["text"])
            self.assertIn(self.other, buttons[1][0]["text"])
            self.box.remember([self.graph_choice(1, graph=unconfigured)])
        self.assertEqual(self.box.selected_graph(101), self.primary)
        self.assertNotIn("management-secret", json.dumps(buttons))

    def test_graph_discovery_failure_clears_buttons_and_does_not_change_selection(self):
        self.box.graph_command("/select_graph", 101)
        self.box.logseq_users = {"101": "owner@example.com"}
        self.box.allowlist_token = "management-secret"
        with patch("src.inbox.request_json", side_effect=OSError("private detail")):
            self.assertIn("Unable to refresh", self.box.graph_command("/select_graph", 101))
            self.assertIsNone(self.box.graph_keyboard)
            self.box.remember([self.graph_choice(1)])
        self.assertEqual(self.box.selected_graph(101), self.primary)

    def test_failed_graph_callback_ack_or_edit_does_not_undo_selection_or_retry_update(self):
        self.box.bot_token = "test-token"
        with patch.object(self.box, "telegram", side_effect=OSError("expired callback")):
            self.box.remember([self.graph_choice(1)])
        self.assertEqual(self.box.selected_graph(101), self.other)
        self.assertEqual(self.box.db.execute("SELECT offset FROM state").fetchone()[0], 2)

    def test_checklist_retry_keeps_ciphertext_date_and_ids_and_never_calls_ai(self):
        message = update(1, text='/checklist "Other graph"\nBuy oats\nPrepare workout')
        message["message"]["date"] = int(datetime.fromisoformat("2026-10-28T23:30:00+00:00").timestamp())
        self.box.remember([message, message])
        self.box.ai.enabled = True
        self.box.bot_token = "test-token"
        captures = []
        encryptions = []
        def request(url, payload=None, headers=None, method=None):
            if url.endswith("/health"):
                return {"journal-checklists": True, "boolean-checklists": True}
            self.assertIn(self.other, url)
            self.assertEqual(headers["Authorization"], "Bearer other-token")
            if url.endswith("/encrypt"):
                encryptions.append(payload)
                return {"texts": ["encrypted:" + str(i) for i in range(len(payload["texts"]))]}
            self.assertTrue(url.endswith("/capture"))
            captures.append(payload)
            if len(captures) == 1:
                raise TimeoutError("Response lost")
            return {"page-id": "journal-page"}
        with patch("src.inbox.request_json", side_effect=request), patch.object(self.box, "telegram"), \
                patch.object(self.box.ai, "step") as ai, patch.object(self.box, "describe") as describe:
            self.box.capture(1)
            self.box.remember([update(2, text="/graph Training")])
            self.box.capture(1)
            ai.assert_not_called()
            describe.assert_not_called()
        self.assertEqual(len(encryptions), 1)
        self.assertEqual(captures[0], captures[1])
        self.assertEqual(captures[0]["journal-day"], 20261029)
        tree = captures[0]["blocks"][0]
        self.assertEqual(len(tree["children"]), 2)
        self.assertTrue(all(item["checkbox"] is True for item in tree["children"]))
        self.assertNotIn("Buy oats", json.dumps(captures))
        self.assertEqual(self.record(1)["publish_status"], "published")
        self.assertEqual(self.box.db.execute("SELECT done FROM submissions WHERE id=1").fetchone()[0], 1)

    def test_missing_worker_capability_and_removed_graph_do_not_write_elsewhere(self):
        self.box.remember([update(1, text='/checklist "Other graph"\nItem')])
        with patch("src.inbox.request_json", return_value={"journal-checklists": True}) as request:
            self.box.capture(1)
            request.assert_called_once_with("http://sync/health")
        self.box.extra_graphs.clear()
        with patch("src.inbox.request_json") as request:
            self.box.capture(1)
            request.assert_not_called()
        self.assertNotEqual(self.record(1)["publish_status"], "published")

    def test_normal_capture_uses_its_snapshotted_graph_credentials(self):
        self.box.remember([update(1, text='/graph "Other graph"'), update(2, text="Workout note"), update(3, text="/graph Training")])
        def request(url, payload=None, headers=None, method=None):
            self.assertIn(self.other, url)
            self.assertEqual(headers["Authorization"], "Bearer other-token")
            if url.endswith("/encrypt"):
                return {"texts": ["ciphertext" + str(i) for i in range(len(payload["texts"]))]}
            return {"uuid": "page"} if url.endswith("/pages") else {"blocks": []}
        with patch("src.inbox.request_json", side_effect=request):
            self.box.capture(2)
        self.assertEqual(self.record(2)["encrypted_payload"]["graph_id"], self.other)
        record = self.record(2)
        record["analysis"] = {"status": "completed", "summary": "Summary", "source": "caption", "model": "test", "exercises": []}
        with patch("src.inbox.request_json", side_effect=request):
            self.box.publish_analysis(record)
            self.box.rename_page(record)
        self.assertTrue(record["analysis"]["published"])

    def test_unencrypted_checklist_retries_without_calling_encryption(self):
        self.box.extra_graphs[0]["key"] = None
        self.box.remember([update(1, text='/checklist "Other graph"\nMilk\nBread')])
        writes = []
        def request(url, payload=None, headers=None, method=None):
            if url.endswith("/health"):
                return {"journal-checklists": True, "boolean-checklists": True}
            self.assertEqual(headers["Authorization"], "Bearer other-token")
            self.assertTrue(url.endswith("/capture"))
            writes.append(payload)
            if len(writes) == 1:
                raise TimeoutError("Lost response")
            return {"page-id": "journal"}
        with patch("src.inbox.request_json", side_effect=request):
            self.box.capture(1)
            self.box.capture(1)
        self.assertEqual(writes[0], writes[1])
        self.assertEqual([c["title"] for c in writes[0]["blocks"][0]["children"]], ["Milk", "Bread"])
        self.assertEqual(self.record(1)["publish_status"], "published")

    def test_checklist_uses_boolean_items_and_never_sends_task_status(self):
        self.box.extra_graphs[0]["key"] = None
        self.box.remember([update(1, text='/checklist "Other graph"\nMilk')])
        with patch("src.inbox.request_json", side_effect=[{"boolean-checklists": True}, {"page-id": "journal"}]) as request:
            self.box.capture(1)
        self.assertEqual(request.call_count, 2)
        payload = request.call_args.args[1]
        self.assertEqual(payload["checkbox-property"], {"title": "Checked", "name": "checked"})
        item = payload["blocks"][0]["children"][0]
        self.assertTrue(item["checkbox"])
        self.assertNotIn("task", item)
        self.assertEqual(self.record(1)["publish_status"], "published")

    def test_unencrypted_notes_analysis_and_rename_use_plain_text(self):
        self.box.extra_graphs[0]["key"] = None
        self.box.remember([update(1, text='/graph "Other graph"'), update(2, text="Shopping note")])
        calls = []
        def request(url, payload=None, headers=None, method=None):
            self.assertNotIn("/encrypt", url)
            self.assertIn(self.other, url)
            self.assertEqual(headers["Authorization"], "Bearer other-token")
            calls.append((url, payload))
            return {"uuid": "page"} if url.endswith("/pages") else {}
        with patch("src.inbox.request_json", side_effect=request):
            self.box.capture(2)
            record = self.record(2)
            record["analysis"] = {"status": "completed", "summary": "Summary", "source": "caption", "model": "test", "exercises": []}
            self.box.publish_analysis(record)
            self.box.rename_page(record)
        self.assertIn("Original text: Shopping note", [b["title"] for b in calls[1][1]["blocks"]])
        self.assertIn("Summary", calls[2][1]["blocks"][0]["title"])
        self.assertIn("AI processed", calls[3][1]["title"])

    def test_explicit_null_key_loads_but_missing_key_is_rejected(self):
        entry = {"id": self.other, "name": "Shopping", "token": "token", "key": None}
        self.catalog.write_text(json.dumps([entry]))
        plain = Inbox(self.root / "plain", "101", graphs_file=self.catalog)
        plain.db.close()
        del entry["key"]
        self.catalog.write_text(json.dumps([entry]))
        with self.assertRaises(ValueError):
            Inbox(self.root / "missing", "101", graphs_file=self.catalog)

    def test_duplicate_catalog_names_and_invalid_keys_are_rejected(self):
        for entry in [{"id": self.other, "name": "training", "token": "token", "key": self.key},
                      {"id": self.other, "name": "Other", "token": "token", "key": base64.b64encode(b"short").decode()}]:
            self.catalog.write_text(json.dumps([entry]))
            with self.assertRaises(ValueError):
                Inbox(self.root / "invalid", "101", graph_id=self.primary, graph_name="Training", graphs_file=self.catalog)

    def test_upgrade_pins_legacy_pending_records_before_a_later_default_change(self):
        self.box.remember([update(1, text="Legacy note")])
        record = self.record(1)
        record.pop("graph_id")
        self.box.save(record)
        upgraded = Inbox(self.root, "101", graph_id=self.primary)
        upgraded.db.close()
        moved = Inbox(self.root, "101", graph_id=self.other)
        try:
            self.assertEqual(self.record(1)["graph_id"], self.primary)
            with self.assertRaises(ValueError):
                moved.graph_credentials(self.record(1))
        finally:
            moved.db.close()

    def test_live_discovery_lists_unconfigured_graphs_and_uses_requesters_identity(self):
        self.box.logseq_users = {"101": "owner@example.com", "202": "brother@example.com"}
        self.box.allowlist_token = "metadata-secret"
        shopping = "33333333-3333-4333-8333-333333333333"
        def discover(url, payload=None, headers=None, method=None):
            self.assertEqual(headers, {"Authorization": "Bearer metadata-secret"})
            self.assertIsNone(payload)
            rows = [{"id": self.primary, "name": "Training"}]
            if "owner%40example.com" in url:
                rows += [{"id": self.other, "name": "Other graph"}, {"id": shopping, "name": "Shopping Lists"}]
            else:
                self.assertIn("brother%40example.com", url)
            return {"graphs": rows}
        with patch("src.inbox.request_json", side_effect=discover):
            listing = self.box.graph_command("/graphs", 101)
            self.assertIn("Shopping Lists — bot setup required", listing)
            self.assertIn("Other graph — ready", listing)
            self.assertNotIn("Shopping Lists", self.box.graph_command("/graphs", 202))
            self.assertIn("setup is required", self.box.graph_command('/graph "Shopping Lists"', 101))
            with self.assertRaisesRegex(ValueError, "setup is required"):
                self.box.parse_checklist('/checklist "Shopping Lists"\nItem', 101)
            self.assertEqual(self.box.selected_graph(101), self.primary)
        with patch("src.inbox.request_json") as request:
            self.assertIn("not linked", self.box.graph_command("/graphs", 999))
            request.assert_not_called()

    def test_discovery_outage_does_not_fall_back_to_stale_credentials(self):
        self.box.logseq_users = {"101": "owner@example.com"}
        self.box.allowlist_token = "metadata-secret"
        with patch("src.inbox.request_json", side_effect=OSError("credential-internal-detail")):
            self.assertEqual(self.box.graph_command("/graphs", 101), "Unable to refresh shared graphs. Please try again later.")
        self.box.logseq_users.clear()
        self.box.remember([update(1, text='/checklist "Other graph"\nItem')])
        self.box.logseq_users = {"101": "owner@example.com"}
        with patch("src.inbox.request_json", return_value={"graphs": []}) as request:
            self.box.capture(1)
            self.assertEqual(request.call_count, 1)
            self.assertIn("/training/graphs?", request.call_args.args[0])
        self.assertNotEqual(self.record(1)["publish_status"], "published")

    def test_duplicate_discovered_names_require_an_explicit_graph_id(self):
        self.box.logseq_users = {"101": "owner@example.com"}
        self.box.allowlist_token = "metadata-secret"
        rows = [{"id": self.primary, "name": "Training"}, {"id": self.other, "name": "Training"}]
        with patch("src.inbox.request_json", return_value={"graphs": rows}):
            self.assertIn(self.other, self.box.graph_command("/graphs", 101))
            self.assertIn("Several graphs", self.box.graph_command('/graph "Training"', 101))
            self.assertIn("Selected graph", self.box.graph_command('/graph ' + self.other, 101))
            self.assertEqual(self.box.selected_graph(101), self.other)

    def test_unmapped_contributor_does_not_stall_other_users(self):
        self.box.logseq_users = {"101": "owner@example.com"}
        self.box.allowlist_token = "metadata-secret"
        self.box.bot_token = "test-only"
        with patch("src.inbox.request_json", return_value={"graphs": [{"id": self.primary, "name": "Training"}]}), \
                patch.object(self.box, "telegram") as telegram:
            self.box.remember([update(1, sender=202, text="Unlinked user's note"), update(2, text="Owner's note")])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 1)
        self.assertEqual(self.record(2)["submitted_by"], 101)
        self.assertIn("not linked", telegram.call_args.args[1]["text"])
        self.assertEqual(self.box.db.execute("SELECT offset FROM state").fetchone()[0], 3)

    def test_video_approval_cannot_target_another_accounts_private_graph(self):
        self.box.remember([update(1, text="Video")])
        record = self.record(1)
        record["analysis"]["status"] = "needs_approval"
        self.box.save(record)
        self.box.logseq_users = {"202": "brother@example.com"}
        self.box.allowlist_token = "metadata-secret"
        with patch("src.inbox.request_json", return_value={"graphs": []}):
            self.assertIn("not available", self.box.approve_analysis("/analyze 1", 202))
        self.assertEqual(self.record(1)["analysis"]["status"], "needs_approval")


if __name__ == "__main__":
    unittest.main()
