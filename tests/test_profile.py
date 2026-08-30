import tempfile
import unittest
from pathlib import Path

from embodied_runtime.profile import ProfileLoadError, load_profile


class ProfileTests(unittest.TestCase):
    def test_loads_mira_profile(self) -> None:
        profile = load_profile("mira")
        self.assertEqual(profile.identifier, "mira")
        self.assertEqual(profile.name, "Mira")

    def test_missing_profile_has_useful_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ProfileLoadError, "was not found"):
                load_profile("unknown", Path(directory))
