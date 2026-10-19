"""Offline checks for deployment choices; no preparation or hardware adapters."""

from dataclasses import replace
from pathlib import Path
import re
import sys
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from embodied_runtime.cli import (
    build_cognition_backend, build_visual_perception_backend,
    parse_launch_arguments, validate_launch_dependencies,
)
from embodied_runtime.config import HISTORICAL_DEFAULTS
from embodied_runtime.jobs import workspace_root_for_database
from embodied_runtime.startup import (
    TWILIO_ENVIRONMENT, build_deployment, config_candidates, render_unit,
    required_environment, select_config,
)


ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    "mira-openai-astra.toml": ("openai-responses", "gpt-6-astra"),
    "mira-openai-luna.toml": ("openai-responses", "gpt-6-luna"),
    "mira-openai-sol.toml": ("openai-responses", "gpt-6.1-sol"),
    "mira-openrouter-claude.toml": ("openai-compatible", "~anthropic/claude-opus-latest"),
    "mira-openrouter-gemini.toml": ("openai-compatible", "google/gemini-4-argon"),
    "mira-openrouter-glm.toml": ("openai-compatible", "~z-ai/glm-flash-latest"),
    "mira-openrouter-grok.toml": ("openai-compatible", "~x-ai/grok-latest"),
}
EXPERIMENT_DATABASES = {
    "procedure-experiment.toml": "work/procedure-experiment.sqlite3",
    "reflection-experiment.toml": "data/jobs.sqlite3",
    "shared-reads-pilot.toml": "work/shared-reads-pilot.sqlite3",
}


class DeploymentProfileTests(unittest.TestCase):
    def test_profiles_resolve_and_construct_matching_lazy_backends(self):
        with patch.dict("os.environ", {
            "OPENAI_MODEL": "wrong-cognition-model",
            "OPENAI_VISION_MODEL": "wrong-vision-model",
        }, clear=True):
            for name, (backend, model) in PROFILES.items():
                with self.subTest(profile=name):
                    _, args, launch = parse_launch_arguments([
                        "--config", str(ROOT / "config" / name),
                    ])
                    validate_launch_dependencies(args)
                    self.assertEqual((launch.cognition, launch.cognition_model), (backend, model))
                    self.assertEqual(launch.vision, "configured")
                    self.assertEqual(len(launch.vision_configuration.backends), 1)
                    entry, = launch.vision_configuration.backends
                    self.assertEqual((entry.backend, entry.model), (backend, model))
                    self.assertEqual(entry.request_timeout_seconds, 60)
                    self.assertEqual(launch.vision_configuration.request_timeout_seconds, 60)

                    cognition = build_cognition_backend(args)
                    vision = build_visual_perception_backend(args)
                    self.assertEqual(len(vision.backends), 1)
                    visual, = vision.backends
                    self.assertEqual((cognition.model, visual.model), (model, model))
                    self.assertEqual(cognition.identifier, visual.identifier)
                    self.assertTrue(cognition.supports_image_input)
                    self.assertIsNone(cognition._client)
                    self.assertIsNone(visual._client)
                    if backend == "openai-compatible":
                        connection = launch.cognition_openai_compatible
                        self.assertEqual(connection.api_key_env, "OPENROUTER_API_KEY")
                        self.assertEqual(entry.api_key_env, connection.api_key_env)
                        self.assertEqual(connection.base_url, "https://openrouter.ai/api/v1")
                        self.assertEqual(entry.base_url, connection.base_url)
                        self.assertEqual(connection.request_timeout_seconds, 60)
                        self.assertTrue(connection.tool_calling)
                        self.assertTrue(connection.image_input)
                        self.assertEqual(connection.parallel_tool_calls_mode,
                                         "disable" if name == "mira-openrouter-glm.toml" else "omit")
                    else:
                        self.assertEqual(entry.api_key_env, "OPENAI_API_KEY")
                        self.assertIsNone(entry.base_url)

    def test_all_other_operating_settings_and_persistence_match_normal_mira(self):
        baseline = parse_launch_arguments([
            "--config", str(ROOT / "config/mira-agentic.toml"),
        ])[2]
        for name in PROFILES:
            with self.subTest(profile=name):
                alternative = parse_launch_arguments([
                    "--config", str(ROOT / "config" / name),
                ])[2]
                # Compare every operating field, including voice/persistence
                # and defaults, excluding only cognition/vision selection.
                self.assertEqual(replace(
                    alternative, cognition=baseline.cognition,
                    cognition_model=baseline.cognition_model,
                    cognition_openai_compatible=baseline.cognition_openai_compatible,
                    vision=baseline.vision,
                    vision_configuration=baseline.vision_configuration,
                ), baseline)
                self.assertEqual(
                    workspace_root_for_database(alternative.jobs_database_path),
                    ROOT / "data/jobs-workspaces",
                )

    def test_experiments_preserve_effective_storage_and_isolation(self):
        for name, original_location in EXPERIMENT_DATABASES.items():
            with self.subTest(experiment=name):
                path = ROOT / "config/experiments" / name
                _, args, launch = parse_launch_arguments(["--config", str(path)])
                validate_launch_dependencies(args)
                expected = ROOT / original_location
                self.assertEqual(launch.jobs_database_path, expected)
                self.assertEqual(
                    workspace_root_for_database(launch.jobs_database_path),
                    expected.with_name(expected.stem + "-workspaces"),
                )
                self.assertFalse(launch.memory_enabled)
                self.assertFalse(launch.conversation_history_enabled)
                self.assertEqual(launch.conversation_media, HISTORICAL_DEFAULTS.conversation_media)
                self.assertEqual((launch.hardware, launch.camera, launch.vision),
                                 ("virtual", "none", "none"))
                self.assertFalse(launch.jobs_auto_continue)

    def test_wizard_discovers_deployments_and_accepts_explicit_experiment_paths(self):
        candidates = config_candidates(ROOT)
        self.assertEqual({path.name for path in candidates},
                         {"mira-agentic.toml", *PROFILES})
        self.assertEqual(select_config(ROOT, None, interactive=True),
                         ROOT / "config/mira-agentic.toml")
        for name in EXPERIMENT_DATABASES:
            path = ROOT / "config/experiments" / name
            self.assertNotIn(path, candidates)
            self.assertEqual(select_config(ROOT, path), path)
        template = ROOT / "config/experiments/reflective-continuity-job.toml"
        self.assertNotIn(template, candidates)
        definition = tomllib.loads(template.read_text())
        self.assertEqual(set(definition), {"name", "description"})
        self.assertIn("{sources}", definition["description"])

    def test_wizard_builds_profiles_and_discovers_credentials_without_installing(self):
        for name, (backend, _) in PROFILES.items():
            with self.subTest(profile=name):
                path = ROOT / "config" / name
                deployment = build_deployment(
                    repo=ROOT, python=Path(sys.executable), config=path,
                    user="robot", euid=1000,
                    account_lookup=lambda _: SimpleNamespace(pw_uid=1000),
                    with_sms=True, capture_env=True,
                )
                key = "OPENAI_API_KEY" if backend == "openai-responses" else "OPENROUTER_API_KEY"
                self.assertEqual(required_environment(deployment),
                                 (key, "ELEVENLABS_API_KEY", *TWILIO_ENVIRONMENT))
                self.assertIn(f'"{path}"', render_unit(deployment))

    def test_active_experiment_path_references_resolve(self):
        names = (*EXPERIMENT_DATABASES, "reflective-continuity-job.toml")
        paths = [ROOT / "README.md"]
        for directory in ("docs", "scripts", "tests"):
            paths.extend((ROOT / directory).rglob("*.md" if directory == "docs" else "*.py"))
        checked = set()
        for path in paths:
            for reference in re.findall(r"config/(?:experiments/)?[\w-]+\.toml", path.read_text()):
                if Path(reference).name in names:
                    with self.subTest(source=path, reference=reference):
                        self.assertTrue((ROOT / reference).is_file())
                        checked.add(Path(reference).name)
        self.assertEqual(checked, set(names))


if __name__ == "__main__":
    unittest.main()
