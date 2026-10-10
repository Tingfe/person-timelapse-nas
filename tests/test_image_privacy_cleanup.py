import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
AUDIT_SPEC = importlib.util.spec_from_file_location("audit_public_images", ROOT / "scripts/audit_public_images.py")
AUDIT = importlib.util.module_from_spec(AUDIT_SPEC)
AUDIT_SPEC.loader.exec_module(AUDIT)
SPEC = importlib.util.spec_from_file_location("image_cleanup", ROOT / "scripts/sanitize_image_attestations.py")
CLEANUP = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {"audit_public_images": AUDIT}):
    SPEC.loader.exec_module(CLEANUP)


RUNTIME = "sha256:" + "1" * 64
ATTESTATION = "sha256:" + "2" * 64
INDEX = {"manifests": [
    {"digest": RUNTIME, "platform": {"architecture": "arm64", "os": "linux", "variant": "v8"}},
    {"digest": ATTESTATION, "annotations": {"vnd.docker.reference.type": "attestation-manifest"}},
]}


class ImagePrivacyCleanupTests(unittest.TestCase):
    def test_only_original_arm64_runtime_is_selected(self):
        self.assertEqual(CLEANUP.platform_reference(INDEX), RUNTIME)

    def test_unexpected_or_multiple_platforms_are_rejected(self):
        for document in ({}, {"manifests": []}, {"manifests": [
                {"digest": RUNTIME, "platform": {"architecture": "amd64", "os": "linux"}}]},
                {"manifests": [INDEX["manifests"][0], INDEX["manifests"][0]]}):
            with self.subTest(document=document), self.assertRaisesRegex(RuntimeError, "unexpected platform"):
                CLEANUP.platform_reference(document)

    def test_plan_flags_email_without_printing_its_value(self):
        responses = [
            {"results": [{"name": "example"}], "next": None}, {"token": "test-token"}, INDEX,
            {"layers": [{"digest": "sha256:" + "3" * 64, "mediaType": "application/vnd.in-toto+json"}]},
            {"predicate": {"github_event_payload": {"pusher": {"email": "private@example.org"}}}},
        ]
        output = io.StringIO()
        with patch.object(CLEANUP, "fetch", side_effect=[json.dumps(value).encode() for value in responses]), \
                contextlib.redirect_stdout(output):
            changes, token = CLEANUP.plan_changes()
        self.assertEqual(token, "test-token")
        self.assertEqual(changes[0]["platform"], RUNTIME)
        self.assertIn("/predicate/github_event_payload/pusher/email", output.getvalue())
        self.assertNotIn("private@example.org", output.getvalue())

    def test_public_git_ssh_url_is_not_a_private_email(self):
        self.assertFalse(AUDIT.findings(b'git@github.com:example/public-project.git'))
        self.assertIn("email address requiring review", AUDIT.findings(b'private@example.org'))

    def test_preview_never_authenticates_or_writes(self):
        with patch.object(sys, "argv", ["cleanup"]), patch.object(CLEANUP, "plan_changes", return_value=([], "token")), \
                patch.object(CLEANUP, "push_token") as auth, patch.object(CLEANUP, "push_index") as write, \
                contextlib.redirect_stdout(io.StringIO()):
            CLEANUP.main()
        auth.assert_not_called()
        write.assert_not_called()

    def test_moved_tag_is_not_overwritten(self):
        changes = [{"tag": "example", "before": "sha256:" + "0" * 64, "platform": RUNTIME}]
        with patch.object(sys, "argv", ["cleanup", "--apply"]), \
                patch.object(CLEANUP, "plan_changes", return_value=(changes, "token")), \
                patch.object(CLEANUP, "fetch", return_value=b"changed"), \
                patch.object(CLEANUP, "push_token", return_value="write-token"), \
                patch.object(CLEANUP, "push_index") as write, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "Tag changed"):
                CLEANUP.main()
        write.assert_not_called()

    def test_apply_verifies_the_preserved_runtime(self):
        before = json.dumps(INDEX).encode()
        clean = {"manifests": [INDEX["manifests"][0]]}
        changes = [{"tag": "example", "before": "sha256:" + hashlib.sha256(before).hexdigest(),
                    "platform": RUNTIME, "clean": clean}]
        after = json.dumps(clean).encode()
        with patch.object(sys, "argv", ["cleanup", "--apply"]), \
                patch.object(CLEANUP, "plan_changes", return_value=(changes, "token")), \
                patch.object(CLEANUP, "fetch", side_effect=[before, after]), \
                patch.object(CLEANUP, "push_token", return_value="write-token"), \
                patch.object(CLEANUP, "push_index", return_value="sha256:" + hashlib.sha256(after).hexdigest()) as write, \
                contextlib.redirect_stdout(io.StringIO()):
            CLEANUP.main()
        self.assertEqual(write.call_args.args, ("example", clean, "write-token"))

    def test_apply_rejects_readback_with_changed_runtime(self):
        before = json.dumps(INDEX).encode()
        changes = [{"tag": "example", "before": "sha256:" + hashlib.sha256(before).hexdigest(),
                    "platform": RUNTIME, "clean": {"manifests": [INDEX["manifests"][0]]}}]
        after = json.dumps({"manifests": [{**INDEX["manifests"][0], "digest": "sha256:" + "4" * 64}]}).encode()
        with patch.object(sys, "argv", ["cleanup", "--apply"]), \
                patch.object(CLEANUP, "plan_changes", return_value=(changes, "token")), \
                patch.object(CLEANUP, "fetch", side_effect=[before, after]), \
                patch.object(CLEANUP, "push_token", return_value="write-token"), \
                patch.object(CLEANUP, "push_index", return_value="sha256:" + hashlib.sha256(after).hexdigest()), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "read-back failed"):
                CLEANUP.main()

    def test_apply_waits_for_exact_index_readback(self):
        before = json.dumps(INDEX).encode()
        clean = {"manifests": [INDEX["manifests"][0]]}
        after = json.dumps(clean).encode()
        changes = [{"tag": "example", "before": "sha256:" + hashlib.sha256(before).hexdigest(),
                    "platform": RUNTIME, "clean": clean}]
        with patch.object(sys, "argv", ["cleanup", "--apply"]), \
                patch.object(CLEANUP, "plan_changes", return_value=(changes, "token")), \
                patch.object(CLEANUP, "fetch", side_effect=[before, before, after]), \
                patch.object(CLEANUP, "push_token", return_value="write-token"), \
                patch.object(CLEANUP, "push_index", return_value="sha256:" + hashlib.sha256(after).hexdigest()), \
                patch.object(CLEANUP.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            CLEANUP.main()
        sleep.assert_called_once_with(1)

    def test_apply_requires_protected_credentials(self):
        with patch.dict(CLEANUP.os.environ, {}, clear=True), patch.object(CLEANUP, "request") as request:
            with self.assertRaisesRegex(RuntimeError, "protected runner"):
                CLEANUP.push_token()
        request.assert_not_called()
