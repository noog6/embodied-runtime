from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from embodied_runtime.cli import (
    build_job_store, build_persistent_memory_store, build_text_to_speech_provider,
    main, parse_launch_arguments,
)
from embodied_runtime.jobs import SQLiteJobStore
from embodied_runtime.memory import SQLiteMemoryStore
from embodied_runtime.config import (
    ConfigurationError, HISTORICAL_DEFAULTS, load_runtime_config,
)


EXPLICIT_AGENTIC = [
    "--hardware", "auto", "--camera", "auto",
    "--cognition", "openai-responses",
    "--cognition-model", "gpt-5.6-luna",
    "--vision", "openai-responses", "--initiative",
    "--initiative-platform-attention", "--initiative-actions",
    "--initiative-messages", "--initiative-continuation",
    "--initiative-goal-closure", "--console",
    "--voice", "--tts", "elevenlabs", "--fallback-tts", "espeak",
    "--openai-tts-model", "gpt-4o-mini-tts",
    "--openai-tts-voice", "marin",
    "--elevenlabs-tts-model", "eleven_flash_v2_5",
    "--elevenlabs-tts-voice-id", "pFZP5JQG7iQjIQuC4Bku",
    "--elevenlabs-tts-speed", "1.1",
]


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.history_patch = patch(
            "embodied_runtime.cli.start_run", side_effect=OSError("unavailable")
        )
        self.history_patch.start()

    def tearDown(self) -> None:
        self.history_patch.stop()

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
                "timezone": "America/Toronto",
                "memory_enabled": True,
                "memory_database_path": Path("data/mira-memory.sqlite3").resolve(),
                "jobs_enabled": True,
                "jobs_database_path": Path("data/jobs.sqlite3").resolve(),
                "jobs_auto_continue": True,
                "jobs_max_concurrent_work": 4,
                "findings_context_selection_enabled": True,
            }),
        )

    def test_no_arguments_preserves_historical_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(parse_launch_arguments([])[2], HISTORICAL_DEFAULTS)
        self.assertEqual(HISTORICAL_DEFAULTS.interaction_environment, "workstation")
        self.assertFalse(HISTORICAL_DEFAULTS.sms_enabled)
        self.assertTrue(HISTORICAL_DEFAULTS.earcons_enabled)
        self.assertEqual(HISTORICAL_DEFAULTS.jobs_max_concurrent_work, 1)

    def test_cognition_model_configuration_is_strict_and_optional(self):
        configured = load_runtime_config(self.write(
            "[cognition]\nmodel = '  gpt-5.6-luna  '\n"
        ))
        self.assertEqual(configured.cognition.model, "gpt-5.6-luna")
        self.assertIsNone(load_runtime_config(self.write("")).cognition.model)
        for contents in (
            "[cognition]\nunknown = 'value'\n",
            "[cognition]\nmodel = 123\n",
            "[cognition]\nmodel = ''\n",
            "[cognition]\nmodel = '   '\n",
        ):
            with self.subTest(contents=contents), self.assertRaises(ConfigurationError):
                load_runtime_config(self.write(contents))

    def test_cognition_model_precedence(self):
        with patch.dict("os.environ", {"OPENAI_MODEL": "environment-model"}, clear=True):
            self.assertEqual(self.effective("").cognition_model, "environment-model")
            self.assertEqual(
                self.effective("[cognition]\nmodel='file-model'\n").cognition_model,
                "file-model",
            )
            self.assertEqual(
                self.effective(
                    "[cognition]\nmodel='file-model'\n",
                    ("--cognition-model", " cli-model "),
                ).cognition_model,
                "cli-model",
            )
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.effective("").cognition_model, "gpt-5.6-luna")

    def test_empty_cognition_model_cli_is_rejected(self):
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            parse_launch_arguments(["--cognition-model", "   "])

    def test_job_concurrent_work_configuration_and_cli_override(self):
        self.assertEqual(
            self.effective("[jobs]\nmax_concurrent_work=4\n").jobs_max_concurrent_work,
            4,
        )
        self.assertEqual(
            self.effective("[jobs]\nmax_concurrent_work=32\n").jobs_max_concurrent_work,
            32,
        )
        self.assertEqual(
            self.effective("[jobs]\nmax_concurrent_work=256\n").jobs_max_concurrent_work,
            256,
        )
        self.assertEqual(
            self.effective(
                "[jobs]\nmax_concurrent_work=4\n",
                ("--jobs-max-concurrent-work", "32"),
            ).jobs_max_concurrent_work,
            32,
        )

    def test_job_concurrent_work_configuration_rejects_invalid_values(self):
        for value in ("0", "-1", "257", "true", "1.5"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ConfigurationError, "max_concurrent_work"
            ):
                load_runtime_config(self.write(
                    f"[jobs]\nmax_concurrent_work={value}\n"
                ))

    def test_all_hardware_selection_values_are_accepted(self):
        for hardware in ("auto", "virtual", "host", "fusion-hat"):
            with self.subTest(hardware=hardware):
                self.assertEqual(
                    self.effective(f"[runtime]\nhardware='{hardware}'\n").hardware,
                    hardware,
                )

    def test_all_camera_selection_values_are_accepted(self):
        for camera in ("auto", "none", "picamera2"):
            with self.subTest(camera=camera):
                self.assertEqual(
                    self.effective(f"[runtime]\ncamera='{camera}'\n").camera,
                    camera,
                )

    def test_earcons_config_and_cli_disable_are_strict_and_independent_of_tts(self):
        self.assertTrue(self.effective("[earcons]\nenabled=true\n").earcons_enabled)
        self.assertFalse(self.effective("[earcons]\nenabled=false\n").earcons_enabled)
        overridden = self.effective(
            "[runtime]\nhardware='fusion-hat'\n"
            "[earcons]\nenabled=true\n"
            "[voice]\nenabled=true\ntts='espeak'\n",
            ("--no-earcons",),
        )
        self.assertFalse(overridden.earcons_enabled)
        self.assertTrue(overridden.voice_enabled)
        self.assertEqual(overridden.voice_tts, "espeak")
        args = parse_launch_arguments([
            "--hardware", "fusion-hat", "--voice", "--no-earcons",
        ])[1]
        self.assertIsNotNone(build_text_to_speech_provider(args))
        with self.assertRaisesRegex(ConfigurationError, "earcons.enabled"):
            load_runtime_config(self.write("[earcons]\nenabled='yes'\n"))

    def test_sms_configuration_is_strict_and_cli_can_opt_in(self):
        effective = self.effective(
            "[sms]\nenabled=false\nbackend='twilio'\nbind_host='0.0.0.0'\n"
            "bind_port=8081\nwebhook_path='/incoming'\n",
            ("--sms",),
        )
        self.assertTrue(effective.sms_enabled)
        self.assertEqual(
            (effective.sms_backend, effective.sms_bind_host,
             effective.sms_bind_port, effective.sms_webhook_path),
            ("twilio", "0.0.0.0", 8081, "/incoming"),
        )
        invalid = (
            "[sms]\nbackend='other'\n",
            "[sms]\nbind_port=0\n",
            "[sms]\nwebhook_path='sms'\n",
            "[sms]\nbind_host=''\n",
        )
        for contents in invalid:
            with self.subTest(contents=contents), self.assertRaises(ConfigurationError):
                load_runtime_config(self.write(contents))

    def test_interaction_environment_is_strict_and_resolved(self):
        for environment in ("workstation", "companion", "unattended", "remote"):
            self.assertEqual(
                self.effective(f"[interaction]\nenvironment='{environment}'\n")
                .interaction_environment,
                environment,
            )
        with self.assertRaisesRegex(ConfigurationError, "unsupported value"):
            load_runtime_config(self.write("[interaction]\nenvironment='nearby'\n"))

    def test_partial_config_uses_historical_defaults(self):
        effective = self.effective("[runtime]\ncamera = 'picamera2'\n")
        self.assertEqual(effective.camera, "picamera2")
        self.assertEqual(
            effective,
            HISTORICAL_DEFAULTS.__class__(
                **{**HISTORICAL_DEFAULTS.__dict__, "camera": "picamera2"}
            ),
        )

    def test_power_policy_defaults_overrides_and_validation(self):
        defaults = self.effective("")
        self.assertEqual(
            (defaults.power_interval_seconds, defaults.power_attention_voltage_v,
             defaults.power_recovery_voltage_v),
            (30.0, 7.4, 7.7),
        )
        configured = self.effective(
            "[power]\ninterval_seconds=12.5\nattention_voltage_v=7.2\n"
            "recovery_voltage_v=7.8\n"
        )
        self.assertEqual(
            (configured.power_interval_seconds,
             configured.power_attention_voltage_v,
             configured.power_recovery_voltage_v),
            (12.5, 7.2, 7.8),
        )
        for contents in (
            "[power]\ninterval_seconds=0\n",
            "[power]\ninterval_seconds='often'\n",
            "[power]\nattention_voltage_v=7.7\nrecovery_voltage_v=7.7\n",
        ):
            with self.subTest(contents=contents), self.assertRaises(ConfigurationError):
                load_runtime_config(self.write(contents))

    def test_jobs_configuration_is_independent_and_resolves_relative_path(self):
        effective = self.effective(
            "[jobs]\nenabled=true\ndatabase_path='data/jobs.sqlite3'\n"
            "[memory]\nenabled=false\n"
        )
        self.assertTrue(effective.jobs_enabled)
        self.assertFalse(effective.memory_enabled)
        self.assertEqual(effective.jobs_database_path.name, "jobs.sqlite3")

        with self.assertRaisesRegex(ConfigurationError, "jobs.database_path"):
            load_runtime_config(self.write("[jobs]\nenabled=true\n"))

    def test_jobs_continuation_defaults_and_validation(self):
        self.assertFalse(HISTORICAL_DEFAULTS.jobs_auto_continue)
        self.assertEqual(HISTORICAL_DEFAULTS.jobs_heartbeat_seconds, 30.0)
        self.assertEqual(HISTORICAL_DEFAULTS.jobs_max_auto_steps, 3)
        effective = self.effective(
            "[jobs]\nenabled=true\ndatabase_path='jobs.db'\n"
            "auto_continue=true\nheartbeat_seconds=2.5\nmax_auto_steps=4\n"
        )
        self.assertTrue(effective.jobs_auto_continue)
        self.assertEqual(effective.jobs_heartbeat_seconds, 2.5)
        self.assertEqual(effective.jobs_max_auto_steps, 4)
        for value in ("0", "-1", "false"):
            with self.subTest(heartbeat=value), self.assertRaisesRegex(
                ConfigurationError, "heartbeat_seconds"
            ):
                load_runtime_config(self.write(
                    f"[jobs]\nheartbeat_seconds={value}\n"
                ))
        for value in ("0", "-1", "true", "1.5"):
            with self.subTest(steps=value), self.assertRaisesRegex(
                ConfigurationError, "max_auto_steps"
            ):
                load_runtime_config(self.write(f"[jobs]\nmax_auto_steps={value}\n"))

    def test_job_store_builder_is_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "jobs.sqlite3"
            store = build_job_store(SimpleNamespace(
                jobs_enabled=True, jobs_database_path=path
            ))
            self.assertIsInstance(store, SQLiteJobStore)
            store.close()
            self.assertTrue(path.is_file())
        self.assertIsNone(build_job_store(SimpleNamespace(jobs_enabled=False)))

    def test_timezone_valid_invalid_and_historical_default(self):
        self.assertEqual(HISTORICAL_DEFAULTS.timezone, "UTC")
        self.assertEqual(self.effective(
            "[runtime]\ntimezone='America/Toronto'\n"
        ).timezone, "America/Toronto")
        with self.assertRaisesRegex(ConfigurationError, "unknown IANA timezone"):
            load_runtime_config(self.write(
                "[runtime]\ntimezone='Moon/SeaOfTranquility'\n"
            ))

    def test_cli_scalars_and_modes_override_file(self):
        effective = self.effective(
            "[runtime]\ncamera='none'\ncognition='none'\nmode='run'\n",
            ("--camera", "picamera2", "--cognition", "openai-responses", "--console"),
        )
        self.assertEqual((effective.camera, effective.cognition, effective.mode),
                         ("picamera2", "openai-responses", "console"))
        effective = self.effective("[runtime]\nmode='console'\n", ("--diagnostics",))
        self.assertEqual(effective.mode, "diagnostics")

    def test_explicit_mode_overrides_file_and_aliases_remain_compatible(self):
        cases = (
            ("console", ("--mode", "run"), "run"),
            ("run", ("--mode", "console"), "console"),
            ("run", ("--mode", "diagnostics"), "diagnostics"),
            ("run", ("--console",), "console"),
            ("console", ("--diagnostics",), "diagnostics"),
        )
        for configured, argv, expected in cases:
            with self.subTest(configured=configured, argv=argv):
                effective = self.effective(
                    f"[runtime]\nmode='{configured}'\n", argv,
                )
                self.assertEqual(effective.mode, expected)

    def test_conflicting_mode_selectors_fail_explicitly(self):
        for argv in (
            ("--mode", "run", "--console"),
            ("--mode", "console", "--diagnostics"),
        ):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit):
                parse_launch_arguments(argv)

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
            ("[memory]\nenabled='yes'\n", "memory.enabled must be boolean"),
            ("[memory]\ndatabase_path=4\n", "memory.database_path must be a string"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                ConfigurationError, message
            ):
                load_runtime_config(self.write(contents))

    def test_memory_configuration_is_disabled_by_default_and_resolves_paths(self):
        self.assertFalse(self.effective("").memory_enabled)
        path = self.write("[memory]\nenabled=true\ndatabase_path='db/memory.sqlite3'\n")
        effective = parse_launch_arguments(["--config", str(path)])[2]
        self.assertTrue(effective.memory_enabled)
        self.assertEqual(effective.memory_database_path, path.parent / "db/memory.sqlite3")
        absolute = self.effective(
            "[memory]\nenabled=true\ndatabase_path='/tmp/absolute-memory.sqlite3'\n"
        )
        self.assertEqual(absolute.memory_database_path, Path("/tmp/absolute-memory.sqlite3"))

    def test_enabled_memory_requires_nonempty_path_and_rejects_unknown_keys(self):
        for contents in ("[memory]\nenabled=true\n", "[memory]\nenabled=true\ndatabase_path='  '\n"):
            with self.assertRaisesRegex(ConfigurationError, "non-empty string"):
                load_runtime_config(self.write(contents))
        with self.assertRaisesRegex(ConfigurationError, "memory.backend"):
            load_runtime_config(self.write("[memory]\nbackend='sqlite'\n"))

    def test_persistent_memory_composition_disabled_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "missing" / "memory.sqlite3"
            store = build_persistent_memory_store(SimpleNamespace(
                memory_enabled=False, memory_database_path=database,
            ))
            self.assertIsNone(store)
            self.assertFalse(database.parent.exists())
            self.assertFalse(database.exists())

    def test_persistent_memory_composition_enabled_creates_store_and_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "missing" / "memory.sqlite3"
            store = build_persistent_memory_store(SimpleNamespace(
                memory_enabled=True, memory_database_path=database,
            ))
            self.assertIsInstance(store, SQLiteMemoryStore)
            self.assertTrue(database.parent.is_dir())
            self.assertTrue(database.is_file())
            entity = store.create_entity("object", "test")
            self.assertEqual(entity.identity, "ENT1")
            store.close()

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

    def test_elevenlabs_tts_defaults_and_cli_precedence(self):
        configured = self.effective(
            "[voice]\ntts='elevenlabs'\nelevenlabs_tts_model='file-model'\n"
            "elevenlabs_tts_voice_id='file-voice'\n",
            ("--elevenlabs-tts-model", "cli-model", "--elevenlabs-tts-speed", "1.2",
             "--elevenlabs-tts-voice-id", "cli-voice"),
        )
        self.assertEqual(configured.voice_tts, "elevenlabs")
        self.assertEqual(configured.voice_elevenlabs_tts_model, "cli-model")
        self.assertEqual(configured.voice_elevenlabs_tts_voice_id, "cli-voice")
        self.assertEqual(configured.voice_elevenlabs_tts_speed, 1.2)
        self.assertEqual(
            HISTORICAL_DEFAULTS.voice_elevenlabs_tts_model, "eleven_flash_v2_5"
        )
        self.assertIsNone(HISTORICAL_DEFAULTS.voice_elevenlabs_tts_voice_id)
        self.assertEqual(HISTORICAL_DEFAULTS.voice_elevenlabs_tts_speed, 1.0)

    def test_elevenlabs_tts_speed_is_strict_and_cli_overrides_toml(self):
        configured = self.effective(
            "[voice]\nelevenlabs_tts_speed=1.1\n",
            ("--elevenlabs-tts-speed", "0.8"),
        )
        self.assertEqual(configured.voice_elevenlabs_tts_speed, 0.8)
        for value in ("0.7", "1", "1.2"):
            with self.subTest(value=value):
                self.assertEqual(
                    self.effective(f"[voice]\nelevenlabs_tts_speed={value}\n")
                    .voice_elevenlabs_tts_speed,
                    float(value),
                )
        for value in ("true", "'1.1'", "0.6", "1.3"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ConfigurationError, r"voice.elevenlabs_tts_speed must be"
            ):
                load_runtime_config(
                    self.write(f"[voice]\nelevenlabs_tts_speed={value}\n")
                )

    def test_elevenlabs_tts_values_must_be_strings(self):
        for key in ("elevenlabs_tts_model", "elevenlabs_tts_voice_id"):
            with self.subTest(key=key), self.assertRaisesRegex(
                ConfigurationError, f"voice.{key} must be a string"
            ):
                load_runtime_config(self.write(f"[voice]\n{key}=7\n"))

    def test_elevenlabs_requires_voice_id_after_merge(self):
        path = self.write("[voice]\ntts='elevenlabs'\n")
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            main(["--config", str(path)])
        effective = self.effective(
            "[voice]\ntts='elevenlabs'\nelevenlabs_tts_voice_id='file-voice'\n",
            ("--elevenlabs-tts-voice-id", "cli-voice"),
        )
        self.assertEqual(effective.voice_elevenlabs_tts_voice_id, "cli-voice")

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
