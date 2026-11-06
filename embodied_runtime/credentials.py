"""Local, interactive provider credential setup. Never execute stored shell text."""

import getpass
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
import warnings

_PROVIDERS = {
    "OPENAI_API_KEY": ("OpenAI", "openai.env"),
    "OPENROUTER_API_KEY": ("OpenRouter", "openrouter.env"),
    "ELEVENLABS_API_KEY": ("ElevenLabs", "elevenlabs.env"),
}


def _source_line(variable, filename):
    return (f'if [ -z "${{{variable}:-}}" ] && '
            f'[ -r "$HOME/.config/embodied-runtime/{filename}" ]; then '
            f'. "$HOME/.config/embodied-runtime/{filename}"; fi')


SOURCE_LINE = _source_line("OPENAI_API_KEY", "openai.env")
SETUP_COMMAND = "python scripts/setup_environment.py --config YOUR_CONFIG --credentials-only"


class CredentialSetupError(ValueError):
    """A safe, operator-facing credential setup failure."""


def openai_key_required(launch):
    return (launch.cognition == "openai-responses"
            or launch.vision == "openai-responses"
            or (launch.cognition == "openai-compatible"
                and launch.cognition_openai_compatible.api_key_env == "OPENAI_API_KEY")
            or (launch.vision == "configured" and any(
                entry.api_key_env == "OPENAI_API_KEY"
                for entry in launch.vision_configuration.backends))
            or (launch.voice_enabled and launch.voice_tts == "openai"))


def required_credentials(launch):
    """Select supported wizard credentials from the effective launch."""
    required = []
    if openai_key_required(launch):
        required.append("OPENAI_API_KEY")
    if ((launch.cognition == "openai-compatible"
         and launch.cognition_openai_compatible.api_key_env == "OPENROUTER_API_KEY")
            or (launch.vision == "configured" and any(
                entry.api_key_env == "OPENROUTER_API_KEY"
                for entry in launch.vision_configuration.backends))):
        required.append("OPENROUTER_API_KEY")
    if launch.voice_enabled and launch.voice_tts == "elevenlabs":
        required.append("ELEVENLABS_API_KEY")
    return tuple(required)


def _validate_key(key):
    if not key or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in key):
        raise CredentialSetupError("Key must be nonempty and contain no whitespace or control characters.")
    return key


def _private_directory(directory, repository):
    if directory.resolve().is_relative_to(repository.resolve()):
        raise CredentialSetupError("Credential storage must be outside the Git checkout.")
    if directory.is_symlink():
        raise CredentialSetupError("Credential directory must not be a symbolic link.")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise CredentialSetupError("Credential directory must be owned by the current user.")
    directory.chmod(0o700)


def _read_saved_key(path, variable):
    # O_NOFOLLOW plus fstat prevents opening symlinks or permissive secret files.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise CredentialSetupError("Saved key must be a user-owned regular file with mode 600.")
        lines = [line.strip() for line in stream if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise CredentialSetupError(f"Saved key file must contain one export {variable} assignment.")
    try:
        tokens = shlex.split(lines[0])
    except ValueError:
        raise CredentialSetupError("Saved key file has invalid quoting.") from None
    if len(tokens) != 2 or tokens[0] != "export" or not tokens[1].startswith(variable + "="):
        raise CredentialSetupError(f"Saved key file must contain one export {variable} assignment.")
    return _validate_key(tokens[1].split("=", 1)[1])


def _save_key(path, key, variable):
    if path.is_symlink():
        raise CredentialSetupError("Credential file must not be a symbolic link.")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(f"export {variable}=" + shlex.quote(key) + "\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _enable_bashrc(home, repository, variable, provider, filename):
    path = home / ".bashrc"
    if path.resolve().is_relative_to(repository.resolve()):
        raise CredentialSetupError("Shell configuration must be outside the Git checkout.")
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "r+") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise CredentialSetupError(".bashrc must be a regular file owned by the current user.")
        if info.st_mode & 0o022:
            raise CredentialSetupError(".bashrc must not be writable by other users.")
        source_line = _source_line(variable, filename)
        if source_line not in stream.read().splitlines():
            stream.write(f"\n# Embodied Runtime: load the protected {provider} key when not already set.\n"
                         + source_line + "\n")


def setup_openai_credentials(launch, *, environ=None, interactive=None, home=None, repository=None, replace=False):
    """Keep the existing OpenAI-only entry point for the service startup wizard."""
    environment = dict(os.environ if environ is None else environ)
    if not openai_key_required(launch):
        return environment
    return _setup_key("OPENAI_API_KEY", environment, interactive=interactive,
                      home=home, repository=repository, replace=replace)


def setup_credentials(launch, *, environ=None, interactive=None, home=None, repository=None,
                      replace_openai_key=False, replace_elevenlabs_key=False,
                      replace_openrouter_key=False):
    """Handle each required provider independently in a setup-local environment."""
    environment = dict(os.environ if environ is None else environ)
    replacements = {"OPENAI_API_KEY": replace_openai_key,
                    "ELEVENLABS_API_KEY": replace_elevenlabs_key,
                    "OPENROUTER_API_KEY": replace_openrouter_key}
    for variable in required_credentials(launch):
        environment = _setup_key(variable, environment, interactive=interactive,
                                 home=home, repository=repository, replace=replacements[variable])
    return environment


def _setup_key(variable, environment, *, interactive, home, repository, replace):
    provider, filename = _PROVIDERS[variable]
    if environment.get(variable, "").strip() and not replace:
        print(f"{variable}: set (not validated with the provider)")
        return environment
    print(f"{variable}: replacement requested." if replace else
          f"{variable}: missing or empty. {provider} requests will be unavailable.")
    print("Set it up with: " + SETUP_COMMAND)
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stderr.isatty()
    if not interactive:
        return environment
    if home is None and os.geteuid() == 0:
        print("Run credential setup as the non-root user who will launch the runtime.")
        return environment
    home = Path.home() if home is None else Path(home)
    repository = Path(__file__).resolve().parent.parent if repository is None else Path(repository)
    path = home / ".config/embodied-runtime" / filename
    try:
        if not input(f"Set up {variable} using protected storage at {path}? [y/N] ").strip().lower().startswith("y"):
            return environment
        _private_directory(path.parent, repository)
        if path.exists() or path.is_symlink():
            if not replace and input("Load the existing protected key for this setup? [Y/n] ").strip().lower() != "n":
                key = _read_saved_key(path, variable)
            else:
                if not input(f"Replace the saved {provider} key? [y/N] ").strip().lower().startswith("y"):
                    return environment
                key = None
        else:
            key = None
        if key is None:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                entered = getpass.getpass(f"{provider} API key (hidden; blank cancels): ")
            if not entered:
                print("Credential entry cancelled.")
                return environment
            key = _validate_key(entered)
        # Normalize reused files too: only a literal, shell-quoted assignment
        # may be sourced later, never arbitrary existing shell expressions.
        _save_key(path, key, variable)
        environment[variable] = key
        print(f"{variable} is available to this setup process; no API request was made.")
        print("Your current terminal is unchanged. Before launching, run:\n"
              f"  source ~/.config/embodied-runtime/{filename}")
        if input("Load the protected key automatically in future Bash terminals via ~/.bashrc? [y/N] ").strip().lower().startswith("y"):
            _enable_bashrc(home, repository, variable, provider, filename)
            print("Bash startup source line is configured; the key itself is not in .bashrc.")
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("\nCredential entry cancelled or a hidden-input terminal was unavailable.")
    except CredentialSetupError as error:
        print(f"Credential setup: {error}")
    except OSError:
        print("Credential setup could not access the protected file or shell configuration. Check ownership and permissions.")
    return environment
