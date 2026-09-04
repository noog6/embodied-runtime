from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from embodied_runtime.cli import main, parse_launch_arguments
from embodied_runtime.config import (
    ConfigurationError, HISTORICAL_DEFAULTS, load_runtime_config,
)


EXPLICIT_AGENTIC = [
    "--camera", "picamera2", "--cognition", "openai-responses", "--initiative",
    "--initiative-platform-attention", "--initiative-actions",
    "--initiative-messages", "--initiative-continuation",
    "--initiative-goal-closure", "--console",
]


class ConfigurationTests(unittest.TestCase):
    def write(self, contents: str) -> Path:
        temporary = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        self.addCleanup(Path(temporary.name).unlink, missing_ok=True)
        with temporary:
            temporary.write(contents)
        return Path(temporary.name)

    def effective(self, contents: str, extra=()):
        path = self.write(contents)
        return parse_launch_arguments(["--config", str(path), *extra])[2]

    def test_checked_in_config_matches_full_explicit_launch(self):
        configured = parse_launch_arguments(
            ["--config", "config/mira-agentic.toml"]
        )[2]
        explicit = parse_launch_arguments(EXPLICIT_AGENTIC)[2]
        self.assertEqual(configured, explicit)

    def test_no_arguments_preserves_historical_defaults(self):
        self.assertEqual(parse_launch_arguments([])[2], HISTORICAL_DEFAULTS)

    def test_partial_config_uses_historical_defaults(self):
        effective = self.effective("[runtime]\ncamera = 'picamera2'\n")
        self.assertEqual(effective.camera, "picamera2")
        self.assertEqual(
            effective,
            HISTORICAL_DEFAULTS.__class__(
                **{**HISTORICAL_DEFAULTS.__dict__, "camera": "picamera2"}
            ),
        )

    def test_cli_scalars_and_modes_override_file(self):
        effective = self.effective(
            "[runtime]\ncamera='none'\ncognition='none'\nmode='run'\n",
            ("--camera", "picamera2", "--cognition", "openai-responses", "--console"),
        )
        self.assertEqual((effective.camera, effective.cognition, effective.mode),
                         ("picamera2", "openai-responses", "console"))
        effective = self.effective("[runtime]\nmode='console'\n", ("--diagnostics",))
        self.assertEqual(effective.mode, "diagnostics")

    def test_positive_boolean_flags_override_false_and_absence_preserves_true(self):
        effective = self.effective(
            "[initiative]\nenabled=false\nplatform_attention=false\n",
            ("--initiative", "--initiative-platform-attention",
             "--cognition", "openai-responses"),
        )
        self.assertTrue(effective.initiative)
        self.assertTrue(effective.initiative_platform_attention)
        configured_true = self.effective("[initiative]\nenabled=true\nactions=true\n")
        self.assertTrue(configured_true.initiative)
        self.assertTrue(configured_true.initiative_actions)

    def test_dependency_validation_occurs_after_merge(self):
        valid = self.write(
            "[runtime]\ncognition='openai-responses'\nmode='console'\n"
            "[initiative]\nenabled=true\nmessages=true\n"
        )
        with patch("embodied_runtime.cli._run_application", new=AsyncMock(return_value=0)):
            self.assertEqual(main(["--config", str(valid)]), 0)
        invalid = self.write(
            "[runtime]\ncognition='openai-responses'\nmode='run'\n"
            "[initiative]\nenabled=true\nmessages=true\n"
        )
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            main(["--config", str(invalid)])

    def test_unknown_sections_and_keys_are_rejected(self):
        for contents, key in (
            ("[banana]\nenabled=true\n", "banana"),
            ("[runtime]\ncamrea='none'\n", "runtime.camrea"),
            ("[initiative]\nfree_will=10\n", "initiative.free_will"),
            ("[cognition]\napi_key='secret'\n", "cognition"),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(
                ConfigurationError, f"unknown configuration key: {key}"
            ):
                load_runtime_config(self.write(contents))

    def test_wrong_types_are_rejected(self):
        for contents, message in (
            ("[initiative]\nenabled='true'\n", "initiative.enabled must be boolean"),
            ("[initiative]\nactions=1\n", "initiative.actions must be boolean"),
            ("[runtime]\ncamera=true\n", "runtime.camera must be a string"),
            ("[runtime]\nmode=7\n", "runtime.mode must be a string"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                ConfigurationError, message
            ):
                load_runtime_config(self.write(contents))

    def test_unsupported_enums_are_rejected(self):
        for key in ("hardware", "camera", "cognition", "mode"):
            with self.subTest(key=key), self.assertRaisesRegex(
                ConfigurationError, f"unsupported value for runtime.{key}"
            ):
                load_runtime_config(self.write(f"[runtime]\n{key}='invalid'\n"))

    def test_missing_and_malformed_files_fail_cleanly(self):
        missing = Path(tempfile.gettempdir()) / "embodied-runtime-missing-config.toml"
        missing.unlink(missing_ok=True)
        with self.assertRaisesRegex(ConfigurationError, "configuration file not found"):
            load_runtime_config(missing)
        malformed = self.write("[runtime\ncamera='none'")
        with self.assertRaisesRegex(ConfigurationError, "invalid configuration"):
            load_runtime_config(malformed)
        for path in (missing, malformed):
            with self.subTest(path=path), patch("sys.stderr") as stderr, \
                    self.assertRaises(SystemExit):
                main(["--config", str(path)])
            self.assertNotIn("Traceback", "".join(
                call.args[0] for call in stderr.write.call_args_list
            ))
