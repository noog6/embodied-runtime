"""Credential setup tests use dummy values and temporary home directories only."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from embodied_runtime.config import resolve_launch_configuration
from embodied_runtime.credentials import setup_openai_credentials, SOURCE_LINE
from embodied_runtime.cognition.base import CognitionError
from embodied_runtime.cognition.provider_diagnostics import cognition_setup_guidance


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.launch = resolve_launch_configuration(SimpleNamespace(cognition="openai-responses"))
        self.path = self.home / ".config/embodied-runtime/openai.env"
        self.output = io.StringIO()

    def setup(self, **options):
        with redirect_stdout(self.output):
            return setup_openai_credentials(self.launch, environ={}, home=self.home,
                                            repository=self.repo, **options)

    def test_missing_key_noninteractive_is_read_only_and_actionable(self):
        with patch("builtins.input") as prompt, patch("getpass.getpass") as hidden:
            self.assertNotIn("OPENAI_API_KEY", self.setup(interactive=False))
        prompt.assert_not_called()
        hidden.assert_not_called()
        self.assertFalse(self.path.parent.exists())
        self.assertIn("--credentials-only", self.output.getvalue())

    def test_existing_key_does_not_prompt_or_write(self):
        with patch("builtins.input") as prompt, redirect_stdout(self.output):
            result = setup_openai_credentials(self.launch, environ={"OPENAI_API_KEY": "dummy-existing"},
                                              home=self.home, repository=self.repo, interactive=True)
        prompt.assert_not_called()
        self.assertEqual(result["OPENAI_API_KEY"], "dummy-existing")
        self.assertNotIn("dummy-existing", self.output.getvalue())
        self.assertFalse(self.path.exists())

    def test_hidden_entry_permissions_quoting_and_opt_in_bashrc(self):
        dummy = "test-placeholder'$(id)`id`"
        with patch("builtins.input", side_effect=["y", "y"]), patch("getpass.getpass", return_value=dummy):
            result = self.setup(interactive=True)
        self.assertEqual(result["OPENAI_API_KEY"], dummy)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        bashrc = (self.home / ".bashrc").read_text()
        self.assertIn(SOURCE_LINE, bashrc)
        self.assertNotIn(dummy, bashrc + self.output.getvalue())
        subprocess.run(["bash", "-c", '. "$1"; [ "$OPENAI_API_KEY" = "$2" ]',
                        "test", str(self.path), dummy], check=True)
        self.assertIn("current terminal is unchanged", self.output.getvalue())

    def test_reuse_and_bashrc_are_idempotent_and_preserve_other_content(self):
        (self.home / ".bashrc").write_text("# existing shell settings\n")
        with patch("builtins.input", side_effect=["y", "y"]), patch("getpass.getpass", return_value="dummy-key"):
            self.setup(interactive=True)
        with patch("builtins.input", side_effect=["y", "", "y"]), patch("getpass.getpass") as hidden:
            self.assertEqual(self.setup(interactive=True)["OPENAI_API_KEY"], "dummy-key")
        hidden.assert_not_called()
        bashrc = (self.home / ".bashrc").read_text()
        self.assertEqual(bashrc.count(SOURCE_LINE), 1)
        self.assertTrue(bashrc.startswith("# existing shell settings\n"))

    def test_decline_and_blank_entry_do_not_save(self):
        with patch("builtins.input", return_value="n"), patch("getpass.getpass") as hidden:
            self.setup(interactive=True)
        hidden.assert_not_called()
        with patch("builtins.input", return_value="y"), patch("getpass.getpass", return_value=""):
            self.setup(interactive=True)
        self.assertFalse(self.path.exists())

    def test_key_with_control_characters_is_rejected_without_echo(self):
        dummy = "test-secret\nexport BAD=value"
        with patch("builtins.input", return_value="y"), patch("getpass.getpass", return_value=dummy):
            result = self.setup(interactive=True)
        self.assertNotIn("OPENAI_API_KEY", result)
        self.assertFalse(self.path.exists())
        self.assertNotIn(dummy, self.output.getvalue())

    def test_secret_file_symlink_is_not_followed(self):
        self.path.parent.mkdir(parents=True)
        target = self.home / "other"
        target.write_text("untouched")
        self.path.symlink_to(target)
        with patch("builtins.input", side_effect=["y", ""]), patch("getpass.getpass") as hidden:
            self.setup(interactive=True)
        hidden.assert_not_called()
        self.assertEqual(target.read_text(), "untouched")

    def test_explicit_rotation_replaces_saved_key_without_echo(self):
        with patch("builtins.input", side_effect=["y", "n"]), patch("getpass.getpass", return_value="dummy-old"):
            self.setup(interactive=True)
        with patch("builtins.input", side_effect=["y", "y", "n"]), patch("getpass.getpass", return_value="dummy-new"):
            result = self.setup(interactive=True, replace=True)
        self.assertEqual(result["OPENAI_API_KEY"], "dummy-new")
        self.assertNotIn("dummy-old", self.path.read_text())
        self.assertNotIn("dummy-new", self.output.getvalue())

    def test_insecure_saved_key_is_not_loaded(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("export OPENAI_API_KEY=dummy-key\n")
        self.path.chmod(0o644)
        with patch("builtins.input", side_effect=["y", ""]):
            result = self.setup(interactive=True)
        self.assertNotIn("OPENAI_API_KEY", result)
        self.assertIn("mode 600", self.output.getvalue())

    def test_storage_inside_repo_is_rejected(self):
        with patch("builtins.input", return_value="y"), redirect_stdout(self.output):
            setup_openai_credentials(self.launch, environ={}, home=self.repo, repository=self.repo, interactive=True)
        self.assertFalse((self.repo / ".config").exists())

    def test_hidden_input_failure_does_not_fall_back_to_echo(self):
        import getpass
        with patch("builtins.input", return_value="y"), \
                patch("getpass.getpass", side_effect=getpass.GetPassWarning("private detail")):
            self.setup(interactive=True)
        self.assertFalse(self.path.exists())
        self.assertNotIn("private detail", self.output.getvalue())

    def test_bashrc_symlink_does_not_modify_target(self):
        target = self.home / "shell-config"
        target.write_text("untouched")
        (self.home / ".bashrc").symlink_to(target)
        with patch("builtins.input", side_effect=["y", "y"]), patch("getpass.getpass", return_value="dummy-key"):
            self.setup(interactive=True)
        self.assertEqual(target.read_text(), "untouched")
        self.assertTrue(self.path.exists())

    def test_offline_config_needs_no_key(self):
        self.launch = resolve_launch_configuration(SimpleNamespace())
        with patch("builtins.input") as prompt:
            self.setup(interactive=True)
        prompt.assert_not_called()
        self.assertEqual(self.output.getvalue(), "")

    def test_error_guidance_does_not_expose_exception_or_arbitrary_category(self):
        backend = SimpleNamespace(identifier="openai-responses")
        for category in ("missing_credentials", "authentication", "connection", "client_initialization", "secret-category"):
            result = cognition_setup_guidance(backend, CognitionError("secret-key", category=category))
            self.assertNotIn("secret", str(result))
        category, hint = cognition_setup_guidance(backend, CognitionError("secret", category="authentication"))
        self.assertIn("--replace-openai-key", hint)
        self.assertIn("permissions", hint)


if __name__ == "__main__":
    unittest.main()
