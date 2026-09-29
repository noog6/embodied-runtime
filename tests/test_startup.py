"""Tests for deterministic, non-mutating Startup Wizard deployment logic."""

from pathlib import Path
import io
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from embodied_runtime.startup import (
    Deployment, InstallResult, StartupError, SystemPaths, TWILIO_ENVIRONMENT,
    build_deployment, check_deployment, discover_repository, environment_presence, install,
    render_unit, required_environment, select_config, serialize_environment, uninstall,
    validate_managed_environment_file, wizard_main,
)


class FakeRunner:
    def __init__(self, *, active: bool = False) -> None:
        self.commands: list[list[str]] = []
        self.active = active

    def __call__(self, command, *, check=True):
        command = list(command)
        self.commands.append(command)
        plain = command[1:] if command and command[0] == "sudo" else command
        if plain and plain[0] == "install" and "-d" in plain:
            Path(plain[-1]).mkdir(parents=True, exist_ok=True)
        if plain and plain[0] == "install" and "-d" not in plain:
            shutil.copyfile(plain[-2], plain[-1])
        if plain[:2] == ["rm", "-f"]:
            Path(plain[-1]).unlink(missing_ok=True)
        if plain[:2] == ["systemctl", "is-active"]:
            return SimpleNamespace(
                returncode=0 if self.active else 3,
                stdout="active\n" if self.active else "inactive\n", stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class StartupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name) / "Test Install" / "My Robot" / "embodied-runtime"
        (root / "embodied_runtime").mkdir(parents=True)
        (root / "config").mkdir()
        (root / "profiles").mkdir()
        (root / "deploy/systemd").mkdir(parents=True)
        (root / ".venv/bin").mkdir(parents=True)
        (root / "main.py").write_text("")
        python = root / ".venv/bin/python"
        python.write_text("#!/bin/sh\n")
        python.chmod(0o755)
        (root / "profiles/mira.toml").write_text(
            '[profile]\nid="mira"\nname="Mira"\ndescription="test"\n'
        )
        (root / "deploy/systemd/embodied-runtime.service.in").write_text(
            (Path(__file__).parents[1] / "deploy/systemd/embodied-runtime.service.in").read_text()
        )
        self.root = root
        self.system = Path(self.temporary.name) / "system"
        self.paths = SystemPaths(self.system / "units", self.system / "environment")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def config(self, *, cognition="none", vision="none", tts="espeak", sms=False) -> Path:
        path = self.root / "config/runtime config.toml"
        path.write_text(
            f'''[runtime]
profile = "mira"
hardware = "virtual"
camera = "{'picamera2' if vision != 'none' else 'none'}"
cognition = "{cognition}"
vision = "{vision}"
mode = "console"

[voice]
enabled = {str(tts != 'espeak').lower()}
tts = "{tts}"
elevenlabs_tts_voice_id = "voice"

[sms]
enabled = {str(sms).lower()}
'''
        )
        return path

    def deployment(self, **kwargs) -> Deployment:
        config = kwargs.pop("config", None)
        if config is None:
            config = self.config()
        return build_deployment(
            repo=self.root, config=config, user="robot", euid=1000,
            account_lookup=lambda _: SimpleNamespace(pw_uid=1000), paths=self.paths,
            **kwargs,
        )

    def test_discovers_checkout_from_script_not_working_directory(self) -> None:
        script = self.root / "scripts/startup_wizard.py"
        script.parent.mkdir()
        script.write_text("")
        self.assertEqual(discover_repository(script), self.root.resolve())

    def test_venv_entry_point_symlink_is_not_resolved_in_unit(self) -> None:
        entry_point = self.root / ".venv/bin/python"
        entry_point.unlink()
        entry_point.symlink_to(Path(sys.executable))
        deployment = self.deployment()
        unit = render_unit(deployment)
        self.assertEqual(deployment.python, entry_point)
        self.assertIn(f'ExecStart="{entry_point}"', unit)
        self.assertNotIn(f'ExecStart="{entry_point.resolve()}"', unit)

    def test_config_selection_is_interactive_only_when_ambiguous(self) -> None:
        mira = self.root / "config/mira-agentic.toml"
        other = self.root / "config/other.toml"
        mira.write_text("[runtime]\n")
        other.write_text("[runtime]\n")
        self.assertEqual(select_config(self.root, None, interactive=True), mira.resolve())
        with self.assertRaisesRegex(StartupError, "--config"):
            select_config(self.root, None, interactive=False)
        self.assertEqual(select_config(self.root, other, interactive=False), other.resolve())

    def test_single_config_is_selected_noninteractively(self) -> None:
        only = self.root / "config/only.toml"
        only.write_text("[runtime]\n")
        self.assertEqual(select_config(self.root, None), only.resolve())

    def test_render_exact_policy_and_space_safe_arguments(self) -> None:
        deployment = self.deployment(with_sms=True, capture_env=True)
        unit = render_unit(deployment)
        expected = (
            "Type=simple", "User=robot",
            f"WorkingDirectory={str(self.root).replace(' ', r'\x20')}",
            f'"{self.root / ".venv/bin/python"}"', f'"{self.root / "main.py"}"',
            f'"{deployment.config}"', '"--mode" "run"', '"--no-color"',
            '"--sms"', "Restart=on-failure", "RestartSec=10s",
            "TimeoutStopSec=30s", "StartLimitIntervalSec=300",
            "StartLimitBurst=3", "WantedBy=multi-user.target",
            "StandardOutput=journal", "StandardError=journal",
            f"EnvironmentFile={deployment.environment_path}",
        )
        for value in expected:
            self.assertIn(value, unit)
        forbidden = (
            "git pull", "ExecStartPre", "Restart=always", "ExecStop=/bin/kill",
            "KillSignal=SIGINT", "XDG_RUNTIME_DIR", "/run/user/1000",
            "/home/pi/workarea", "pyPiBot.log", "secret-test-value",
        )
        for value in forbidden:
            self.assertNotIn(value, unit)

    def test_required_provider_environment_and_presence_are_secret_free(self) -> None:
        deployment = self.deployment(config=self.config(cognition="openai-responses"))
        self.assertEqual(required_environment(deployment), ("OPENAI_API_KEY",))
        self.assertEqual(environment_presence(deployment, {"OPENAI_API_KEY": "secret"}),
                         {"OPENAI_API_KEY": True})

        eleven = self.deployment(config=self.config(tts="elevenlabs"))
        self.assertEqual(environment_presence(eleven, {}), {"ELEVENLABS_API_KEY": False})
        self.assertNotIn("secret-test-value", str(environment_presence(eleven, {})))

    def test_sms_requires_complete_twilio_catalog(self) -> None:
        deployment = self.deployment(with_sms=True)
        self.assertEqual(required_environment(deployment), TWILIO_ENVIRONMENT)

    def test_capture_contains_only_required_approved_values(self) -> None:
        deployment = self.deployment(
            config=self.config(cognition="openai-responses"), capture_env=True,
        )
        content = serialize_environment(deployment, {
            "OPENAI_API_KEY": "secret-test-value", "PATH": "/bin", "HOME": "/home/x",
            "RANDOM_SECRET_FROM_SOMETHING_ELSE": "wrong",
        }).decode()
        self.assertEqual(content, 'OPENAI_API_KEY="secret-test-value"\n')
        with self.assertRaisesRegex(StartupError, "cannot be safely stored"):
            serialize_environment(deployment, {"OPENAI_API_KEY": "bad\nvalue"})

    def test_managed_environment_file_security(self) -> None:
        deployment = self.deployment(config=self.config(cognition="openai-responses"))
        deployment.environment_path.parent.mkdir(parents=True)
        deployment.environment_path.write_text("opaque")
        deployment.environment_path.chmod(0o600)
        self.assertTrue(validate_managed_environment_file(deployment))

        for mode in (0o644, 0o620, 0o602):
            with self.subTest(mode=oct(mode)):
                deployment.environment_path.chmod(mode)
                with self.assertRaisesRegex(StartupError, "not securely protected"):
                    validate_managed_environment_file(deployment)

        deployment.environment_path.unlink()
        target = deployment.environment_path.with_suffix(".target")
        target.write_text("opaque")
        target.chmod(0o600)
        deployment.environment_path.symlink_to(target)
        with self.assertRaisesRegex(StartupError, "not securely protected"):
            validate_managed_environment_file(deployment)

    @patch("embodied_runtime.startup.shutil.which", return_value=None)
    def test_missing_or_unsafe_secret_source_fails_before_mutation(self, _which) -> None:
        deployment = self.deployment(config=self.config(cognition="openai-responses"))
        runner = FakeRunner()
        with self.assertRaisesRegex(StartupError, "requires service secrets"):
            install(deployment, runner=runner, euid=1000)
        self.assertEqual(runner.commands, [])

        deployment.environment_path.parent.mkdir(parents=True)
        deployment.environment_path.write_text("opaque")
        deployment.environment_path.chmod(0o644)
        with self.assertRaisesRegex(StartupError, "not securely protected"):
            install(deployment, runner=runner, euid=1000)
        self.assertEqual(runner.commands, [])

    @patch("embodied_runtime.startup.shutil.which", return_value=None)
    def test_check_uses_capture_shell_or_secure_managed_source(self, _which) -> None:
        config = self.config(cognition="openai-responses")
        captured = self.deployment(config=config, capture_env=True)
        with self.assertRaisesRegex(StartupError, "OPENAI_API_KEY"):
            check_deployment(captured, environ={}, runner=FakeRunner())
        self.assertEqual(
            check_deployment(captured, environ={"OPENAI_API_KEY": "opaque"}, runner=FakeRunner()),
            "systemd-analyze is unavailable",
        )

        managed = self.deployment(config=config)
        managed.environment_path.parent.mkdir(parents=True)
        managed.environment_path.write_text("opaque")
        managed.environment_path.chmod(0o600)
        self.assertEqual(check_deployment(managed, environ={}, runner=FakeRunner()),
                         "systemd-analyze is unavailable")
        self.assertIn("EnvironmentFile=", render_unit(managed))

    @patch("embodied_runtime.startup.shutil.which", return_value=None)
    def test_stale_environment_file_is_omitted_and_empty_capture_is_not_written(self, _which) -> None:
        deployment = self.deployment(capture_env=True)
        deployment.environment_path.parent.mkdir(parents=True)
        deployment.environment_path.write_text("OLD_SECRET=opaque\n")
        deployment.environment_path.chmod(0o600)
        self.assertNotIn("EnvironmentFile=", render_unit(deployment))
        before = deployment.environment_path.read_bytes()
        runner = FakeRunner()
        install(deployment, runner=runner, euid=1000)
        self.assertEqual(deployment.environment_path.read_bytes(), before)
        self.assertFalse(any(str(deployment.environment_path) in command
                             for command in runner.commands))

    @patch("embodied_runtime.startup.shutil.which", return_value=None)
    def test_check_is_non_mutating(self, _which) -> None:
        deployment = self.deployment()
        runner = FakeRunner()
        self.assertEqual(check_deployment(deployment, runner=runner),
                         "systemd-analyze is unavailable")
        self.assertEqual(runner.commands, [])

    @patch("embodied_runtime.startup.shutil.which", return_value=None)
    def test_install_lifecycle_start_is_explicit_and_reinstall_is_idempotent(self, _which) -> None:
        deployment = self.deployment()
        runner = FakeRunner()
        result = install(deployment, runner=runner, euid=1000)
        flattened = [command[1:] if command[0] == "sudo" else command for command in runner.commands]
        self.assertEqual(result.unit_status, "installed")
        self.assertIn(["systemctl", "daemon-reload"], flattened)
        self.assertIn(["systemctl", "enable", "mira.service"], flattened)
        self.assertNotIn(["systemctl", "start", "mira.service"], flattened)

        second = FakeRunner()
        self.assertEqual(install(deployment, runner=second, euid=1000).unit_status,
                         "already current")
        self.assertFalse(any(command[-2:-1] == ["0644"] for command in second.commands))

        started = FakeRunner()
        install(deployment, runner=started, euid=1000, start=True)
        self.assertIn(["systemctl", "start", "mira.service"], [c[1:] for c in started.commands])

    @patch("embodied_runtime.startup.shutil.which", return_value=None)
    def test_restart_required_uses_preinstall_active_state(self, _which) -> None:
        deployment = self.deployment()

        inactive = FakeRunner(active=False)
        result = install(deployment, runner=inactive, euid=1000)
        self.assertFalse(result.restart_required)

        deployment.unit_path.write_text("changed")
        inactive_start = FakeRunner(active=False)
        result = install(deployment, runner=inactive_start, euid=1000, force=True, start=True)
        self.assertFalse(result.restart_required)
        self.assertIn(["sudo", "systemctl", "start", "mira.service"], inactive_start.commands)

        active_same = FakeRunner(active=True)
        result = install(deployment, runner=active_same, euid=1000)
        self.assertFalse(result.restart_required)

        deployment.unit_path.write_text("changed again")
        active_changed = FakeRunner(active=True)
        result = install(deployment, runner=active_changed, euid=1000, force=True, start=True)
        self.assertTrue(result.restart_required)
        self.assertNotIn(["sudo", "systemctl", "start", "mira.service"],
                         active_changed.commands)

    @patch("embodied_runtime.startup.shutil.which", return_value=None)
    def test_captured_environment_requires_restart_only_when_already_active(
        self, _which,
    ) -> None:
        environment = {"OPENAI_API_KEY": "opaque-rotated-value"}
        deployment = self.deployment(
            config=self.config(cognition="openai-responses"), capture_env=True,
        )
        deployment.unit_path.parent.mkdir(parents=True)
        deployment.unit_path.write_text(render_unit(deployment))

        active = FakeRunner(active=True)
        result = install(
            deployment, environ=environment, runner=active, euid=1000, start=True,
        )
        self.assertTrue(result.restart_required)
        self.assertTrue(deployment.environment_path.is_file())
        self.assertNotIn(["sudo", "systemctl", "start", "mira.service"], active.commands)
        self.assertFalse(any("restart" in command for command in active.commands))

        inactive = FakeRunner(active=False)
        result = install(
            deployment, environ=environment, runner=inactive, euid=1000,
        )
        self.assertFalse(result.restart_required)

        no_capture = self.deployment()
        no_capture.unit_path.parent.mkdir(parents=True, exist_ok=True)
        no_capture.unit_path.write_text(render_unit(no_capture))
        result = install(no_capture, runner=FakeRunner(active=True), euid=1000)
        self.assertFalse(result.restart_required)

    def test_interactive_restart_required_skips_start_prompt_and_commands(self) -> None:
        deployment = self.deployment()
        output = io.StringIO()
        with patch(
            "embodied_runtime.startup.build_deployment", return_value=deployment,
        ), patch(
            "embodied_runtime.startup.install",
            return_value=InstallResult("already current", restart_required=True),
        ), patch(
            "embodied_runtime.startup.run_command",
        ) as command, patch(
            "builtins.input", side_effect=["y"],
        ) as prompt, redirect_stdout(output):
            self.assertEqual(wizard_main([], script_path=self.root / "wizard.py"), 0)

        self.assertEqual(prompt.call_count, 1)
        self.assertNotIn("Start Mira now?", output.getvalue())
        self.assertIn("Restart explicitly", output.getvalue())
        self.assertIn("sudo systemctl restart mira.service", output.getvalue())
        command.assert_not_called()

    def test_interactive_inactive_service_keeps_start_prompt(self) -> None:
        deployment = self.deployment()
        output = io.StringIO()
        with patch(
            "embodied_runtime.startup.build_deployment", return_value=deployment,
        ), patch(
            "embodied_runtime.startup.install",
            return_value=InstallResult("installed", restart_required=False),
        ), patch(
            "embodied_runtime.startup.run_command",
        ) as command, patch(
            "builtins.input", side_effect=["y", "n"],
        ) as prompt, redirect_stdout(output):
            self.assertEqual(wizard_main([], script_path=self.root / "wizard.py"), 0)

        self.assertEqual(prompt.call_count, 2)
        self.assertIn("Start Mira now?", prompt.call_args_list[1].args[0])
        self.assertIn("sudo systemctl start mira.service", output.getvalue())
        command.assert_not_called()

    @patch("embodied_runtime.startup.shutil.which", return_value=None)
    def test_changed_install_requires_force(self, _which) -> None:
        deployment = self.deployment()
        deployment.unit_path.parent.mkdir(parents=True)
        deployment.unit_path.write_text("unrelated unit")
        with self.assertRaisesRegex(StartupError, "--force"):
            install(deployment, runner=FakeRunner(), euid=1000)

    def test_uninstall_preserves_environment_by_default(self) -> None:
        deployment = self.deployment(capture_env=True)
        deployment.unit_path.parent.mkdir(parents=True)
        deployment.environment_path.parent.mkdir(parents=True)
        deployment.unit_path.write_text("unit")
        deployment.environment_path.write_text("secret")
        runner = FakeRunner()
        uninstall(deployment, runner=runner, euid=1000)
        self.assertFalse(deployment.unit_path.exists())
        self.assertTrue(deployment.environment_path.exists())
        flattened = [command[1:] for command in runner.commands]
        self.assertEqual(flattened[0], ["systemctl", "stop", "mira.service"])
        self.assertEqual(flattened[1], ["systemctl", "disable", "mira.service"])
        self.assertEqual(flattened[-1], ["systemctl", "daemon-reload"])

    def test_headless_launch_uses_real_cli_dependency_validation(self) -> None:
        config = self.root / "config/headless.toml"
        config.write_text('''[runtime]
profile="mira"
cognition="openai-responses"
mode="console"
[initiative]
enabled=true
messages=true
''')
        with self.assertRaisesRegex(StartupError, "operator delivery route"):
            self.deployment(config=config)
        self.assertTrue(self.deployment(config=config, with_sms=True).launch.sms_enabled)

    def test_root_requires_explicit_non_root_user(self) -> None:
        with self.assertRaisesRegex(StartupError, "requires --user"):
            build_deployment(
                repo=self.root, config=self.config(), environ={}, euid=0,
                account_lookup=lambda _: SimpleNamespace(pw_uid=1000), paths=self.paths,
            )


if __name__ == "__main__":
    unittest.main()
