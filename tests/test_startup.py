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
    local_sms_upstream, render_ngrok_config, render_ngrok_unit, render_unit,
    render_ngrok_state, required_environment, select_config, serialize_environment,
    serialize_ngrok_environment, uninstall, validate_managed_environment_file,
    validate_ngrok_domain, validate_ngrok_config, wizard_main,
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
            Path(plain[-1]).chmod(int(plain[plain.index("-m") + 1], 8))
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

    def config(self, *, cognition="none", vision="none", tts="espeak", sms=False,
               sms_port=8080, webhook_path="/sms") -> Path:
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
bind_port = {sms_port}
webhook_path = "{webhook_path}"
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

    def ngrok_deployment(self, **kwargs) -> Deployment:
        binary = self.root / "tools with spaces/ngrok"
        binary.parent.mkdir(exist_ok=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        runner = lambda command, check=True: SimpleNamespace(
            returncode=0, stdout="ngrok version 3.20.0\n", stderr="",
        )
        config = kwargs.pop("config", None) or self.config(sms=True)
        return self.deployment(
            config=config, with_ngrok=True, ngrok=binary,
            ngrok_domain=kwargs.pop("ngrok_domain", "mira-example.ngrok-free.app"),
            runner=runner, **kwargs,
        )

    def test_ngrok_domain_and_local_upstream_validation(self) -> None:
        self.assertEqual(validate_ngrok_domain("Mira-Example.Ngrok-Free.App."),
                         "mira-example.ngrok-free.app")
        for invalid in ("https://host/something", "host:1234", "user@host", "host?x=1"):
            with self.subTest(invalid=invalid), self.assertRaises(StartupError):
                validate_ngrok_domain(invalid)
        self.assertEqual(local_sms_upstream("0.0.0.0", 8080), "http://127.0.0.1:8080")
        self.assertEqual(local_sms_upstream("::1", 8080), "http://[::1]:8080")
        with self.assertRaisesRegex(StartupError, "local"):
            local_sms_upstream("example.com", 8080)

    def test_ngrok_rendering_is_stable_secret_free_and_independent(self) -> None:
        deployment = self.ngrok_deployment(capture_env=True)
        config = render_ngrok_config(deployment)
        unit = render_ngrok_unit(deployment)
        ingress = serialize_ngrok_environment(
            deployment, {"NGROK_AUTHTOKEN": "actual-token"},
        ).decode()
        self.assertIn("version: 3", config)
        self.assertNotIn("authtoken:", config)
        self.assertNotIn("$NGROK_AUTHTOKEN", config)
        self.assertNotIn("actual-token", config)
        self.assertIn('url: "https://mira-example.ngrok-free.app"', config)
        self.assertIn('url: "http://127.0.0.1:8080"', config)
        self.assertEqual(deployment.public_webhook_url,
                         "https://mira-example.ngrok-free.app/sms")
        for expected in ("Type=simple", "User=robot", "Restart=on-failure",
                         "StandardOutput=journal", "WantedBy=multi-user.target",
                         str(deployment.ngrok_environment_path)):
            self.assertIn(expected, unit)
        self.assertIn('"' + str(deployment.ngrok) + '"', unit)
        for forbidden in ("Requires=mira.service", "ExecStartPre", "service install",
                          "actual-token", "TWILIO_AUTH_TOKEN", "OPENAI_API_KEY"):
            self.assertNotIn(forbidden, config + unit)
        self.assertNotIn("Requires=mira-ngrok.service", render_unit(deployment))
        self.assertEqual(config.splitlines()[2], "endpoints:")
        self.assertIn(
            f"EnvironmentFile={deployment.ngrok_environment_path}", unit,
        )
        self.assertEqual(ingress, 'NGROK_AUTHTOKEN="actual-token"\n')
        self.assertIn(
            f'"start" "{deployment.service_name}-sms" "--config"', unit,
        )

    def test_ngrok_config_check_uses_temporary_config_without_starting(self) -> None:
        deployment = self.ngrok_deployment(capture_env=True)
        commands = []
        temporary_path = None

        def runner(command, *, check=True):
            nonlocal temporary_path
            commands.append(list(command))
            temporary_path = Path(command[-1])
            self.assertEqual(temporary_path.read_text(), render_ngrok_config(deployment))
            return SimpleNamespace(returncode=0, stdout="Valid configuration file", stderr="")

        self.assertIsNone(validate_ngrok_config(
            deployment, environ={"NGROK_AUTHTOKEN": "opaque"}, runner=runner,
        ))
        self.assertEqual(commands[0][1:4], ["config", "check", "--config"])
        self.assertNotIn("opaque", commands[0])
        self.assertFalse(any("start" in command for command in commands))
        self.assertFalse(temporary_path.exists())
        self.assertIn("skipped", validate_ngrok_config(
            deployment, environ={}, runner=runner,
        ))

    @patch("embodied_runtime.startup.shutil.which", return_value=None)
    def test_public_webhook_state_detects_domain_and_path_not_upstream(self, _which) -> None:
        environment = {
            "TWILIO_ACCOUNT_SID": "sid", "TWILIO_AUTH_TOKEN": "token",
            "TWILIO_PHONE_NUMBER": "phone", "MIRA_SMS_OPERATOR_NUMBER": "operator",
            "NGROK_AUTHTOKEN": "ngrok-token",
        }
        original = self.ngrok_deployment(
            capture_env=True, ngrok_domain="one.ngrok-free.app",
        )
        install(original, environ=environment, runner=FakeRunner(), euid=1000)
        self.assertEqual(
            original.ngrok_state_path.read_text(),
            "public_webhook_url=https://one.ngrok-free.app/sms\n",
        )

        same = self.ngrok_deployment(ngrok_domain="one.ngrok-free.app")
        self.assertEqual(install(same, runner=FakeRunner(), euid=1000).unit_status,
                         "already current")

        changed_domain = self.ngrok_deployment(ngrok_domain="two.ngrok-free.app")
        with self.assertRaisesRegex(StartupError, "--capture-env"):
            install(changed_domain, runner=FakeRunner(), euid=1000, force=True)

        path_config = self.config(sms=True, webhook_path="/twilio")
        changed_path = self.ngrok_deployment(
            config=path_config, capture_env=True, ngrok_domain="one.ngrok-free.app",
        )
        existing_ngrok_environment = changed_path.ngrok_environment_path.read_bytes()
        runtime_only_environment = {
            name: value for name, value in environment.items()
            if name != "NGROK_AUTHTOKEN"
        }
        result = install(
            changed_path, environ=runtime_only_environment,
            runner=FakeRunner(active=True),
            euid=1000, force=True,
        )
        self.assertTrue(result.restart_required)
        self.assertFalse(result.ngrok_restart_required)
        self.assertEqual(changed_path.ngrok_environment_path.read_bytes(),
                         existing_ngrok_environment)
        self.assertEqual(
            changed_path.ngrok_state_path.read_text(),
            "public_webhook_url=https://one.ngrok-free.app/twilio\n",
        )

        # Restore the public path, then change only the private upstream port.
        install(original, environ=environment, runner=FakeRunner(), euid=1000, force=True)
        port_config = self.config(sms=True, sms_port=8081)
        changed_port = self.ngrok_deployment(
            config=port_config, ngrok_domain="one.ngrok-free.app",
        )
        result = install(
            changed_port, runner=FakeRunner(active=True), euid=1000, force=True,
        )
        self.assertFalse(result.restart_required)
        self.assertTrue(result.ngrok_restart_required)

    @patch("embodied_runtime.startup.shutil.which", return_value=None)
    def test_webhook_capture_also_captures_an_explicit_token_rotation(self, _which) -> None:
        environment = {
            "TWILIO_ACCOUNT_SID": "sid", "TWILIO_AUTH_TOKEN": "token",
            "TWILIO_PHONE_NUMBER": "phone", "MIRA_SMS_OPERATOR_NUMBER": "operator",
            "NGROK_AUTHTOKEN": "old-token",
        }
        original = self.ngrok_deployment(
            capture_env=True, ngrok_domain="one.ngrok-free.app",
        )
        install(original, environ=environment, runner=FakeRunner(), euid=1000)
        path_config = self.config(sms=True, webhook_path="/twilio")
        changed = self.ngrok_deployment(
            config=path_config, capture_env=True, ngrok_domain="one.ngrok-free.app",
        )
        rotated = {**environment, "NGROK_AUTHTOKEN": "new-token"}
        result = install(
            changed, environ=rotated, runner=FakeRunner(active=True),
            euid=1000, force=True,
        )
        self.assertTrue(result.restart_required)
        self.assertTrue(result.ngrok_restart_required)
        self.assertEqual(changed.ngrok_environment_path.read_text(),
                         'NGROK_AUTHTOKEN="new-token"\n')

    @patch("embodied_runtime.startup.shutil.which", return_value=None)
    def test_token_only_rotation_does_not_rewrite_runtime_environment(self, _which) -> None:
        environment = {
            "TWILIO_ACCOUNT_SID": "sid", "TWILIO_AUTH_TOKEN": "token",
            "TWILIO_PHONE_NUMBER": "phone", "MIRA_SMS_OPERATOR_NUMBER": "operator",
            "NGROK_AUTHTOKEN": "old-token",
        }
        original = self.ngrok_deployment(capture_env=True)
        install(original, environ=environment, runner=FakeRunner(), euid=1000)
        runtime_before = original.environment_path.read_bytes()
        rotation = self.ngrok_deployment(capture_env=True)
        result = install(
            rotation, environ={"NGROK_AUTHTOKEN": "new-token"},
            runner=FakeRunner(active=True), euid=1000,
        )
        self.assertFalse(result.restart_required)
        self.assertTrue(result.ngrok_restart_required)
        self.assertEqual(rotation.environment_path.read_bytes(), runtime_before)
        self.assertEqual(rotation.ngrok_environment_path.read_text(),
                         'NGROK_AUTHTOKEN="new-token"\n')

    @patch("embodied_runtime.startup.shutil.which", return_value=None)
    def test_check_enforces_webhook_state_and_reuses_managed_ngrok_token(self, _which) -> None:
        environment = {
            "TWILIO_ACCOUNT_SID": "sid", "TWILIO_AUTH_TOKEN": "token",
            "TWILIO_PHONE_NUMBER": "phone", "MIRA_SMS_OPERATOR_NUMBER": "operator",
            "NGROK_AUTHTOKEN": "managed-token",
        }
        original = self.ngrok_deployment(capture_env=True)
        install(original, environ=environment, runner=FakeRunner(), euid=1000)
        changed_config = self.config(sms=True, webhook_path="/twilio")
        uncaptured = self.ngrok_deployment(config=changed_config)
        runner = FakeRunner()
        with self.assertRaisesRegex(StartupError, "changed public webhook.*--capture-env"):
            check_deployment(uncaptured, environ={}, runner=runner)
        self.assertEqual(runner.commands, [])

        captured = self.ngrok_deployment(config=changed_config, capture_env=True)
        runtime_environment = {
            name: value for name, value in environment.items()
            if name != "NGROK_AUTHTOKEN"
        }
        warning = check_deployment(
            captured, environ=runtime_environment, runner=FakeRunner(),
        )
        self.assertIn("NGROK_AUTHTOKEN is not loaded", warning)
        self.assertEqual(captured.ngrok_environment_path.read_text(),
                         'NGROK_AUTHTOKEN="managed-token"\n')

    def test_ngrok_capture_has_least_privilege_and_derives_webhook(self) -> None:
        deployment = self.ngrok_deployment(capture_env=True)
        environment = {
            "TWILIO_ACCOUNT_SID": "sid", "TWILIO_AUTH_TOKEN": "twilio-secret",
            "TWILIO_PHONE_NUMBER": "+15550001", "MIRA_SMS_OPERATOR_NUMBER": "+15550002",
            "NGROK_AUTHTOKEN": "ngrok-secret", "OPENAI_API_KEY": "not-required",
        }
        runtime = serialize_environment(deployment, environment).decode()
        ingress = serialize_ngrok_environment(deployment, environment).decode()
        self.assertIn('TWILIO_WEBHOOK_URL="https://mira-example.ngrok-free.app/sms"', runtime)
        self.assertNotIn("NGROK_AUTHTOKEN", runtime)
        self.assertEqual(ingress, 'NGROK_AUTHTOKEN="ngrok-secret"\n')
        self.assertNotIn("twilio-secret", ingress)

    @patch("embodied_runtime.startup.shutil.which", return_value=None)
    def test_ngrok_install_enables_both_and_starts_in_order(self, _which) -> None:
        deployment = self.ngrok_deployment(capture_env=True)
        environment = {
            "TWILIO_ACCOUNT_SID": "sid", "TWILIO_AUTH_TOKEN": "token",
            "TWILIO_PHONE_NUMBER": "phone", "MIRA_SMS_OPERATOR_NUMBER": "operator",
            "NGROK_AUTHTOKEN": "ngrok-token",
        }
        runner = FakeRunner()
        install(deployment, environ=environment, runner=runner, euid=1000, start=True)
        flattened = [c[1:] if c[0] == "sudo" else c for c in runner.commands]
        self.assertIn(["systemctl", "enable", "mira.service"], flattened)
        self.assertIn(["systemctl", "enable", "mira-ngrok.service"], flattened)
        runtime_start = flattened.index(["systemctl", "start", "mira.service"])
        ingress_start = flattened.index(["systemctl", "start", "mira-ngrok.service"])
        self.assertLess(runtime_start, ingress_start)

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
        self.assertIn("Restart required", output.getvalue())
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
        self.assertIn("Start Mira services now?", prompt.call_args_list[1].args[0])
        self.assertIn("sudo systemctl start mira.service", output.getvalue())
        command.assert_not_called()

    def test_interactive_can_enable_ngrok_and_show_public_webhook(self) -> None:
        sms = self.deployment(config=self.config(sms=True))
        ingress = self.ngrok_deployment(capture_env=False)
        output = io.StringIO()
        with patch(
            "embodied_runtime.startup.build_deployment", side_effect=[sms, ingress],
        ), patch(
            "embodied_runtime.startup.shutil.which", return_value=str(ingress.ngrok),
        ), patch(
            "builtins.input",
            side_effect=["y", "", "mira-example.ngrok-free.app", "n"],
        ), redirect_stdout(output):
            self.assertEqual(wizard_main([], script_path=self.root / "wizard.py"), 0)
        rendered = output.getvalue()
        self.assertIn("ngrok ingress\n  enabled", rendered)
        self.assertIn("ngrok version 3.20.0", rendered)
        self.assertIn("https://mira-example.ngrok-free.app/sms", rendered)

    def test_interactive_ngrok_start_uses_two_service_order(self) -> None:
        sms = self.deployment(config=self.config(sms=True))
        ingress = self.ngrok_deployment(capture_env=False)
        result = InstallResult("installed", ngrok_unit_status="installed")
        output = io.StringIO()
        with patch(
            "embodied_runtime.startup.build_deployment", side_effect=[sms, ingress],
        ), patch(
            "embodied_runtime.startup.shutil.which", return_value=str(ingress.ngrok),
        ), patch(
            "embodied_runtime.startup.install", return_value=result,
        ), patch(
            "embodied_runtime.startup.start_inactive_services",
        ) as start, patch(
            "builtins.input",
            side_effect=["y", "", "mira-example.ngrok-free.app", "y", "y", "y"],
        ), redirect_stdout(output):
            self.assertEqual(wizard_main([], script_path=self.root / "wizard.py"), 0)
        start.assert_called_once()
        self.assertTrue(start.call_args.args[0].capture_env)

        runner = FakeRunner()
        from embodied_runtime.startup import start_inactive_services
        start_inactive_services(ingress, runner=runner, euid=1000)
        starts = [command for command in runner.commands if "start" in command]
        self.assertEqual(starts, [
            ["sudo", "systemctl", "start", "mira.service"],
            ["sudo", "systemctl", "start", "mira-ngrok.service"],
        ])

    def test_interactive_reports_both_restarts_without_start_prompt(self) -> None:
        sms = self.deployment(config=self.config(sms=True))
        ingress = self.ngrok_deployment(capture_env=False)
        result = InstallResult(
            "installed", restart_required=True, ngrok_unit_status="installed",
            ngrok_restart_required=True,
        )
        output = io.StringIO()
        with patch(
            "embodied_runtime.startup.build_deployment", side_effect=[sms, ingress],
        ), patch(
            "embodied_runtime.startup.shutil.which", return_value=str(ingress.ngrok),
        ), patch(
            "embodied_runtime.startup.install", return_value=result,
        ), patch(
            "embodied_runtime.startup.start_inactive_services",
        ) as start, patch(
            "builtins.input",
            side_effect=["y", "", "mira-example.ngrok-free.app", "y", "y"],
        ) as prompt, redirect_stdout(output):
            self.assertEqual(wizard_main([], script_path=self.root / "wizard.py"), 0)
        self.assertEqual(prompt.call_count, 5)
        self.assertIn("sudo systemctl restart mira.service", output.getvalue())
        self.assertIn("sudo systemctl restart mira-ngrok.service", output.getvalue())
        self.assertNotIn("Start Mira services now?", output.getvalue())
        start.assert_not_called()

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
