import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import slack_document_inputs


class DummyResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class SlackDocumentInputParsingTests(unittest.TestCase):
    def test_is_document_like_file_checks_multiple_signals(self):
        self.assertTrue(slack_document_inputs.is_document_like_file({"mimetype": "text/plain"}))
        self.assertTrue(slack_document_inputs.is_document_like_file({"mimetype": "application/pdf"}))
        self.assertTrue(slack_document_inputs.is_document_like_file({"mimetype": "text/x-julia"}))
        self.assertTrue(slack_document_inputs.is_document_like_file({"mimetype": "application/x-ipynb+json"}))
        self.assertTrue(
            slack_document_inputs.is_document_like_file(
                {"mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
            )
        )
        self.assertTrue(slack_document_inputs.is_document_like_file({"name": "notes.md"}))
        self.assertTrue(slack_document_inputs.is_document_like_file({"name": "analysis.jl"}))
        self.assertTrue(slack_document_inputs.is_document_like_file({"name": "notebook.ipynb"}))
        self.assertTrue(slack_document_inputs.is_document_like_file({"filetype": "julia"}))
        self.assertTrue(slack_document_inputs.is_document_like_file({"filetype": "notebook"}))
        self.assertFalse(slack_document_inputs.is_document_like_file({"name": "photo.png", "mimetype": "image/png"}))
        self.assertFalse(slack_document_inputs.is_document_like_file({"name": "archive.zip"}))

    def test_build_document_downloads_from_event_filters_supported_documents(self):
        payload = {
            "event": {
                "files": [
                    {
                        "id": "F1",
                        "name": "report.pdf",
                        "mimetype": "application/pdf",
                        "url_private_download": "https://files.slack.com/report.pdf",
                    },
                    {
                        "id": "F2",
                        "name": "photo.png",
                        "mimetype": "image/png",
                        "url_private_download": "https://files.slack.com/photo.png",
                    },
                    {
                        "id": "F3",
                        "name": "archive.zip",
                        "mimetype": "application/zip",
                        "url_private_download": "https://files.slack.com/archive.zip",
                    },
                ]
            }
        }
        downloads = slack_document_inputs.build_document_downloads_from_event(payload)
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0].file_id, "F1")
        self.assertEqual(downloads[0].filename, "report.pdf")

    def test_choose_download_filename_falls_back_to_docx_hint(self):
        file_obj = {
            "id": "F1",
            "name": "proposal",
            "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        self.assertEqual(slack_document_inputs.choose_download_filename(file_obj), "proposal.docx")

    def test_choose_download_filename_uses_julia_and_notebook_hints(self):
        self.assertEqual(
            slack_document_inputs.choose_download_filename({"id": "F1", "name": "script", "filetype": "julia"}),
            "script.jl",
        )
        self.assertEqual(
            slack_document_inputs.choose_download_filename({"id": "F2", "name": "experiment", "filetype": "notebook"}),
            "experiment.ipynb",
        )


class SlackDocumentDownloadTests(unittest.TestCase):
    def test_download_slack_document_files_writes_files_and_sets_auth_header(self):
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return DummyResponse(b"document")

        downloads = [
            slack_document_inputs.SlackDocumentDownload(
                file_id="F1",
                filename="notes.txt",
                download_url="https://files.slack.com/notes.txt",
                mimetype="text/plain",
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("slack_document_inputs.urlopen", side_effect=fake_urlopen):
                files = slack_document_inputs.download_slack_document_files(
                    downloads,
                    bot_token="xoxb-test",
                    download_dir=tmpdir,
                    timeout_seconds=5,
                )

            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].filename, "notes.txt")
            self.assertEqual(files[0].path.read_bytes(), b"document")

        self.assertEqual(requests[0][1], 5)
        self.assertEqual(requests[0][0].headers.get("Authorization"), "Bearer xoxb-test")

    def test_download_slack_document_files_cleans_partial_files_on_failure(self):
        calls = {"count": 0}

        def fake_urlopen(_request, timeout=0):
            calls["count"] += 1
            if calls["count"] == 1:
                return DummyResponse(b"ok")
            raise OSError("network failed")

        downloads = [
            slack_document_inputs.SlackDocumentDownload(
                file_id="F1",
                filename="a.txt",
                download_url="https://files.slack.com/a.txt",
            ),
            slack_document_inputs.SlackDocumentDownload(
                file_id="F2",
                filename="b.txt",
                download_url="https://files.slack.com/b.txt",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("slack_document_inputs.urlopen", side_effect=fake_urlopen):
                with self.assertRaisesRegex(RuntimeError, "Failed downloading Slack document F2"):
                    slack_document_inputs.download_slack_document_files(
                        downloads,
                        bot_token="xoxb-test",
                        download_dir=tmpdir,
                    )
            self.assertEqual(list(Path(tmpdir).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
