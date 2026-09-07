from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from embodied_runtime.cli import main, parse_launch_arguments
from embodied_runtime.config import (
    ConfigurationError, HISTORICAL_DEFAULTS, load_runtime_config,
)


EXPLICIT_AGENTIC = [
    "--hardware", "fusion-hat", "--camera", "picamera2",
    "--cognition", "openai-responses",
    "--vision", "openai-responses", "--initiative",
    "--initiative-platform-attention", "--initiative-actions",
    "--initiative-messages", "--initiative-continuation",
    "--initiative-goal-closure", "--console",
    "--voice", "--tts", "openai",
    "--openai-tts-model", "gpt-4o-mini-tts",
    "--openai-tts-voice", "marin",
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
        self.assertEqual(
            configured,
            explicit.__class__(**{
                **explicit.__dict__,
                "voice_wake_word_enabled": True,
                "voice_wake_words": ["mira", "mirror", "huh mirror"],
                "voice_initial_timeout_seconds": 18,
                "voice_followup_timeout_seconds": 12,
            }),
        )

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

    def test_vision_defaults_toml_and_cli_precedence(self):
        self.assertEqual(HISTORICAL_DEFAULTS.vision, "none")
        configured = self.effective(
            "[runtime]\ncamera='picamera2'\ncognition='openai-responses'\n"
            "vision='openai-responses'\n"
        )
        self.assertEqual(configured.vision, "openai-responses")
        overridden = self.effective(
            "[runtime]\nvision='openai-responses'\n", ("--vision", "none")
        )
        self.assertEqual(overridden.vision, "none")

    def test_vision_dependencies_are_validated_after_merge(self):
        for contents in (
            "[runtime]\nvision='openai-responses'\ncognition='openai-responses'\n",
            "[runtime]\nvision='openai-responses'\ncamera='picamera2'\n",
        ):
            path = self.write(contents)
            with patch("sys.stderr"), self.assertRaises(SystemExit):
                main(["--config", str(path)])

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

    def test_goal_closure_config_requires_only_initiative(self):
        valid = self.write(
            "[runtime]\ncognition='openai-responses'\nmode='console'\n"
            "[initiative]\nenabled=true\ngoal_closure=true\n"
        )
        with patch(
            "embodied_runtime.cli._run_application", new=AsyncMock(return_value=0)
        ) as run:
            self.assertEqual(main(["--config", str(valid)]), 0)
        args = run.await_args.args[0]
        self.assertTrue(args.initiative_goal_closure)
        self.assertFalse(args.initiative_actions)
        self.assertFalse(args.initiative_messages)

    def test_unknown_sections_and_keys_are_rejected(self):
        for contents, key in (
            ("[banana]\nenabled=true\n", "banana"),
            ("[runtime]\ncamrea='none'\n", "runtime.camrea"),
            ("[initiative]\nfree_will=10\n", "initiative.free_will"),
            ("[cognition]\napi_key='secret'\n", "cognition"),
            ("[voice]\ngain_db=30\n", "voice.gain_db"),
            ("[voice]\nwake_word='mira'\n", "voice.wake_word"),
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
            ("[voice]\nenabled='yes'\n", "voice.enabled must be boolean"),
            ("[voice]\ninitial_timeout_seconds=0\n", "voice.initial_timeout_seconds must be a positive number"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                ConfigurationError, message
            ):
                load_runtime_config(self.write(contents))

    def test_voice_is_opt_in_with_small_bounded_timeout_config(self):
        effective = self.effective(
            "[voice]\nenabled=true\ninitial_timeout_seconds=15\n"
            "followup_timeout_seconds=8.5\n"
        )
        self.assertTrue(effective.voice_enabled)
        self.assertEqual(effective.voice_initial_timeout_seconds, 15)
        self.assertEqual(effective.voice_followup_timeout_seconds, 8.5)

    def test_tts_defaults_to_espeak_and_accepts_piper_model(self):
        self.assertEqual(HISTORICAL_DEFAULTS.voice_tts, "espeak")
        self.assertIsNone(HISTORICAL_DEFAULTS.voice_piper_model)
        effective = self.effective(
            "[voice]\ntts='piper'\npiper_model='~/voice.onnx'\n"
        )
        self.assertEqual(effective.voice_tts, "piper")
        self.assertEqual(effective.voice_piper_model, "~/voice.onnx")

    def test_openai_tts_defaults_and_cli_precedence(self):
        effective = self.effective("[voice]\ntts='openai'\n")
        self.assertEqual(effective.voice_tts, "openai")
        self.assertEqual(effective.voice_openai_tts_model, "gpt-4o-mini-tts")
        self.assertEqual(effective.voice_openai_tts_voice, "cedar")

        effective = self.effective(
            "[voice]\ntts='openai'\nopenai_tts_model='configured-model'\n"
            "openai_tts_voice='configured-voice'\n",
            ("--openai-tts-model", "cli-model", "--openai-tts-voice", "cli-voice"),
        )
        self.assertEqual(effective.voice_openai_tts_model, "cli-model")
        self.assertEqual(effective.voice_openai_tts_voice, "cli-voice")

    def test_openai_tts_values_must_be_strings(self):
        for key in ("openai_tts_model", "openai_tts_voice"):
            with self.subTest(key=key), self.assertRaisesRegex(
                ConfigurationError, f"voice.{key} must be a string"
            ):
                load_runtime_config(self.write(f"[voice]\n{key}=7\n"))

    def test_piper_requires_model_path_after_cli_and_file_merge(self):
        path = self.write("[voice]\ntts='piper'\n")
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            main(["--config", str(path)])
        overridden = self.effective(
            "[voice]\ntts='piper'\npiper_model='/configured/model.onnx'\n",
            ("--tts", "espeak"),
        )
        self.assertEqual(overridden.voice_tts, "espeak")

    def test_unsupported_tts_is_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, "unsupported value for voice.tts"):
            load_runtime_config(self.write("[voice]\ntts='cloud'\n"))

    def test_wake_words_configuration_is_strict_and_opt_in(self):
        effective = self.effective(
            "[voice]\nenabled=true\nwake_word_enabled=true\n"
            "wake_words=['Mira', 'mirror']\n"
        )
        self.assertTrue(effective.voice_wake_word_enabled)
        self.assertEqual(effective.voice_wake_words, ["Mira", "mirror"])

    def test_wake_words_default_is_mira(self):
        self.assertEqual(HISTORICAL_DEFAULTS.voice_wake_words, ["mira"])
        self.assertEqual(self.effective("[voice]\nenabled=true\n").voice_wake_words,
                         ["mira"])

    def test_invalid_wake_words_are_rejected(self):
        for value, message in (
            ("[]", "must be a non-empty list"),
            ("['mira', '   ']", "entries must be non-empty strings"),
            ("['mira', 7]", "entries must be non-empty strings"),
            ("'mira'", "must be a non-empty list"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                ConfigurationError, message
            ):
                load_runtime_config(self.write(f"[voice]\nwake_words={value}\n"))

    def test_unsupported_enums_are_rejected(self):
        for key in ("hardware", "camera", "cognition", "vision", "mode"):
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
