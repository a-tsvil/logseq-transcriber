from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.inbox import Inbox


PRIMARY = "11111111-1111-4111-8111-111111111111"
SHOPPING = "22222222-2222-4222-8222-222222222222"


def update(identifier=1, user=303, text="/start", chat_type="private", bot=False):
    return {"update_id": identifier, "message": {"message_id": identifier, "date": 1700000000,
            "from": {"id": user, "first_name": "Test Wife", "username": "test_wife", "is_bot": bot},
            "chat": {"id": user, "type": chat_type}, "text": text}}


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "graphs.json"
        self.registry.write_text(json.dumps([{"id": SHOPPING, "name": "Shopping Lists", "token": "TEST_TOKEN", "key": None}]))
        self.settings = dict(graph_id=PRIMARY, graph_name="Training", graphs_file=str(self.registry), admin_id="101",
                             logseq_users='{"101":"owner@example.com","202":"brother@example.com"}',
                             api_url="http://sync", allowlist_token="TEST_POLICY", bot_token="TEST_BOT")
        self.box = Inbox(self.root, "101,202", **self.settings)
        self.addCleanup(self.box.db.close)
        self.graphs = {"graphs": [{"id": PRIMARY, "name": "Training"}, {"id": SHOPPING, "name": "Shopping Lists"}]}
        activity = patch("src.inbox.telegram_activity", side_effect=lambda *args, **kwargs: nullcontext())
        activity.start()
        self.addCleanup(activity.stop)
        telegram = patch.object(self.box, "telegram", return_value=True)
        self.telegram = telegram.start()
        self.addCleanup(telegram.stop)
        request = patch("src.inbox.request_json", return_value=self.graphs)
        self.request = request.start()
        self.addCleanup(request.stop)

    def callback(self, action="allow", graph=SHOPPING, target=303, sender=101, chat=101, chat_type="private"):
        return {"id": "callback", "from": {"id": sender}, "message": {"message_id": 1, "chat": {"id": chat, "type": chat_type}},
                "data": f"user:{action}:{target}" + (":" + graph if graph else "")}

    def register(self, graph=SHOPPING, user=303):
        self.box.remember([update(user=user)])
        self.box.registration_callback(self.callback(graph=graph, target=user))

    def test_start_requests_access_without_auto_registration_or_note_export(self):
        self.box.remember([update()])
        row = self.box.db.execute("SELECT name,status FROM bot_users WHERE user_id=303").fetchone()
        self.assertIn("Test Wife", row[0])
        self.assertEqual(row[1], "pending")
        self.assertNotIn(303, self.box.allowed)
        notices = [call.args[1] for call in self.telegram.call_args_list if call.args[0] == "sendMessage"]
        self.assertEqual([notice["chat_id"] for notice in notices], [101, 303])
        self.assertIn("reply_markup", notices[0])
        self.box.remember([update(2, text="Unapproved private note")])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)

    def test_repeated_start_does_not_spam_owner_and_group_or_bot_start_is_ignored(self):
        self.box.remember([update(), update(2), update(3, user=404, chat_type="group"), update(4, user=505, bot=True)])
        owner_notices = [call for call in self.telegram.call_args_list if call.args[0] == "sendMessage" and call.args[1]["chat_id"] == 101]
        self.assertEqual(len(owner_notices), 1)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM bot_users").fetchone()[0], 1)

    def test_non_owner_or_group_callbacks_cannot_grant_posting_access(self):
        self.box.remember([update()])
        for callback in (self.callback(sender=202, chat=202), self.callback(chat=202), self.callback(chat_type="group"), self.callback(graph=None)):
            self.box.registration_callback(callback)
        self.assertNotIn(303, self.box.allowed)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM bot_user_graphs").fetchone()[0], 0)

    def test_approval_grants_only_the_selected_graph_without_claiming_a_logseq_identity(self):
        self.register()
        self.assertIn(303, self.box.allowed)
        self.assertNotIn("303", self.box.logseq_users)
        self.assertEqual(self.box.selected_graph(303), SHOPPING)
        self.assertEqual(self.box.graph_catalog(303), [{"id": SHOPPING, "name": "Shopping Lists", "enabled": True}])
        self.assertNotIn("Training", self.box.graph_command("/graphs", 303))
        menu = {command["command"] for command in self.box.commands(303)}
        self.assertFalse(menu & {"users", "sync", "search"})
        self.assertIn("owner", self.box.search_images("/search cat", 303))
        self.assertIn("Only", self.box.sync_accounts("/sync list", 303))

    def test_posting_user_picker_only_shows_grants_and_stale_buttons_cannot_restore_access(self):
        self.register()
        self.box.graph_command("/select_graph", 303)
        buttons = self.box.graph_keyboard["inline_keyboard"]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0][0]["callback_data"], f"graph:303:{SHOPPING}")
        callback = {"id": "graph-choice", "from": {"id": 303}, "data": f"graph:303:{PRIMARY}",
                    "message": {"message_id": 20, "chat": {"id": 303, "type": "private"}}}
        self.box.graph_callback(callback)
        self.assertEqual(self.box.selected_graph(303), SHOPPING)
        self.box.registration_callback(self.callback(action="drop"))
        callback["data"] = buttons[0][0]["callback_data"]
        self.box.graph_callback(callback)
        self.assertIsNone(self.box.db.execute("SELECT graph_id FROM user_graphs WHERE user_id=303").fetchone())
        self.assertEqual(self.box.graph_keyboard["inline_keyboard"], [])

    def test_posting_user_draft_keeps_granted_graph_and_survives_revoked_access(self):
        self.register()
        self.box.remember([update(2, text="/checklist Shopping"), update(3, text="Milk\nBread")])
        self.box.registration_callback(self.callback(action="drop"))
        self.box.remember([update(4, text="/done")])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM checklist_drafts").fetchone()[0], 1)
        self.box.registration_callback(self.callback())
        self.box.remember([update(5, text="/done")])
        record = json.loads(self.box.db.execute("SELECT record FROM submissions WHERE id=5").fetchone()[0])
        self.assertEqual(record["graph_id"], SHOPPING)
        self.assertEqual(record["items"], ["Milk", "Bread"])
        self.assertEqual(record["checklist_title"], "Shopping")

    def test_posting_user_can_send_notes_and_checklists_but_not_override_private_destination(self):
        self.register()
        self.box.remember([update(2, text="Buy milk"), update(3, text="/checklist\nMilk\nEggs"),
                           update(4, text='/checklist "Training"\nPrivate item')])
        rows = self.box.db.execute("SELECT record FROM submissions ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            record = json.loads(row[0])
            self.assertEqual(record["submitted_by"], 303)
            self.assertEqual(record["graph_id"], SHOPPING)
            self.assertEqual(self.box.graph_credentials(record)[0], SHOPPING)
        self.assertEqual(self.box.selected_graph(303), SHOPPING)

    def test_registration_and_grants_survive_restart_but_empty_seed_allowlist_disables_access(self):
        self.register()
        restored = Inbox(self.root, "101,202", **self.settings)
        self.addCleanup(restored.db.close)
        self.assertIn(303, restored.allowed)
        self.assertEqual(restored.selected_graph(303), SHOPPING)
        disabled = Inbox(self.root, "", **self.settings)
        self.addCleanup(disabled.db.close)
        self.assertFalse(disabled.allowed)

    def test_delivery_rechecks_grants_and_owners_current_graph_access(self):
        self.register()
        record = {"submitted_by": 303, "graph_id": SHOPPING}
        self.request.return_value = {"graphs": [{"id": PRIMARY, "name": "Training"}]}
        with self.assertRaises(ValueError):
            self.box.graph_credentials(record)
        self.request.return_value = self.graphs
        self.box.registration_callback(self.callback(action="drop"))
        with self.assertRaises(ValueError):
            self.box.graph_credentials(record)
        self.box.remember([update(2, text="Not granted anywhere")])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)

    def test_granting_second_graph_and_revoking_selected_graph_changes_only_future_selection(self):
        self.register(graph=PRIMARY)
        self.box.remember([update(2, text="Pinned training note")])
        self.box.registration_callback(self.callback(graph=SHOPPING))
        self.box.registration_callback(self.callback(action="drop", graph=PRIMARY))
        self.assertEqual(self.box.selected_graph(303), SHOPPING)
        record = json.loads(self.box.db.execute("SELECT record FROM submissions WHERE id=2").fetchone()[0])
        self.assertEqual(record["graph_id"], PRIMARY)
        with self.assertRaises(ValueError):
            self.box.graph_credentials(record)

    def test_disable_persists_and_blocks_queued_delivery_even_in_legacy_email_free_mode(self):
        self.register()
        self.box.registration_callback(self.callback(action="deny", graph=None))
        self.assertNotIn(303, self.box.allowed)
        self.box.logseq_users = {}
        with self.assertRaises(ValueError):
            self.box.graph_credentials({"submitted_by": 303, "graph_id": SHOPPING})
        self.box.remember([update(2)])
        self.assertEqual(self.box.db.execute("SELECT status FROM bot_users WHERE user_id=303").fetchone()[0], "denied")
        restored = Inbox(self.root, "101,202,303", **self.settings)
        self.addCleanup(restored.db.close)
        self.assertNotIn(303, restored.allowed)

    def test_graph_discovery_failure_cannot_grant_access(self):
        self.box.remember([update()])
        self.request.side_effect = OSError("unavailable")
        self.box.registration_callback(self.callback())
        self.assertNotIn(303, self.box.allowed)

    def test_callback_sizes_fit_telegram_limit_with_large_user_id(self):
        self.box.remember([update(user=9007199254740991)])
        panel = self.box.registration_panel()
        self.assertTrue(all(len(button["callback_data"].encode()) <= 64
                            for row in panel["reply_markup"]["inline_keyboard"] for button in row))

    def test_users_command_is_owner_only_and_never_becomes_a_note(self):
        self.box.remember([update(), update(2, user=202, text="/users"), update(3, user=101, text="/users")])
        messages = [call.args[1] for call in self.telegram.call_args_list if call.args[0] == "sendMessage"]
        self.assertIn("Only", messages[-2]["text"])
        self.assertIn("reply_markup", messages[-1])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
