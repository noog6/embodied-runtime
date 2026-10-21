"""Offline coverage for provider selection and environment setup boundaries."""

from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from embodied_runtime.config import load_runtime_config, resolve_launch_configuration
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
                patch("embodied_runtime.credentials.setup_openai_credentials",
                      return_value={"OPENAI_API_KEY": "test-placeholder-key"}) as setup:
            configure_environment(ROOT / "config/mira-agentic.toml", credentials_only=True)
        run.assert_not_called()
        verify.assert_not_called()
        setup.assert_called_once()

    def test_credentials_only_reports_unresolved_missing_key(self):
        with patch("embodied_runtime.credentials.setup_openai_credentials", return_value={}):
            with self.assertRaisesRegex(SystemExit, "still missing"):
                configure_environment(ROOT / "config/mira-agentic.toml", credentials_only=True)

    def test_check_never_prompts_for_credentials(self):
        with patch("embodied_runtime.environment_setup.subprocess.run"), \
                patch("embodied_runtime.environment_setup.verify_imports"), \
                patch("embodied_runtime.credentials.setup_openai_credentials") as setup, \
                redirect_stdout(io.StringIO()):
            configure_environment(ROOT / "config/mira-agentic.toml", check_only=True)
        self.assertIs(setup.call_args.kwargs["interactive"], False)

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


if __name__ == "__main__":
    unittest.main()
