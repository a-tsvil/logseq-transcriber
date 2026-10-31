from threading import Event
import unittest
from unittest.mock import Mock, patch

from src.activity import telegram_activity
from src.image_search import render_matches


class ActivityTests(unittest.TestCase):
    def test_activity_marks_working_and_clears_reaction_after_completion(self):
        ready = Event()
        calls = []
        def send(method, payload):
            calls.append((method, payload))
            if method == "sendChatAction":
                ready.set()
        with telegram_activity(send, 101, 55):
            self.assertTrue(ready.wait(1))
        self.assertEqual(calls[0], ("setMessageReaction", {"chat_id": 101, "message_id": 55, "reaction": [{"type": "emoji", "emoji": "👀"}]}))
        self.assertEqual(calls[-1], ("setMessageReaction", {"chat_id": 101, "message_id": 55, "reaction": []}))

    def test_activity_refreshes_before_telegram_status_expires(self):
        event = Mock()
        event.is_set.side_effect = [False, False, True]
        send = Mock()
        with patch("src.activity.Event", return_value=event):
            with telegram_activity(send, 101, action="upload_photo"):
                pass
        self.assertEqual(send.call_count, 2)
        self.assertTrue(all(call.args == ("sendChatAction", {"chat_id": 101, "action": "upload_photo"}) for call in send.call_args_list))
        self.assertEqual([call.args for call in event.wait.call_args_list], [(4,), (4,)])

    def test_feedback_failure_does_not_hide_task_exception_and_cleanup_runs(self):
        event = Mock()
        event.is_set.side_effect = [False, True]
        send = Mock(side_effect=OSError("unavailable"))
        with patch("src.activity.Event", return_value=event):
            with self.assertRaisesRegex(ValueError, "task failed"):
                with telegram_activity(send, 101, 55):
                    raise ValueError("task failed")
        self.assertEqual(send.call_args.args[1]["reaction"], [])

    def test_search_summary_hides_descriptions_and_internal_references(self):
        text = render_matches([{"description": "Very long debug details", "chat_id": -452019584, "message_id": 123}])
        self.assertIn("Matching images: 1", text)
        self.assertNotIn("debug", text)
        self.assertNotIn("452019584", text)
