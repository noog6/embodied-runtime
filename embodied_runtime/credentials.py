"""Local, interactive OpenAI credential setup. Never execute stored shell text."""

import getpass
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
import warnings

SOURCE_LINE = ('if [ -z "${OPENAI_API_KEY:-}" ] && '
               '[ -r "$HOME/.config/embodied-runtime/openai.env" ]; then '
               '. "$HOME/.config/embodied-runtime/openai.env"; fi')
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


def _read_saved_key(path):
    # O_NOFOLLOW plus fstat prevents opening symlinks or permissive secret files.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise CredentialSetupError("Saved key must be a user-owned regular file with mode 600.")
        lines = [line.strip() for line in stream if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise CredentialSetupError("Saved key file must contain one export OPENAI_API_KEY assignment.")
    try:
        tokens = shlex.split(lines[0])
    except ValueError:
        raise CredentialSetupError("Saved key file has invalid quoting.") from None
    if len(tokens) != 2 or tokens[0] != "export" or not tokens[1].startswith("OPENAI_API_KEY="):
        raise CredentialSetupError("Saved key file must contain one export OPENAI_API_KEY assignment.")
    return _validate_key(tokens[1].split("=", 1)[1])


def _save_key(path, key):
    if path.is_symlink():
        raise CredentialSetupError("Credential file must not be a symbolic link.")
    fd, temporary = tempfile.mkstemp(prefix=".openai-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write("export OPENAI_API_KEY=" + shlex.quote(key) + "\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _enable_bashrc(home, repository):
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
        if SOURCE_LINE not in stream.read().splitlines():
            stream.write("\n# Embodied Runtime: load the protected OpenAI key when not already set.\n" + SOURCE_LINE + "\n")


def setup_openai_credentials(launch, *, environ=None, interactive=None, home=None, repository=None, replace=False):
    """Return a setup-local environment; never imply the parent shell was changed."""
    environment = dict(os.environ if environ is None else environ)
    if not openai_key_required(launch):
        return environment
    if environment.get("OPENAI_API_KEY", "").strip() and not replace:
        print("OPENAI_API_KEY: set (not validated with the provider)")
        return environment
    print("OPENAI_API_KEY: replacement requested." if replace else
          "OPENAI_API_KEY: missing or empty. OpenAI requests will be unavailable.")
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
    path = home / ".config/embodied-runtime/openai.env"
    try:
        if not input(f"Set up OPENAI_API_KEY using protected storage at {path}? [y/N] ").strip().lower().startswith("y"):
            return environment
        _private_directory(path.parent, repository)
        if path.exists() or path.is_symlink():
            if not replace and input("Load the existing protected key for this setup? [Y/n] ").strip().lower() != "n":
                key = _read_saved_key(path)
            else:
                if not input("Replace the saved OpenAI key? [y/N] ").strip().lower().startswith("y"):
                    return environment
                key = None
        else:
            key = None
        if key is None:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                entered = getpass.getpass("OpenAI API key (hidden; blank cancels): ")
            if not entered:
                print("Credential entry cancelled.")
                return environment
            key = _validate_key(entered)
        # Normalize reused files too: only a literal, shell-quoted assignment
        # may be sourced later, never arbitrary existing shell expressions.
        _save_key(path, key)
        environment["OPENAI_API_KEY"] = key
        print("OPENAI_API_KEY is available to this setup process; no API request was made.")
        print("Your current terminal is unchanged. Before launching, run:\n"
              "  source ~/.config/embodied-runtime/openai.env")
        if input("Load the protected key automatically in future Bash terminals via ~/.bashrc? [y/N] ").strip().lower().startswith("y"):
            _enable_bashrc(home, repository)
            print("Bash startup source line is configured; the key itself is not in .bashrc.")
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("\nCredential entry cancelled or a hidden-input terminal was unavailable.")
    except CredentialSetupError as error:
        print(f"Credential setup: {error}")
    except OSError:
        print("Credential setup could not access the protected file or shell configuration. Check ownership and permissions.")
    return environment
