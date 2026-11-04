"""Offline coverage for provider selection and environment setup boundaries."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from embodied_runtime.config import load_runtime_config, resolve_launch_configuration
from embodied_runtime.credentials import setup_credentials
from embodied_runtime.environment_setup import configure_environment, required_extras, verify_imports

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("setup_environment_script", ROOT / "scripts/setup_environment.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class EnvironmentSetupTest(unittest.TestCase):
    def launch(self, **overrides):
        return resolve_launch_configuration(SimpleNamespace(**overrides))

    def test_offline_default_needs_no_provider(self):
        self.assertEqual(required_extras(self.launch()), [])

    def test_both_cognition_backends_need_openai_sdk(self):
        for backend in ("openai-responses", "openai-compatible"):
            self.assertEqual(required_extras(self.launch(cognition=backend)), ["openai"])

    def test_vision_needs_sdk_without_cognition(self):
        self.assertEqual(required_extras(self.launch(vision="openai-responses")), ["openai"])
        config = load_runtime_config(ROOT / "config/mira-openrouter-gemini.toml")
        launch = resolve_launch_configuration(SimpleNamespace(), config)
        self.assertIn("openai", required_extras(launch))

    def test_disabled_voice_does_not_install_unused_provider(self):
        self.assertEqual(required_extras(self.launch(tts="elevenlabs")), [])
        for tts in ("elevenlabs", "openai", "piper"):
            self.assertEqual(required_extras(self.launch(voice=True, tts=tts)), [tts])

    def test_sms_config_and_launch_override(self):
        self.assertEqual(required_extras(self.launch(sms=True)), ["twilio"])
        self.assertEqual(required_extras(self.launch(), with_sms=True), ["twilio"])

    def test_reference_config_selects_voice_and_cognition(self):
        config = load_runtime_config(ROOT / "config/mira-agentic.toml")
        launch = resolve_launch_configuration(SimpleNamespace(), config)
        self.assertEqual(required_extras(launch), ["elevenlabs", "openai"])

    def test_check_does_not_install_or_start_runtime(self):
        with patch("embodied_runtime.environment_setup.subprocess.run") as run, \
                patch("embodied_runtime.environment_setup.verify_imports") as verify, \
                redirect_stdout(io.StringIO()):
            configure_environment(ROOT / "config/mira-agentic.toml", check_only=True)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1:], ["-m", "pip", "check"])
        verify.assert_called_once_with(["elevenlabs", "openai"])

    def test_credentials_only_skips_pip_and_dependency_checks(self):
        with patch("embodied_runtime.environment_setup.subprocess.run") as run, \
                patch("embodied_runtime.environment_setup.verify_imports") as verify, \
                patch("embodied_runtime.credentials.setup_credentials",
                      return_value={"OPENAI_API_KEY": "test-placeholder-key",
                                    "ELEVENLABS_API_KEY": "test-placeholder-voice-key"}) as setup:
            configure_environment(ROOT / "config/mira-agentic.toml", credentials_only=True)
        run.assert_not_called()
        verify.assert_not_called()
        setup.assert_called_once()

    def test_credentials_only_reports_unresolved_missing_key(self):
        with patch("embodied_runtime.credentials.setup_credentials", return_value={}):
            with self.assertRaisesRegex(SystemExit, "OPENAI_API_KEY, ELEVENLABS_API_KEY still missing"):
                configure_environment(ROOT / "config/mira-agentic.toml", credentials_only=True)

    def test_check_never_prompts_for_credentials(self):
        with patch("embodied_runtime.environment_setup.subprocess.run"), \
                patch("embodied_runtime.environment_setup.verify_imports"), \
                patch("embodied_runtime.credentials.setup_credentials") as setup, \
                redirect_stdout(io.StringIO()):
            configure_environment(ROOT / "config/mira-agentic.toml", check_only=True)
        self.assertIs(setup.call_args.kwargs["interactive"], False)

    def test_existing_openai_does_not_block_missing_elevenlabs_in_either_setup_mode(self):
        for credentials_only in (False, True):
            with self.subTest(credentials_only=credentials_only), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "home"
                home.mkdir()
                with patch.dict("os.environ", {"OPENAI_API_KEY": "dummy-openai"}, clear=True), \
                        patch("embodied_runtime.environment_setup.subprocess.run"), \
                        patch("embodied_runtime.environment_setup.verify_imports"), \
                        patch("embodied_runtime.credentials.setup_credentials",
                              side_effect=lambda launch, **options: setup_credentials(
                                  launch, **{**options, "interactive": True}, home=home, repository=ROOT)), \
                        patch("builtins.input", side_effect=["y", "n"]) as prompt, \
                        patch("getpass.getpass", return_value="dummy-elevenlabs") as hidden, \
                        redirect_stdout(io.StringIO()):
                    configure_environment(ROOT / "config/mira-agentic.toml", credentials_only=credentials_only)
                self.assertIn("ELEVENLABS_API_KEY", prompt.call_args_list[0].args[0])
                hidden.assert_called_once()
                self.assertTrue((home / ".config/embodied-runtime/elevenlabs.env").is_file())
                self.assertFalse((home / ".config/embodied-runtime/openai.env").exists())

    def test_noninteractive_and_check_modes_do_not_prompt_read_or_write_credentials(self):
        for mode in ({}, {"credentials_only": True}, {"check_only": True}):
            with self.subTest(mode=mode), patch.dict("os.environ", {}, clear=True), \
                    patch("embodied_runtime.environment_setup.subprocess.run"), \
                    patch("embodied_runtime.environment_setup.verify_imports"), \
                    patch("embodied_runtime.credentials.sys.stdin.isatty", return_value=False), \
                    patch("embodied_runtime.credentials.sys.stderr.isatty", return_value=False), \
                    patch("builtins.input") as prompt, patch("getpass.getpass") as hidden, \
                    patch("embodied_runtime.credentials._read_saved_key") as read, \
                    patch("embodied_runtime.credentials._save_key") as save, \
                    patch("embodied_runtime.credentials._enable_bashrc") as bashrc, \
                    redirect_stdout(io.StringIO()) as output:
                if mode.get("credentials_only"):
                    with self.assertRaisesRegex(SystemExit, "OPENAI_API_KEY, ELEVENLABS_API_KEY still missing"):
                        configure_environment(ROOT / "config/mira-agentic.toml", **mode)
                else:
                    configure_environment(ROOT / "config/mira-agentic.toml", **mode)
                self.assertIn("ELEVENLABS_API_KEY", output.getvalue())
                for mocked in (prompt, hidden, read, save, bashrc):
                    mocked.assert_not_called()

    def test_both_environment_keys_satisfy_credentials_only_without_prompting(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "dummy-openai", "ELEVENLABS_API_KEY": "dummy-elevenlabs"}, clear=True), \
                patch("builtins.input") as prompt, patch("getpass.getpass") as hidden, \
                redirect_stdout(io.StringIO()):
            configure_environment(ROOT / "config/mira-agentic.toml", credentials_only=True)
        prompt.assert_not_called()
        hidden.assert_not_called()

    def test_replacement_requires_interactive_terminal(self):
        for option in ("replace_openai_key", "replace_elevenlabs_key"):
            with self.subTest(option=option), \
                    patch("embodied_runtime.environment_setup.sys.stdin.isatty", return_value=False), \
                    patch("embodied_runtime.credentials.setup_credentials") as setup:
                with self.assertRaisesRegex(SystemExit, "interactive terminal"):
                    configure_environment(ROOT / "config/mira-agentic.toml", credentials_only=True, **{option: True})
                setup.assert_not_called()

    def test_configuration_selects_only_required_credentials(self):
        for voice in ('enabled = false\ntts = "elevenlabs"', 'enabled = true\ntts = "espeak"'):
            with self.subTest(voice=voice), tempfile.TemporaryDirectory() as temporary:
                config = Path(temporary) / "launch.toml"
                config.write_text('[runtime]\nprofile = "mira"\ncognition = "openai-responses"\n[voice]\n' + voice + '\n')
                with patch.dict("os.environ", {"OPENAI_API_KEY": "dummy-openai"}, clear=True), \
                        patch("builtins.input") as prompt, redirect_stdout(io.StringIO()) as output:
                    configure_environment(config, credentials_only=True)
                prompt.assert_not_called()
                self.assertNotIn("ELEVENLABS_API_KEY", output.getvalue())

    def test_broken_import_fails_check(self):
        with patch("embodied_runtime.environment_setup.importlib.import_module",
                   side_effect=ImportError("missing dependency")):
            with self.assertRaises(ImportError):
                verify_imports([])

    def test_pip_failure_stops_before_import_success(self):
        with patch("embodied_runtime.environment_setup.subprocess.run",
                   side_effect=subprocess.CalledProcessError(1, ["pip"])), \
                patch("embodied_runtime.environment_setup.verify_imports") as verify, \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                configure_environment(ROOT / "config/mira-agentic.toml")
        verify.assert_not_called()

    def test_bootstrap_reuses_venv(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / "venv with spaces"
            environment.mkdir()
            (environment / "pyvenv.cfg").write_text("")
            with patch.object(bootstrap.sys, "version_info", (3, 13)), \
                    patch.object(bootstrap.venv, "EnvBuilder") as builder, \
                    patch.object(bootstrap.subprocess, "run") as run, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(bootstrap.main(["--config", str(ROOT / "config/mira-agentic.toml"),
                                                 "--venv", str(environment), "--check"]), 0)
            builder.assert_not_called()
            self.assertEqual(run.call_count, 2)
            self.assertTrue(all(call.args[0][0] == str(environment / "bin/python")
                                for call in run.call_args_list))

    def test_bootstrap_forwards_each_replacement_flag_without_pip(self):
        for flag, expected in (("--replace-openai-key", ["1", "0"]),
                               ("--replace-elevenlabs-key", ["0", "1"])):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as temporary:
                environment = Path(temporary) / "venv"
                environment.mkdir()
                (environment / "pyvenv.cfg").write_text("")
                with patch.object(bootstrap.sys, "version_info", (3, 13)), \
                        patch.object(bootstrap.subprocess, "run") as run, redirect_stdout(io.StringIO()):
                    self.assertEqual(bootstrap.main(["--config", str(ROOT / "config/mira-agentic.toml"),
                                                     "--venv", str(environment), "--credentials-only", flag]), 0)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args.args[0][-3:], ["1", *expected])

    def test_bootstrap_replacement_flags_require_credentials_only(self):
        for flag in ("--replace-openai-key", "--replace-elevenlabs-key"):
            with self.subTest(flag=flag), redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit):
                    bootstrap.main(["--config", str(ROOT / "config/mira-agentic.toml"), flag])
                self.assertIn(flag + " requires --credentials-only", error.getvalue())


if __name__ == "__main__":
    unittest.main()
