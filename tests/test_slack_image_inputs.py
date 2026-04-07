import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import slack_image_inputs


class DummyResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class SlackImageInputParsingTests(unittest.TestCase):
    def test_extract_candidate_files_merges_event_and_nested_message(self):
        payload = {
            "event": {
                "files": [
                    {"id": "F1", "name": "a.png"},
                    {"id": "F2", "name": "b.jpg"},
                ],
                "message": {
                    "files": [
                        {"id": "F2", "name": "duplicate.jpg"},
                        {"id": "F3", "name": "c.gif"},
                    ]
                },
            }
        }
        files = slack_image_inputs.extract_candidate_files(payload)
        self.assertEqual([item["id"] for item in files], ["F1", "F2", "F3"])

    def test_extract_candidate_files_supports_direct_event_object(self):
        payload = {
            "files": [{"id": "F9", "name": "x.png"}],
            "message": {"files": [{"id": "F10", "name": "y.png"}]},
        }
        files = slack_image_inputs.extract_candidate_files(payload)
        self.assertEqual([item["id"] for item in files], ["F9", "F10"])

    def test_is_image_like_file_checks_multiple_signals(self):
        self.assertTrue(slack_image_inputs.is_image_like_file({"mimetype": "image/png"}))
        self.assertTrue(slack_image_inputs.is_image_like_file({"filetype": "jpeg"}))
        self.assertTrue(slack_image_inputs.is_image_like_file({"name": "photo.webp"}))
        self.assertFalse(slack_image_inputs.is_image_like_file({"name": "notes.txt"}))
        self.assertFalse(slack_image_inputs.is_image_like_file({"mimetype": "image/png", "is_external": True}))

    def test_choose_download_url_prefers_private_download(self):
        file_obj = {
            "url_private_download": "https://files.slack.com/files-pri/T/F/a.png",
            "url_private": "https://files.slack.com/files-pri/T/F/b.png",
        }
        self.assertEqual(
            slack_image_inputs.choose_download_url(file_obj),
            "https://files.slack.com/files-pri/T/F/a.png",
        )

    def test_choose_download_url_rejects_invalid_or_insecure_urls(self):
        file_obj = {
            "url_private_download": "http://files.slack.com/insecure.png",
            "url_private": "https://files.slack.com/safe.png",
        }
        self.assertEqual(
            slack_image_inputs.choose_download_url(file_obj),
            "https://files.slack.com/safe.png",
        )
        self.assertIsNone(slack_image_inputs.choose_download_url({"url_private": "not-a-url"}))

    def test_choose_download_filename_sanitizes_and_preserves_extension(self):
        file_obj = {"name": "../../bad name?.PNG", "id": "F1", "mimetype": "image/png"}
        self.assertEqual(slack_image_inputs.choose_download_filename(file_obj), "bad_name.png")

    def test_choose_download_filename_falls_back_to_mimetype_hint(self):
        file_obj = {"id": "F2", "name": "noext", "mimetype": "image/webp"}
        self.assertEqual(slack_image_inputs.choose_download_filename(file_obj), "noext.webp")

    def test_build_image_downloads_from_event_filters_non_images_and_missing_urls(self):
        payload = {
            "event": {
                "files": [
                    {
                        "id": "F1",
                        "name": "a.png",
                        "mimetype": "image/png",
                        "url_private_download": "https://files.slack.com/a.png",
                    },
                    {
                        "id": "F2",
                        "name": "notes.txt",
                        "mimetype": "text/plain",
                        "url_private_download": "https://files.slack.com/notes.txt",
                    },
                    {
                        "id": "F3",
                        "name": "b.jpg",
                        "mimetype": "image/jpeg",
                    },
                ]
            }
        }
        downloads = slack_image_inputs.build_image_downloads_from_event(payload)
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0].file_id, "F1")
        self.assertEqual(downloads[0].filename, "a.png")


class SlackImageDownloadTests(unittest.TestCase):
    def test_download_slack_image_files_writes_files_and_sets_auth_header(self):
        requests = []
        payloads = [b"img-1", b"img-2"]

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return DummyResponse(payloads.pop(0))

        downloads = [
            slack_image_inputs.SlackImageDownload(
                file_id="F1",
                filename="dup.png",
                download_url="https://files.slack.com/a.png",
                mimetype="image/png",
            ),
            slack_image_inputs.SlackImageDownload(
                file_id="F2",
                filename="dup.png",
                download_url="https://files.slack.com/b.png",
                mimetype="image/png",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("slack_image_inputs.urlopen", side_effect=fake_urlopen):
                paths = slack_image_inputs.download_slack_image_files(
                    downloads,
                    bot_token="xoxb-test",
                    download_dir=tmpdir,
                    timeout_seconds=5,
                )

            self.assertEqual(len(paths), 2)
            self.assertEqual(paths[0].name, "dup.png")
            self.assertEqual(paths[1].name, "dup-2.png")
            self.assertEqual(paths[0].read_bytes(), b"img-1")
            self.assertEqual(paths[1].read_bytes(), b"img-2")

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][1], 5)
        self.assertEqual(requests[0][0].headers.get("Authorization"), "Bearer xoxb-test")

    def test_download_slack_image_files_requires_token(self):
        with self.assertRaisesRegex(RuntimeError, "Missing Slack bot token"):
            slack_image_inputs.download_slack_image_files(
                [
                    slack_image_inputs.SlackImageDownload(
                        file_id="F1",
                        filename="a.png",
                        download_url="https://files.slack.com/a.png",
                    )
                ],
                bot_token="",
            )

    def test_download_slack_image_files_cleans_partial_files_on_failure(self):
        calls = {"count": 0}

        def fake_urlopen(_request, timeout=0):
            calls["count"] += 1
            if calls["count"] == 1:
                return DummyResponse(b"ok")
            raise OSError("network failed")

        downloads = [
            slack_image_inputs.SlackImageDownload(
                file_id="F1",
                filename="a.png",
                download_url="https://files.slack.com/a.png",
            ),
            slack_image_inputs.SlackImageDownload(
                file_id="F2",
                filename="b.png",
                download_url="https://files.slack.com/b.png",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("slack_image_inputs.urlopen", side_effect=fake_urlopen):
                with self.assertRaisesRegex(RuntimeError, "Failed downloading Slack image F2"):
                    slack_image_inputs.download_slack_image_files(
                        downloads,
                        bot_token="xoxb-test",
                        download_dir=tmpdir,
                    )
            self.assertEqual(list(Path(tmpdir).iterdir()), [])

    def test_cleanup_downloaded_files_ignores_missing_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "x.png"
            path.write_bytes(b"data")
            slack_image_inputs.cleanup_downloaded_files([path, Path(tmpdir) / "missing.png"])
            self.assertFalse(path.exists())

    def test_cleanup_download_directory_removes_tree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = Path(tmpdir) / "nested"
            nested.mkdir()
            (nested / "x.txt").write_text("ok", encoding="utf-8")
            slack_image_inputs.cleanup_download_directory(nested)
            self.assertFalse(nested.exists())


if __name__ == "__main__":
    unittest.main()
