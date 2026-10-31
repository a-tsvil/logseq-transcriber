import io
from contextlib import nullcontext
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.image_search import (DIMENSIONS, ImageIndex, embed, image_mime, message_link,
                              normalize_vector, render_matches, strict_matches, validate_description, validate_matches)
from src.inbox import Inbox


def vector(first=1, second=0):
    return [first, second] + [0] * (DIMENSIONS - 2)


class ImageSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = Path(self.temp.name) / "key"
        self.key.write_text("FAKE_TEST_KEY")
        self.index = ImageIndex(self.temp.name, self.key)
        checker = patch("src.image_search.strict_matches", return_value=[0])
        self.checker = checker.start()
        self.addCleanup(checker.stop)

    def add(self, data=b"\xff\xd8\xffcat", chat=-1000000000123, message=1, owner=101):
        return self.index.add(owner, chat, message, "Memes", data)

    def test_indexes_description_text_and_references_without_saving_image_bytes(self):
        with patch("src.image_search.generate", return_value={"description": "Cat in overalls", "text": "Back to work"}) as generate, \
                patch("src.image_search.embed", return_value=vector()) as embeddings:
            self.assertEqual(self.add(), "ready")
        self.assertEqual(generate.call_args.args[1][0]["type"], "image")
        self.assertIs(generate.call_args.kwargs["validate"], validate_description)
        self.assertEqual(embeddings.call_args.args, ("Cat in overalls\nText: Back to work", "FAKE_TEST_KEY", "RETRIEVAL_DOCUMENT"))
        with self.index.connect() as db:
            row = db.execute("SELECT state,description FROM image_assets").fetchone()
            self.assertEqual(tuple(row), ("ready", "Cat in overalls\nText: Back to work"))
            self.assertEqual(db.execute("SELECT owner_id,chat_id,message_id FROM image_messages").fetchone()[:], (101, -1000000000123, 1))
        self.assertEqual({p.name for p in Path(self.temp.name).iterdir()}, {"key", "image-index.sqlite"})
        self.assertNotIn(b"\xff\xd8\xffcat", self.index.path.read_bytes())

    def test_duplicate_image_reuses_analysis_but_preserves_each_message_reference(self):
        with patch("src.image_search.generate", return_value={"description": "Cat", "text": ""}) as generate, \
                patch("src.image_search.embed", return_value=vector()) as embeddings:
            self.add()
            self.add(chat=-1000000000456, message=2)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(embeddings.call_count, 1)
        with self.index.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM image_assets").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM image_messages").fetchone()[0], 2)

    def test_uncertain_request_is_not_retried_without_explicit_flag(self):
        with patch("src.image_search.generate", side_effect=TimeoutError) as generate:
            with self.assertRaises(TimeoutError):
                self.add()
            self.assertEqual(self.add(), "failed")
            self.assertTrue(self.index.seen(101, -1000000000123, 1))
            self.assertFalse(self.index.seen(101, -1000000000123, 1, retry_failed=True))
            generate.assert_called_once()
        with patch("src.image_search.generate", return_value={"description": "Cat", "text": ""}), \
                patch("src.image_search.embed", return_value=vector()):
            self.assertEqual(self.index.add(101, -1000000000123, 1, "Memes", b"\xff\xd8\xffcat", retry_failed=True), "ready")

    def test_interrupted_reservation_is_not_automatically_repeated(self):
        with patch("src.image_search.generate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.add()
        with patch("src.image_search.generate") as generate:
            self.assertEqual(self.add(), "started")
            generate.assert_not_called()

    def test_embedding_retry_reuses_completed_vision_description(self):
        with patch("src.image_search.generate", return_value={"description": "Cat", "text": ""}) as generate, \
                patch("src.image_search.embed", side_effect=[TimeoutError, vector()]):
            with self.assertRaises(TimeoutError):
                self.add()
            self.assertEqual(self.index.add(101, -1000000000123, 1, "Memes", b"\xff\xd8\xffcat", retry_failed=True), "ready")
        generate.assert_called_once()

    def test_search_ranks_by_meaning_caches_query_and_excludes_other_owners(self):
        with patch("src.image_search.generate", side_effect=[
                {"description": "Beach", "text": ""}, {"description": "Factory cat", "text": ""},
                {"description": "Other owner's private image", "text": ""}]), \
                patch("src.image_search.embed", side_effect=[vector(0, 1), vector(), vector()]):
            self.add(data=b"\xff\xd8\xffbeach", message=1)
            self.add(message=2)
            self.add(data=b"\xff\xd8\xffprivate", message=3, owner=202)
        with patch("src.image_search.embed", return_value=vector()) as embeddings:
            results = self.index.search(101, "cat dressed as a worker")
            self.assertEqual([r["message_id"] for r in results], [2])
            self.assertEqual(self.index.search(101, "cat dressed as a worker"), results)
            embeddings.assert_called_once_with("cat dressed as a worker", "FAKE_TEST_KEY", "RETRIEVAL_QUERY")
        self.assertEqual(results[0]["link"], "https://t.me/c/123/2")
        self.assertIn("Matching images", render_matches(results))
        self.assertEqual(self.checker.call_count, 1)

    def test_no_cat_match_returns_nothing_even_when_embeddings_are_identical(self):
        with patch("src.image_search.generate", return_value={"description": "A dog at the beach", "text": ""}), \
                patch("src.image_search.embed", return_value=vector()):
            self.add()
        self.checker.return_value = []
        with patch("src.image_search.embed", return_value=vector()):
            self.assertEqual(self.index.search(101, "cat"), [])
            self.assertEqual(self.index.search(101, "cat"), [])
        self.assertEqual(self.checker.call_count, 1)
        self.assertIn("No matching images", render_matches([]))

    def test_new_indexed_images_invalidate_cached_empty_results(self):
        with patch("src.image_search.generate", return_value={"description": "Dog", "text": ""}), \
                patch("src.image_search.embed", return_value=vector()):
            self.add()
        self.checker.return_value = []
        with patch("src.image_search.embed", return_value=vector()):
            self.assertEqual(self.index.search(101, "cat"), [])
        with patch("src.image_search.generate", return_value={"description": "Cat", "text": ""}), \
                patch("src.image_search.embed", return_value=vector()):
            self.add(data=b"\xff\xd8\xffsecond", message=2)
        self.checker.return_value = [0]
        self.assertEqual(len(self.index.search(101, "cat")), 1)
        self.assertEqual(self.checker.call_count, 2)

    def test_excluded_stickers_are_not_searchable_or_deliverable(self):
        with patch("src.image_search.generate", return_value={"description": "Cat sticker", "text": ""}), \
                patch("src.image_search.embed", return_value=vector()):
            self.add()
        match = {"chat_id": -1000000000123, "message_id": 1}
        token = self.index.keyboard(101, [match])["inline_keyboard"][0][0]["callback_data"][6:]
        self.index.queue_image(token, 101, "one")
        self.index.exclude_sticker(101, -1000000000123, 1)
        self.assertEqual(self.index.search(101, "cat"), [])
        self.assertIsNone(self.index.claim_image(101))
        self.assertIn("unavailable", self.index.queue_image(token, 101, "two"))
        self.checker.assert_not_called()

    def test_strict_filter_validates_model_indices_and_does_not_accept_loose_fallback(self):
        with patch("src.image_search.generate", return_value={"matches": []}) as generate:
            self.assertEqual(strict_matches("model", "TEST_KEY", "cat", ["A dog with OCR Text: cat"]), [])
        self.assertEqual(generate.call_args.kwargs["timeout"], 20)
        self.assertIn("positive evidence", generate.call_args.kwargs["prompt"])
        for bad in ({}, {"matches": [True]}, {"matches": [0, 0]}, {"matches": [1]}, {"matches": [-1]}):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                validate_matches(bad, 1)

    def test_legacy_database_adds_exclusion_flag_without_discarding_indexed_references(self):
        directory = Path(self.temp.name) / "legacy"
        directory.mkdir()
        db = sqlite3.connect(directory / "image-index.sqlite")
        db.execute("CREATE TABLE image_messages (owner_id INTEGER,chat_id INTEGER,message_id INTEGER,profile TEXT,asset_id TEXT,chat_title TEXT,PRIMARY KEY(owner_id,chat_id,message_id,profile))")
        db.execute("INSERT INTO image_messages VALUES (101,-123,1,?,'asset','Memes')", (self.index.profile,))
        db.commit()
        db.close()
        legacy = ImageIndex(directory, self.key)
        with legacy.connect() as db:
            row = db.execute("SELECT message_id,excluded FROM image_messages").fetchone()
            self.assertEqual(tuple(row), (1, 0))

    def test_empty_owner_index_does_not_call_provider(self):
        with patch("src.image_search.embed") as embeddings:
            self.assertEqual(self.index.search(999, "cat"), [])
            embeddings.assert_not_called()

    def test_model_changes_do_not_reuse_incompatible_vectors(self):
        with patch("src.image_search.generate", return_value={"description": "Cat", "text": ""}), \
                patch("src.image_search.embed", return_value=vector()):
            self.add()
        other = ImageIndex(self.temp.name, self.key, "different-vision-model")
        self.assertFalse(other.seen(101, -1000000000123, 1))
        with patch("src.image_search.embed") as embeddings:
            self.assertEqual(other.search(101, "cat"), [])
            embeddings.assert_not_called()

    def test_embedding_transport_limits_response_and_validates_finite_normalized_vector(self):
        response = io.BytesIO(json.dumps({"embedding": {"values": vector(3, 4)}}).encode())
        with patch("src.image_search.urlopen", return_value=response) as request:
            values = embed("Cat", "FAKE_TEST_KEY", "RETRIEVAL_QUERY")
        self.assertEqual(values[:2], [0.6, 0.8])
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload["taskType"], "RETRIEVAL_QUERY")
        self.assertEqual(payload["outputDimensionality"], DIMENSIONS)
        self.assertNotIn("embedContentConfig", payload)
        with patch("src.image_search.urlopen", return_value=io.BytesIO(b"x" * (128 * 1024 + 1))):
            with self.assertRaisesRegex(ValueError, "Oversized"):
                embed("Cat", "FAKE_TEST_KEY", "RETRIEVAL_QUERY")
        for invalid in ([], vector(0), vector(float("nan")), vector(float("inf")), [True] * DIMENSIONS):
            with self.subTest(invalid=invalid[:2]), self.assertRaises(ValueError):
                normalize_vector(invalid)

    def test_image_description_and_query_validation(self):
        self.assertEqual(image_mime(b"\x89PNG\r\n\x1a\nimage"), "image/png")
        self.assertEqual(image_mime(b"RIFF1234WEBPimage"), "image/webp")
        for invalid in (b"", b"not an image", b"x" * (10 * 1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                image_mime(invalid)
        for invalid in ({}, {"description": "", "text": ""}, {"description": "Cat", "text": 123}):
            with self.assertRaises(ValueError):
                validate_description(invalid)
        for query in ("", " " * 10, "x" * 501):
            with self.assertRaises(ValueError):
                self.index.search(101, query)
        self.assertIsNone(message_link(202, 1))
        self.assertIsNone(message_link(-123, 1))
        self.assertIsNone(message_link(-100123, 1))


class SearchCommandTests(unittest.TestCase):
    def setUp(self):
        activity = patch("src.inbox.telegram_activity", side_effect=lambda *args, **kwargs: nullcontext())
        activity.start()
        self.addCleanup(activity.stop)

    def test_poll_handles_one_owner_search_and_preserves_later_update_for_next_pass(self):
        with tempfile.TemporaryDirectory() as data:
            inbox = Inbox(data, "101", admin_id="101", image_search_enabled=True)
            self.addCleanup(inbox.db.close)
            updates = [{"update_id": number, "message": {"from": {"id": 101},
                       "chat": {"id": 101, "type": "private"}, "text": "/search cat"}} for number in (1, 2)]
            with patch.object(inbox, "search_images", return_value="Matches") as search:
                inbox.remember(updates)
                self.assertEqual(search.call_count, 1)
                self.assertEqual(inbox.db.execute("SELECT offset FROM state").fetchone()[0], 2)
                inbox.remember(updates[1:])
                self.assertEqual(search.call_count, 2)
                self.assertEqual(inbox.db.execute("SELECT offset FROM state").fetchone()[0], 3)

    def test_search_is_owner_only_private_never_exported_and_independent_of_logseq(self):
        with tempfile.TemporaryDirectory() as data:
            inbox = Inbox(data, "101,202", bot_token="test", admin_id="101", image_search_enabled=True,
                          logseq_users='{"202":"other@example.com"}')
            self.addCleanup(inbox.db.close)
            self.assertIn("search", [c["command"] for c in inbox.commands(101)])
            self.assertNotIn("search", [c["command"] for c in inbox.commands(202)])
            def update(number, sender, chat_type="private"):
                return {"update_id": number, "message": {"from": {"id": sender}, "chat": {"id": sender, "type": chat_type},
                        "text": "/search factory cat", "message_id": number, "date": 1700000000}}
            with patch("src.inbox.ImageIndex") as index, patch.object(inbox, "telegram") as telegram:
                index.return_value.search.return_value = []
                inbox.remember([update(1, 101), update(2, 202), update(3, 999), update(4, 101, "group")])
                index.return_value.search.assert_called_once_with(101, "factory cat")
                self.assertEqual(telegram.call_count, 2)
                self.assertIn("only", telegram.call_args.args[1]["text"])
            self.assertEqual(inbox.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)

    def test_disabled_search_and_provider_failure_do_not_leak_secrets(self):
        with tempfile.TemporaryDirectory() as data:
            inbox = Inbox(data, "101", admin_id="101")
            self.addCleanup(inbox.db.close)
            with patch("src.inbox.ImageIndex") as index:
                self.assertIn("disabled", inbox.search_images("/search cat", 101))
                index.assert_not_called()
                inbox.image_search_enabled = True
                self.assertIn("Use /search", inbox.search_images("/search", 101))
                index.return_value.search.side_effect = OSError("private token")
                result = inbox.search_images("/search cat", 101)
                self.assertIn("failed", result)
                self.assertNotIn("private token", result)


if __name__ == "__main__":
    unittest.main()
