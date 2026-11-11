import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import media
import test_worker

w = test_worker.w
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 30


class Response(io.BytesIO):
    def __init__(self, data, length=None):
        super().__init__(data)
        self.headers = {} if length is None else {"Content-Length": str(length)}


class API:
    base = "https://api.telegram.org/bot123:SECRET/"

    def call(self, method, params):
        assert method == "getFile"
        return {"file_path": "photos/test.jpg", "file_size": 38}


class MediaTests(unittest.TestCase):
    def test_photo_and_caption_without_text(self):
        event = test_worker.update(text="")
        event["message"]["photo"] = [{"file_id": "small"}, {"file_id": "large"}]
        event["message"]["caption"] = "Descreva"
        params = w.wakeup_params(event, {42}, set())
        self.assertEqual(params["prompt"], "Descreva")
        with patch.object(media, "download", return_value={"path": "state/photo.png", "size": 38, "mime_type": "image/png"}) as download:
            w.receive_media(API(), event, params, "unused", 100)
        self.assertEqual(download.call_args.args[1]["file_id"], "large")
        self.assertEqual(params["images"], [{"path": "state/photo.png", "mime_type": "image/png"}])
        self.assertIsNone(w.wakeup_params(event, {999}, set()))

    def test_document_and_failure_context(self):
        event = test_worker.update(text="")
        event["message"]["document"] = {"file_id": "doc"}
        params = w.wakeup_params(event, {42}, set())
        self.assertTrue(params["prompt"])
        with patch.object(media, "download", side_effect=media.MediaError("limit")):
            w.receive_media(API(), event, params, "unused", 100)
        self.assertIn("attachment_error", params["context"])
        self.assertNotIn("images", params)

    def test_download_uses_generated_paths_and_sniffs_type(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            with patch.object(media.urllib.request, "urlopen", return_value=Response(PNG, len(PNG))) as request:
                result = media.download(API(), {"file_id": "id", "file_name": "../../secret.exe"}, 10, directory, 100)
            path = Path(result["path"])
            self.assertTrue(path.is_relative_to(Path(directory).relative_to(Path.cwd())))
            self.assertEqual(path.parent.name, "event-10")
            self.assertEqual(path.read_bytes(), PNG)
            self.assertEqual(path.suffix, ".png")
            self.assertEqual(result["mime_type"], "image/png")
            self.assertIn("/file/bot", request.call_args.args[0])

    def test_download_limits_and_partial_cleanup(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            with self.assertRaises(media.MediaError):
                media.download(API(), {"file_id": "id", "file_size": 101}, 1, directory, 100)
            with patch.object(media.urllib.request, "urlopen", return_value=Response(b"x" * 101)):
                with self.assertRaises(media.MediaError):
                    media.download(API(), {"file_id": "id"}, 2, directory, 100)
            self.assertEqual(list((Path(directory) / "event-2").iterdir()), [])
            with patch.object(media.urllib.request, "urlopen", return_value=Response(PNG, 999)):
                with self.assertRaises(media.MediaError):
                    media.download(API(), {"file_id": "id"}, 3, directory, 100)

    def test_bad_remote_path_and_external_storage(self):
        with patch.object(API, "call", return_value={"file_path": "../evil"}):
            with self.assertRaises(media.MediaError):
                media.download(API(), {"file_id": "id"}, 1, "unused", 100)
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaises(media.MediaError):
                media.download(API(), {"file_id": "id"}, 1, outside, 100)

    def test_upload_multipart_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.png"
            path.write_bytes(PNG)
            with patch.object(media.urllib.request, "urlopen", return_value=Response(b'{"ok":true}')) as send:
                media.upload(API(), "sendPhoto", "photo", path,
                             {"chat_id": -100, "reply_parameters": {"message_id": 7}, "message_thread_id": 3, "caption": "hi"})
            req = send.call_args.args[0]
            self.assertIn("multipart/form-data", req.get_header("Content-type"))
            self.assertIn(PNG, req.data)
            self.assertIn(b'"message_id": 7', req.data)
            self.assertIn(b'name="photo"', req.data)
            self.assertIn(b'filename="photo.png"', req.data)
            self.assertNotIn(b"parse_mode", req.data)

    def test_upload_limits_and_error_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.txt"
            path.write_bytes(b"text")
            with self.assertRaises(media.MediaError):
                media.upload(API(), "sendPhoto", "photo", path, {})
            with patch.object(media, "MAX_UPLOAD", 3):
                with self.assertRaises(media.MediaError):
                    media.upload(API(), "sendDocument", "document", path, {})
            with patch.object(media.urllib.request, "urlopen", side_effect=OSError(API.base)):
                with self.assertRaises(media.MediaError) as error:
                    media.upload(API(), "sendDocument", "document", path, {})
            self.assertNotIn("SECRET", str(error.exception))

    def test_image_capability_fallback(self):
        import threading
        stop = threading.Event()
        photo = test_worker.update(text="")
        photo["message"]["photo"] = [{"file_id": "photo"}]

        class PollAPI:
            count = 0

            def call(self, method, *_, **kwargs):
                if method == "sendChatAction":
                    return True
                self.count += 1
                if self.count == 1:
                    return [photo]
                stop.set()
                return []

        class RPC:
            def __init__(self, enabled):
                self.image_paths = enabled
                self.events = []

            def wakeup(self, params):
                self.events.append(params)

        for enabled in (False, True):
            stop.clear()
            rpc = RPC(enabled)
            with tempfile.TemporaryDirectory() as directory:
                with patch.object(media, "download", return_value={"path": "photo.png", "mime_type": "image/png", "size": 38}):
                    w.run_worker(PollAPI(), rpc, stop, Path(directory) / "cursor.json", {42}, set())
            self.assertEqual("images" in rpc.events[0], enabled)
            self.assertEqual("image_delivery_error" in rpc.events[0]["context"], not enabled)

    def test_send_document_cli(self):
        import sys
        with patch.object(sys, "argv", ["worker.py", "send-document", "--chat-id", "42", "--reply-to", "7", "--file", "doc.pdf"]):
            with patch.object(w, "token_value", return_value="123:secret"), patch.object(media, "upload") as upload:
                with patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(w.main(), 0)
            self.assertEqual(upload.call_args.args[1:4], ("sendDocument", "document", "doc.pdf"))
            self.assertEqual(upload.call_args.args[4]["reply_parameters"], {"message_id": 7})


if __name__ == "__main__":
    unittest.main()
