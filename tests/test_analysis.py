import base64
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from src.analysis import Analyzer, FIELDS, MAX_VIDEO_BYTES, generate, limit_download_size, render_analysis, validate_result, video_part


def result(needs_video=False):
    exercise = {field: None for field in FIELDS}
    exercise.update(name="Squat", reps="10", evidence="Squats: 10 reps")
    return {"needs_video": needs_video, "summary": "Squats", "transcript": None, "exercises": [exercise]}


def record(duration=30):
    return {"id": 1, "original_text": "https://www.youtube.com/watch?v=BaW_jenozKc", "attachment": None,
            "media_status": "none", "analysis": {"status": "pending", "exercises": [], "transcript": None},
            "descriptions": [{"url": "https://www.youtube.com/watch?v=BaW_jenozKc", "status": "empty", "duration_seconds": duration}]}


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(Path(self.temp.name) / "inbox.sqlite")
        self.key = Path(self.temp.name) / "gemini-key"
        self.key.write_text("FAKE_TEST_KEY")
        self.ai = Analyzer(self.db, self.temp.name, enabled=True, key_file=str(self.key))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_disabled_and_missing_key_never_make_requests(self):
        self.ai.enabled = False
        with patch("src.analysis.generate") as api:
            self.ai.step(record())
            self.ai.enabled = True
            self.key.write_text("")
            self.assertEqual(self.ai.step(record())["status"], "waiting_for_key")
        api.assert_not_called()

    def test_caption_breakdown_avoids_video_and_renders_missing_fields(self):
        item = record(None)
        item["descriptions"][0].update(status="copied", text="Squats: 10 reps")
        with patch("src.analysis.generate", return_value=result()) as api, patch("src.analysis.video_part") as video:
            analysis = self.ai.step(item)
        self.assertEqual(analysis["status"], "completed")
        self.assertEqual(analysis["source"], "caption")
        self.assertTrue(analysis["needs_review"])
        self.assertIn("sets: not specified", render_analysis(analysis))
        self.assertEqual(api.call_args.args[0], "gemini-3.5-flash-lite")
        video.assert_not_called()

    def test_caption_fallback_uses_a_separate_video_step(self):
        item = record(60)
        item["descriptions"][0].update(status="copied", text="Try this workout!")
        with patch("src.analysis.generate", side_effect=[result(True), result(False)]) as api:
            item["analysis"] = self.ai.step(item)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(item["analysis"]["status"], "pending_video")
            final = self.ai.step(item)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(final["source"], "video")
        self.assertEqual(api.call_args.args[0], "gemini-3.5-flash-lite")

    def test_duration_boundaries_block_video_download_and_api_without_consent(self):
        for duration, expected in ((120.01, "needs_approval"), (600, "needs_approval"), (600.01, "too_long")):
            with self.subTest(duration=duration), patch("src.analysis.generate") as api, patch("src.analysis.video_part") as video:
                self.assertEqual(self.ai.step(record(duration))["status"], expected)
                api.assert_not_called()
                video.assert_not_called()
        with patch("src.analysis.generate", return_value=result()) as api:
            self.assertEqual(self.ai.step(record(120))["status"], "completed")
        self.assertEqual(api.call_count, 1)

    def test_unknown_duration_is_accepted_with_a_two_minute_submission_boundary(self):
        for duration in (None, 0, float("nan"), True):
            with self.subTest(duration=duration), patch("src.analysis.generate", return_value=result()) as api:
                analyzed = self.ai.step(record(duration))
                self.assertEqual(analyzed["status"], "completed")
                self.assertTrue(analyzed["duration_unverified"])
                self.assertIn("Duration not verified", render_analysis(analyzed))
                if api.called:
                    video = api.call_args.args[1][0]
                    self.assertEqual(video["processing"]["end_offset"], "120s")

    def test_approval_allows_ten_minutes_but_cannot_bypass_hard_limit(self):
        with patch("src.analysis.generate", return_value=result()) as api:
            item = record(600)
            item["analysis"]["approved"] = True
            self.assertEqual(self.ai.step(item)["status"], "completed")
            too_long = record(601)
            too_long["analysis"]["approved"] = True
            self.assertEqual(self.ai.step(too_long)["status"], "too_long")
        self.assertEqual(api.call_count, 1)

    def test_video_cache_survives_restart_and_does_not_consume_quota_on_reuse(self):
        with patch("src.analysis.generate", return_value=result()) as api:
            self.ai.step(record())
            restarted = Analyzer(self.db, self.temp.name, True, str(self.key))
            self.assertTrue(restarted.step(record())["cached"])
        self.assertEqual(api.call_count, 1)
        self.assertEqual(self.db.execute("SELECT SUM(videos) FROM ai_usage").fetchone()[0], 1)

    def test_daily_limit_is_shared_and_resets_at_utc_midnight(self):
        now = 1700000000
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self.db:
            self.db.execute("INSERT INTO ai_usage VALUES (?,10)", (day,))
        with patch("src.analysis.time.time", return_value=now), patch("src.analysis.generate", return_value=result()) as api:
            blocked = self.ai.step(record())
            self.assertEqual(blocked["status"], "daily_limit")
            api.assert_not_called()
        with patch("src.analysis.time.time", return_value=blocked["retry_after"]), patch("src.analysis.generate", return_value=result()) as api:
            self.assertEqual(self.ai.step(record())["status"], "completed")
            self.assertEqual(api.call_count, 1)

    def test_quota_429_backs_off_globally_and_stops_after_three_attempts(self):
        error = HTTPError("https://generativelanguage.googleapis.com/v1beta/interactions", 429, "quota", None, None)
        with patch("src.analysis.generate", side_effect=error) as api:
            for index in range(3):
                with patch("src.analysis.time.time", return_value=1700000000 + index * 901):
                    response = self.ai.step(record())
                    self.assertEqual(response["status"], "quota_wait" if index < 2 else "needs_review")
                    other = record()
                    other["descriptions"][0]["url"] = "https://www.youtube.com/watch?v=otherVideo1"
                    self.assertEqual(self.ai.step(other)["status"], "quota_wait")
            self.assertEqual(api.call_count, 3)
        error.close()

    def test_timeout_or_crash_is_not_automatically_repeated(self):
        with patch("src.analysis.generate", side_effect=TimeoutError) as api:
            self.assertEqual(self.ai.step(record())["status"], "needs_review")
            self.assertEqual(self.ai.step(record())["status"], "needs_review")
        self.assertEqual(api.call_count, 1)
        self.db.execute("DELETE FROM ai_cache")
        identifier, _ = self.ai.cached("video", [record()["descriptions"][0]["url"], 30])
        with self.db:
            self.db.execute("INSERT INTO ai_cache VALUES (?,'started',NULL,1,0)", (identifier,))
        with patch("src.analysis.generate") as api:
            self.assertEqual(self.ai.step(record())["status"], "needs_review")
        api.assert_not_called()

    def test_provider_503_waits_and_retries_without_hammering_the_api(self):
        error = HTTPError('https://generativelanguage.googleapis.com/v1beta/interactions', 503, 'unavailable', None, None)
        with patch('src.analysis.time.time', return_value=1700000000), patch('src.analysis.generate', side_effect=error) as api:
            item = record()
            item['analysis'] = self.ai.step(item)
            self.assertEqual(item['analysis']['status'], 'retry_wait')
            self.ai.step(item)
            self.assertEqual(api.call_count, 1)
        with patch('src.analysis.time.time', return_value=1700000031), patch('src.analysis.generate', return_value=result()) as api:
            self.assertEqual(self.ai.step(item)['status'], 'completed')
            self.assertEqual(api.call_count, 1)
        error.close()

    def test_multiple_videos_are_saved_without_guessing_which_to_analyze(self):
        item = record()
        item["descriptions"][0]["video_count"] = 2
        with patch("src.analysis.generate") as api:
            self.assertEqual(self.ai.step(item)["status"], "multiple_videos")
        api.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_interactions_request_bounds_output_keeps_key_out_of_url_and_validates_json(self):
        response = {"status": "completed", "steps": [{"type": "model_output", "content": [{"type": "text", "text": json.dumps(result())}]}]}
        with patch("src.analysis.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as http:
            self.assertEqual(generate("gemini-3.5-flash-lite", [{"type": "text", "text": "Squats: 10 reps"}], "FAKE_TEST_KEY"), result())
        request = http.call_args.args[0]
        self.assertNotIn("FAKE_TEST_KEY", request.full_url)
        payload = json.loads(request.data)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["generation_config"]["max_output_tokens"], 2048)
        self.assertEqual(payload["generation_config"]["thinking_level"], "minimal")
        self.assertNotIn("tools", payload)
        with patch("src.analysis.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as http:
            generate("gemini-3.8-flash", [{"type": "text", "text": "Squats"}], "FAKE_TEST_KEY")
        self.assertEqual(json.loads(http.call_args.args[0].data)["generation_config"]["thinking_level"], "low")
        for invalid in ({"status": "incomplete"}, [], {"status": "completed", "steps": []}):
            with patch("src.analysis.urlopen", return_value=io.BytesIO(json.dumps(invalid).encode())):
                with self.assertRaises(ValueError):
                    generate("model", [], "FAKE_TEST_KEY")

    def test_wrong_types_or_invented_extra_fields_are_rejected(self):
        for change in ({"needs_video": "false"}, {"summary": None}, {"exercises": ["squats"]}, {"extra": "field"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_result({**result(), **change})

    def test_downloaded_video_has_kernel_size_limit_and_temporary_file_is_removed(self):
        paths = []

        def download(command, **kwargs):
            self.assertIn("preexec_fn", kwargs)
            path = Path(command[command.index("-o") + 1])
            paths.append(path)
            path.write_bytes(b"test-video")

        with patch("src.analysis.subprocess.run", side_effect=download):
            part = video_part({"kind": "instagram", "url": "https://www.instagram.com/reel/ABC/"}, "/tmp")
        self.assertEqual(base64.b64decode(part["data"]), b"test-video")
        self.assertFalse(paths[0].exists())
        self.assertEqual(part["processing"], {"type": "static", "fps": 1, "end_offset": "120s"})

    def test_oversized_attachment_is_not_read_or_sent(self):
        with tempfile.TemporaryDirectory() as folder:
            media = Path(folder) / "media"
            media.mkdir()
            with (media / "1").open("wb") as stream:
                stream.truncate(MAX_VIDEO_BYTES + 1)
            with self.assertRaises(ValueError):
                video_part({"kind": "attachment", "id": 1, "mime_type": "video/mp4"}, folder)

    def test_kernel_limit_stops_a_downloader_even_when_content_length_is_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "oversized.mp4"
            command = [sys.executable, "-c", "import sys; f=open(sys.argv[1],'wb'); f.write(b'x'*(20*1024*1024+1)); f.write(b'x'); f.close()", str(path)]
            child = subprocess.run(command, preexec_fn=limit_download_size, capture_output=True)
            self.assertNotEqual(child.returncode, 0)
            self.assertEqual(path.stat().st_size, MAX_VIDEO_BYTES)


if __name__ == "__main__":
    unittest.main()
