"""Robot profile representation and loading."""

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib


@dataclass(frozen=True)
class RobotProfile:
    identifier: str
    name: str
    description: str = ""


class ProfileLoadError(ValueError):
    """Raised when a robot profile cannot be loaded."""


DEFAULT_PROFILES_DIRECTORY = Path(__file__).resolve().parent.parent / "profiles"


def load_profile(
    identifier: str, profiles_directory: Path = DEFAULT_PROFILES_DIRECTORY
) -> RobotProfile:
    """Load and validate a named TOML robot profile."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", identifier):
        raise ProfileLoadError(f"Invalid robot profile identifier: {identifier!r}")

    path = profiles_directory / f"{identifier}.toml"
    try:
        with path.open("rb") as profile_file:
            data = tomllib.load(profile_file)
    except FileNotFoundError as error:
        raise ProfileLoadError(f"Robot profile {identifier!r} was not found at {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ProfileLoadError(f"Robot profile {identifier!r} is invalid TOML: {error}") from error

    try:
        profile_id = data["profile"]["id"]
        name = data["profile"]["name"]
    except (KeyError, TypeError) as error:
        raise ProfileLoadError(
            f"Robot profile {identifier!r} must define profile.id and profile.name"
        ) from error

    description = data["profile"].get("description", "")
    if not all(isinstance(value, str) and value for value in (profile_id, name)):
        raise ProfileLoadError(f"Robot profile {identifier!r} has invalid id or name")
    if profile_id != identifier:
        raise ProfileLoadError(
            f"Robot profile file {identifier!r} declares mismatched id {profile_id!r}"
        )
    if not isinstance(description, str):
        raise ProfileLoadError(f"Robot profile {identifier!r} has an invalid description")
    return RobotProfile(profile_id, name, description)
