import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("overview_sync", ROOT / "scripts/sync_dockerhub_overview.py")
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)


class OverviewPublicationTests(unittest.TestCase):
    def test_relative_links_resolve_to_public_repository(self):
        payload = SYNC.build_payload("[checklist](OPEN_SOURCE_CHECKLIST.md) [web](https://example.org) [part](#part)")
        self.assertIn(f"({SYNC.SOURCE_URL}OPEN_SOURCE_CHECKLIST.md)", payload["full_description"])
        self.assertIn("(https://example.org)", payload["full_description"])
        self.assertIn("(#part)", payload["full_description"])

    def test_empty_and_oversize_documents_are_rejected(self):
        for content in (" ", "a" * 25001):
            with self.assertRaises(ValueError):
                SYNC.build_payload(content)

    def test_publish_reads_back_both_fields(self):
        payload = SYNC.build_payload("# Public overview")
        with patch.object(SYNC, "request_json", side_effect=[{}, {"access_token": "test-token"}, {}, payload]) as request:
            SYNC.publish(payload, "example", "test-secret")
        self.assertEqual(request.call_count, 4)
        self.assertEqual(request.call_args_list[2].args[1:], ("PATCH", payload, "test-token"))

    def test_successful_patch_with_wrong_readback_fails(self):
        with patch.object(SYNC, "request_json", side_effect=[{}, {"access_token": "test-token"}, {}, {}]):
            with self.assertRaisesRegex(RuntimeError, "read-back"):
                SYNC.publish(SYNC.build_payload("# Overview"), "example", "test-secret")

    def test_http_error_does_not_expose_response_body(self):
        error = HTTPError("https://hub.docker.com", 401, "private authentication detail", {}, None)
        with patch.object(SYNC, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as caught:
                SYNC.request_json("https://hub.docker.com")
        self.assertNotIn("private authentication detail", str(caught.exception))

    def test_mac_defaults_are_generic_and_match(self):
        for name in ("mac-worker.sh", "mac_console.py", "mac_index.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("/Volumes/camera-recordings", source)
            self.assertNotRegex(source, r"/Volumes/[^\s/\"']*\d{3}")

    def test_image_publication_masks_email_before_actions_and_disables_records(self):
        source = (ROOT / ".github/workflows/dockerhub.yml").read_text(encoding="utf-8")
        self.assertLess(source.index("Mask personal email"), source.index("actions/checkout"))
        self.assertIn('DOCKER_BUILD_RECORD_UPLOAD: "false"', source)
        self.assertIn("::add-mask::", source)
