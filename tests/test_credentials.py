"""Credential setup tests use dummy values and temporary home directories only."""

from contextlib import redirect_stdout
from dataclasses import replace
import io
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from embodied_runtime.config import resolve_launch_configuration
from embodied_runtime.credentials import required_credentials, setup_credentials, setup_openai_credentials, SOURCE_LINE
from embodied_runtime.cognition.base import CognitionError
from embodied_runtime.cognition.provider_diagnostics import cognition_setup_guidance


class CredentialTests(unittest.TestCase):
    variable = "OPENAI_API_KEY"
    filename = "openai.env"
    launch_options = {"cognition": "openai-responses"}
    setup_provider = staticmethod(setup_openai_credentials)
    replace_option = "replace"
    source_line = SOURCE_LINE

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.launch = resolve_launch_configuration(SimpleNamespace(**self.launch_options))
        self.path = self.home / ".config/embodied-runtime" / self.filename
        self.output = io.StringIO()

    def setup(self, *, environ=None, **options):
        if "replace" in options:
            options[self.replace_option] = options.pop("replace")
        with redirect_stdout(self.output):
            return self.setup_provider(self.launch, environ={} if environ is None else environ,
                                       home=self.home, repository=self.repo, **options)

    def test_missing_key_noninteractive_is_read_only_and_actionable(self):
        with patch("builtins.input") as prompt, patch("getpass.getpass") as hidden:
            self.assertNotIn(self.variable, self.setup(interactive=False))
        prompt.assert_not_called()
        hidden.assert_not_called()
        self.assertFalse(self.path.parent.exists())
        self.assertIn("--credentials-only", self.output.getvalue())

    def test_existing_key_does_not_prompt_or_write(self):
        with patch("builtins.input") as prompt:
            result = self.setup(environ={self.variable: "dummy-existing"}, interactive=True)
        prompt.assert_not_called()
        self.assertEqual(result[self.variable], "dummy-existing")
        self.assertNotIn("dummy-existing", self.output.getvalue())
        self.assertFalse(self.path.exists())

    def test_hidden_entry_permissions_quoting_and_opt_in_bashrc(self):
        dummy = "test-placeholder'$(id)`id`"
        with patch("builtins.input", side_effect=["y", "y"]), patch("getpass.getpass", return_value=dummy):
            result = self.setup(interactive=True)
        self.assertEqual(result[self.variable], dummy)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        bashrc = (self.home / ".bashrc").read_text()
        self.assertIn(self.source_line, bashrc)
        self.assertNotIn(dummy, bashrc + self.output.getvalue())
        subprocess.run(["bash", "-c", '. "$1"; [ "${!2}" = "$3" ]',
                        "test", str(self.path), self.variable, dummy], check=True)
        self.assertIn("current terminal is unchanged", self.output.getvalue())
        self.assertIn(f"source ~/.config/embodied-runtime/{self.filename}", self.output.getvalue())

    def test_reuse_and_bashrc_are_idempotent_and_preserve_other_content(self):
        (self.home / ".bashrc").write_text("# existing shell settings\n")
        with patch("builtins.input", side_effect=["y", "y"]), patch("getpass.getpass", return_value="dummy-key"):
            self.setup(interactive=True)
        with patch("builtins.input", side_effect=["y", "", "y"]), patch("getpass.getpass") as hidden:
            self.assertEqual(self.setup(interactive=True)[self.variable], "dummy-key")
        hidden.assert_not_called()
        bashrc = (self.home / ".bashrc").read_text()
        self.assertEqual(bashrc.count(self.source_line), 1)
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
        self.assertNotIn(self.variable, result)
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
            result = self.setup(interactive=True, replace=True, environ={self.variable: "dummy-environment"})
        self.assertEqual(result[self.variable], "dummy-new")
        self.assertNotIn("dummy-old", self.path.read_text())
        self.assertNotIn("dummy-new", self.output.getvalue())

    def test_insecure_saved_key_is_not_loaded(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(f"export {self.variable}=dummy-key\n")
        self.path.chmod(0o644)
        with patch("builtins.input", side_effect=["y", ""]):
            result = self.setup(interactive=True)
        self.assertNotIn(self.variable, result)
        self.assertIn("mode 600", self.output.getvalue())

    def test_storage_inside_repo_is_rejected(self):
        with patch("builtins.input", return_value="y"), redirect_stdout(self.output):
            self.setup_provider(self.launch, environ={}, home=self.repo, repository=self.repo, interactive=True)
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

    def test_credential_directory_symlink_is_rejected(self):
        target = self.home / "other"
        target.mkdir()
        self.path.parent.parent.mkdir()
        self.path.parent.symlink_to(target, target_is_directory=True)
        with patch("builtins.input", return_value="y"), patch("getpass.getpass") as hidden:
            self.assertNotIn(self.variable, self.setup(interactive=True))
        hidden.assert_not_called()
        self.assertEqual(list(target.iterdir()), [])

    def test_foreign_owned_credential_directory_is_rejected(self):
        other_uid = os.getuid() + 1
        with patch("builtins.input", return_value="y"), \
                patch("embodied_runtime.credentials.os.getuid", return_value=other_uid), \
                patch("getpass.getpass") as hidden:
            self.assertNotIn(self.variable, self.setup(interactive=True))
        hidden.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_interrupted_entry_does_not_save(self):
        for error in (EOFError, KeyboardInterrupt):
            with self.subTest(error=error), patch("builtins.input", return_value="y"), \
                    patch("getpass.getpass", side_effect=error):
                self.assertNotIn(self.variable, self.setup(interactive=True))
            self.assertFalse(self.path.exists())

    def test_offline_config_needs_no_key(self):
        self.launch = resolve_launch_configuration(SimpleNamespace())
        with patch("builtins.input") as prompt:
            self.setup(interactive=True)
        prompt.assert_not_called()
        self.assertEqual(self.output.getvalue(), "")


class ElevenLabsCredentialTests(CredentialTests):
    variable = "ELEVENLABS_API_KEY"
    filename = "elevenlabs.env"
    launch_options = {"voice": True, "tts": "elevenlabs"}
    setup_provider = staticmethod(setup_credentials)
    replace_option = "replace_elevenlabs_key"
    source_line = ('if [ -z "${ELEVENLABS_API_KEY:-}" ] && '
                   '[ -r "$HOME/.config/embodied-runtime/elevenlabs.env" ]; then '
                   '. "$HOME/.config/embodied-runtime/elevenlabs.env"; fi')

    def test_bash_startup_loads_missing_key_and_preserves_environment_key(self):
        with patch("builtins.input", side_effect=["y", "y"]), patch("getpass.getpass", return_value="dummy-saved"):
            self.setup(interactive=True)
        for existing in (None, "", "dummy-environment"):
            with self.subTest(existing=existing):
                environment = {"HOME": str(self.home)}
                if existing is not None:
                    environment[self.variable] = existing
                subprocess.run(["bash", "--noprofile", "--norc", "-c",
                                '. "$HOME/.bashrc"; [ "$ELEVENLABS_API_KEY" = "$1" ]',
                                "test", existing or "dummy-saved"], env=environment, check=True)

    def test_replacement_cancellation_preserves_existing_environment_and_file(self):
        with patch("builtins.input", side_effect=["y", "n"]), patch("getpass.getpass", return_value="dummy-old"):
            self.setup(interactive=True)
        original = self.path.read_bytes()
        for answers, entry in ((["n"], None), (["y", "n"], None), (["y", "y"], "")):
            with self.subTest(answers=answers):
                with patch("builtins.input", side_effect=answers), patch("getpass.getpass", return_value=entry):
                    result = self.setup(interactive=True, replace=True, environ={self.variable: "dummy-current"})
                self.assertEqual(result[self.variable], "dummy-current")
                self.assertEqual(self.path.read_bytes(), original)

    def test_saved_openai_assignment_is_not_loaded_as_elevenlabs(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("export OPENAI_API_KEY=dummy-other-provider\n")
        self.path.chmod(0o600)
        with patch("builtins.input", side_effect=["y", ""]):
            self.assertNotIn(self.variable, self.setup(interactive=True))
        self.assertNotIn("dummy-other-provider", self.output.getvalue())

    def test_reuse_normalizes_shell_expressions_without_execution(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("export ELEVENLABS_API_KEY=$(id)\n")
        self.path.chmod(0o600)
        with patch("builtins.input", side_effect=["y", "", "n"]), patch("getpass.getpass") as hidden:
            self.assertEqual(self.setup(interactive=True)[self.variable], "$(id)")
        hidden.assert_not_called()
        self.assertEqual(self.path.read_text(), "export ELEVENLABS_API_KEY='$(id)'\n")


class CredentialSelectionTests(unittest.TestCase):
    def launch(self, **options):
        return resolve_launch_configuration(SimpleNamespace(**options))

    def test_elevenlabs_only_when_enabled_and_selected(self):
        for options, expected in (({}, ()), ({"tts": "elevenlabs"}, ()),
                                  ({"voice": True, "tts": "espeak"}, ()),
                                  ({"voice": True, "tts": "piper"}, ()),
                                  ({"voice": True, "tts": "elevenlabs"}, ("ELEVENLABS_API_KEY",))):
            with self.subTest(options=options):
                self.assertEqual(required_credentials(self.launch(**options)), expected)

    def test_openai_requirements_are_preserved(self):
        for options in ({"cognition": "openai-responses"}, {"vision": "openai-responses"},
                        {"voice": True, "tts": "openai"},
                        {"cognition": "openai-compatible", "compatible_api_key_env": "OPENAI_API_KEY"}):
            with self.subTest(options=options):
                self.assertEqual(required_credentials(self.launch(**options)), ("OPENAI_API_KEY",))
        self.assertEqual(required_credentials(self.launch(cognition="openai-compatible",
                                                         compatible_api_key_env="OTHER_KEY")), ())

    def test_configured_vision_keeps_openai_requirement_only_for_its_variable(self):
        for variable, expected in (("OPENAI_API_KEY", ("OPENAI_API_KEY",)), ("OTHER_KEY", ())):
            with self.subTest(variable=variable):
                launch = replace(self.launch(), vision="configured", vision_configuration=SimpleNamespace(
                    backends=[SimpleNamespace(api_key_env=variable)]))
                self.assertEqual(required_credentials(launch), expected)

    def test_both_required_keys_are_collected_independently(self):
        launch = self.launch(cognition="openai-responses", voice=True, tts="elevenlabs")
        self.assertEqual(required_credentials(launch), ("OPENAI_API_KEY", "ELEVENLABS_API_KEY"))
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            original = {"UNRELATED": "preserved"}
            with patch("builtins.input", side_effect=["y", "n", "y", "n"]), \
                    patch("getpass.getpass", side_effect=["dummy-openai", "dummy-elevenlabs"]), \
                    redirect_stdout(io.StringIO()):
                result = setup_credentials(launch, environ=original, home=home,
                                           repository=Path(temporary) / "repo", interactive=True)
            self.assertEqual(result, {**original, "OPENAI_API_KEY": "dummy-openai",
                                      "ELEVENLABS_API_KEY": "dummy-elevenlabs"})
            self.assertEqual(original, {"UNRELATED": "preserved"})
            self.assertTrue((home / ".config/embodied-runtime/openai.env").is_file())
            self.assertTrue((home / ".config/embodied-runtime/elevenlabs.env").is_file())

    def test_cancelled_openai_does_not_prevent_elevenlabs_setup(self):
        launch = self.launch(cognition="openai-responses", voice=True, tts="elevenlabs")
        with tempfile.TemporaryDirectory() as temporary:
            with patch("builtins.input", side_effect=["n", "y", "n"]), \
                    patch("getpass.getpass", return_value="dummy-elevenlabs"), redirect_stdout(io.StringIO()):
                result = setup_credentials(launch, environ={}, home=Path(temporary) / "home",
                                           repository=Path(temporary) / "repo", interactive=True)
            self.assertNotIn("OPENAI_API_KEY", result)
            self.assertEqual(result["ELEVENLABS_API_KEY"], "dummy-elevenlabs")


class CredentialGuidanceTests(unittest.TestCase):
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
