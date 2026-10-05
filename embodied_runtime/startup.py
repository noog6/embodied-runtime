"""Deterministic deployment support for the Startup Wizard."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import getpass
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from embodied_runtime.cli import parse_launch_arguments, validate_launch_dependencies
from embodied_runtime.config import ConfigurationError, LaunchConfiguration
from embodied_runtime.profile import ProfileLoadError, RobotProfile, load_profile


class StartupError(RuntimeError):
    """An operator-facing deployment error."""


class CommandRunner(Protocol):
    def __call__(self, command: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]: ...


def run_command(
    command: Sequence[str], *, check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one argument-vector command without involving a shell."""
    try:
        return subprocess.run(
            list(command), check=check, text=True, capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "command failed").strip()
        raise StartupError(f"{command[0]} failed: {detail}") from None
    except OSError as error:
        raise StartupError(f"cannot execute {command[0]}: {error.strerror}") from None


@dataclass(frozen=True)
class SystemPaths:
    unit_directory: Path = Path("/etc/systemd/system")
    environment_directory: Path = Path("/etc/embodied-runtime")


@dataclass(frozen=True)
class Deployment:
    repo: Path
    python: Path
    config: Path
    profile: RobotProfile
    launch: LaunchConfiguration
    service_name: str
    user: str
    with_sms: bool
    capture_env: bool
    paths: SystemPaths = SystemPaths()
    with_ngrok: bool = False
    ngrok: Path | None = None
    ngrok_domain: str | None = None
    ngrok_version: str | None = None

    @property
    def unit_name(self) -> str:
        return f"{self.service_name}.service"

    @property
    def unit_path(self) -> Path:
        return self.paths.unit_directory / self.unit_name

    @property
    def environment_path(self) -> Path:
        return self.paths.environment_directory / f"{self.service_name}.env"

    @property
    def ngrok_unit_name(self) -> str:
        return f"{self.service_name}-ngrok.service"

    @property
    def ngrok_unit_path(self) -> Path:
        return self.paths.unit_directory / self.ngrok_unit_name

    @property
    def ngrok_environment_path(self) -> Path:
        return self.paths.environment_directory / f"{self.service_name}-ngrok.env"

    @property
    def ngrok_config_path(self) -> Path:
        return self.paths.environment_directory / f"{self.service_name}-ngrok.yml"

    @property
    def ngrok_state_path(self) -> Path:
        return self.paths.environment_directory / f"{self.service_name}-ngrok.state"

    @property
    def public_webhook_url(self) -> str | None:
        if not self.with_ngrok or self.ngrok_domain is None:
            return None
        return f"https://{self.ngrok_domain}{self.launch.sms_webhook_path}"

    @property
    def uses_environment_file(self) -> bool:
        if not required_environment(self):
            return False
        if self.capture_env:
            return True
        return validate_managed_environment_file(self)


@dataclass(frozen=True)
class InstallResult:
    unit_status: str
    restart_required: bool = False
    ngrok_unit_status: str | None = None
    ngrok_restart_required: bool = False


_SERVICE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_ENVIRONMENT_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
TWILIO_ENVIRONMENT = (
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER",
    "MIRA_SMS_OPERATOR_NUMBER", "TWILIO_WEBHOOK_URL",
)
_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)


def discover_repository(script_path: Path) -> Path:
    """Find the checkout from the installed script location, never from cwd."""
    current = script_path.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "main.py").is_file() and (candidate / "embodied_runtime").is_dir():
            return candidate
    raise StartupError(f"repository root not found from wizard location: {script_path}")


def discover_python(repo: Path, override: Path | None = None) -> Path:
    """Resolve an explicit interpreter or the checkout's own virtual environment."""
    candidate = override.expanduser() if override else repo / ".venv/bin/python"
    # Keep the venv entry-point path rather than resolving its usual symlink to
    # the base interpreter; Python uses that entry point to locate pyvenv.cfg.
    candidate = Path(os.path.abspath(candidate))
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        source = "--python" if override else "project virtual environment"
        raise StartupError(f"{source} interpreter is missing or not executable: {candidate}")
    return candidate


def config_candidates(repo: Path) -> tuple[Path, ...]:
    return tuple(sorted(path.resolve() for path in (repo / "config").glob("*.toml")))


def select_config(repo: Path, override: Path | None, *, interactive: bool = False) -> Path:
    if override:
        candidate = override.expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = candidate.resolve()
        if not candidate.is_file():
            raise StartupError(f"configuration file not found: {candidate}")
        return candidate
    preferred = (repo / "config/mira-agentic.toml").resolve()
    candidates = config_candidates(repo)
    if interactive and preferred in candidates:
        return preferred
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise StartupError("no configuration found; pass --config PATH")
    if not interactive:
        raise StartupError("multiple configurations found; pass --config PATH")
    print("Configurations:")
    for number, candidate in enumerate(candidates, 1):
        print(f"  {number}. {candidate}")
    answer = input("Select configuration number: ").strip()
    try:
        return candidates[int(answer) - 1]
    except (ValueError, IndexError):
        raise StartupError("invalid configuration selection") from None


def resolve_service_user(
    override: str | None, *, environ: Mapping[str, str] = os.environ,
    euid: int | None = None, lookup: Callable[[str], object] = pwd.getpwnam,
) -> str:
    euid = os.geteuid() if euid is None else euid
    candidate = override
    if candidate is None and euid == 0:
        sudo_user = environ.get("SUDO_USER", "")
        if sudo_user and sudo_user != "root":
            candidate = sudo_user
        else:
            raise StartupError("running directly as root requires --user NON_ROOT_USER")
    if candidate is None:
        candidate = getpass.getuser()
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", candidate):
        raise StartupError(f"invalid service user: {candidate!r}")
    try:
        account = lookup(candidate)
    except KeyError:
        raise StartupError(f"service user does not exist: {candidate}") from None
    if getattr(account, "pw_uid", 0) == 0:
        raise StartupError("the service user must not be root")
    return candidate


def validate_service_name(name: str) -> str:
    if not _SERVICE_NAME.fullmatch(name):
        raise StartupError(
            "service name must contain only lowercase letters, digits, '_' or '-'"
        )
    return name


def validate_ngrok_domain(value: str) -> str:
    """Validate a stable hostname (not a URL) and return its canonical form."""
    domain = value.strip().lower().rstrip(".")
    if not _HOSTNAME.fullmatch(domain):
        raise StartupError(
            "ngrok domain must be a hostname without scheme, credentials, port, path, query, or fragment"
        )
    return domain


def discover_ngrok(
    override: Path | None = None, *, runner: CommandRunner = run_command,
) -> tuple[Path, str]:
    candidate = str(override.expanduser()) if override else shutil.which("ngrok")
    if not candidate:
        raise StartupError("ngrok agent is missing; install ngrok v3 first or pass --ngrok PATH")
    path = Path(os.path.abspath(candidate))
    if not path.is_file() or not os.access(path, os.X_OK):
        raise StartupError(f"ngrok agent is missing or not executable: {path}")
    result = runner([str(path), "version"])
    output = f"{result.stdout}\n{result.stderr}".strip()
    match = re.search(r"(?:ngrok(?: version)?\s+)?v?(\d+)(?:\.\d+){1,2}", output, re.I)
    if not match:
        raise StartupError("could not determine ngrok agent version")
    if int(match.group(1)) != 3:
        raise StartupError(f"unsupported ngrok major version {match.group(1)}; ngrok v3 is required")
    return path, output.splitlines()[0]


def build_deployment(
    *, repo: Path, python: Path | None = None, config: Path | None = None,
    service_name: str | None = None, user: str | None = None,
    with_sms: bool = False, capture_env: bool = False,
    interactive: bool = False, environ: Mapping[str, str] = os.environ,
    euid: int | None = None, account_lookup: Callable[[str], object] = pwd.getpwnam,
    paths: SystemPaths = SystemPaths(),
    with_ngrok: bool = False, ngrok: Path | None = None,
    ngrok_domain: str | None = None, runner: CommandRunner = run_command,
) -> Deployment:
    repo = repo.resolve()
    if not repo.is_dir() or not (repo / "main.py").is_file():
        raise StartupError(f"invalid repository root: {repo}")
    python_path = discover_python(repo, python)
    config_path = select_config(repo, config, interactive=interactive)
    argv = ["--config", str(config_path), "--mode", "run", "--no-color"]
    if with_sms:
        argv.append("--sms")
    try:
        _, args, launch = parse_launch_arguments(argv)
        validate_launch_dependencies(args)
        profile = load_profile(launch.profile, repo / "profiles")
    except (ConfigurationError, ProfileLoadError) as error:
        raise StartupError(str(error)) from None
    except SystemExit:
        raise StartupError("runtime launch configuration is invalid") from None
    selected_name = validate_service_name(service_name or profile.identifier)
    selected_user = resolve_service_user(
        user, environ=environ, euid=euid, lookup=account_lookup,
    )
    ngrok_path = None
    ngrok_version = None
    domain = None
    if with_ngrok:
        if not launch.sms_enabled:
            raise StartupError("--with-ngrok requires effective SMS transport; enable SMS or pass --with-sms")
        if not ngrok_domain:
            raise StartupError("--with-ngrok requires --ngrok-domain HOST")
        domain = validate_ngrok_domain(ngrok_domain)
        ngrok_path, ngrok_version = discover_ngrok(ngrok, runner=runner)
        local_sms_upstream(launch.sms_bind_host, launch.sms_bind_port)
    return Deployment(
        repo, python_path, config_path, profile, launch, selected_name,
        selected_user, with_sms, capture_env, paths, with_ngrok, ngrok_path,
        domain, ngrok_version,
    )


def required_environment(deployment: Deployment) -> tuple[str, ...]:
    launch = deployment.launch
    required: list[str] = []
    if (launch.cognition == "openai-responses" or launch.vision == "openai-responses"
            or (launch.voice_enabled and launch.voice_tts == "openai")):
        required.append("OPENAI_API_KEY")
    if launch.voice_enabled and launch.voice_tts == "elevenlabs":
        required.append("ELEVENLABS_API_KEY")
    if launch.sms_enabled:
        required.extend(TWILIO_ENVIRONMENT)
    return tuple(required)


def environment_presence(
    deployment: Deployment, environ: Mapping[str, str] = os.environ,
) -> dict[str, bool]:
    result = {name: bool(environ.get(name)) for name in required_environment(deployment)}
    if deployment.with_ngrok:
        result["TWILIO_WEBHOOK_URL"] = True  # deterministically derived
        result["TWILIO_PUBLIC_MEDIA_BASE_URL"] = True
        result["NGROK_AUTHTOKEN"] = bool(environ.get("NGROK_AUTHTOKEN"))
    elif deployment.launch.sms_enabled and environ.get("TWILIO_PUBLIC_MEDIA_BASE_URL"):
        result["TWILIO_PUBLIC_MEDIA_BASE_URL"] = True
    return result


def validate_managed_environment_file(deployment: Deployment) -> bool:
    """Return whether the exact managed path is a securely protected regular file."""
    return validate_secure_file(deployment.environment_path)


def validate_secure_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise StartupError("managed environment file cannot be inspected") from None
    if (stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o066):
        raise StartupError("managed environment file is not securely protected")
    return True


def serialize_environment(
    deployment: Deployment, environ: Mapping[str, str] = os.environ,
) -> bytes:
    """Serialize only deployment-catalogued secrets, never arbitrary environment."""
    lines = []
    for name in required_environment(deployment):
        if not _ENVIRONMENT_NAME.fullmatch(name):
            raise StartupError(f"unsafe environment variable name: {name!r}")
        if deployment.with_ngrok and name == "TWILIO_WEBHOOK_URL":
            value = deployment.public_webhook_url
        else:
            value = environ.get(name)
        if not value:
            raise StartupError(f"required environment variable is missing: {name}")
        if any(character in value for character in ("\x00", "\n", "\r")):
            raise StartupError(f"environment value cannot be safely stored: {name}")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{name}="{escaped}"\n')
    if deployment.launch.sms_enabled:
        media_base = environ.get("TWILIO_PUBLIC_MEDIA_BASE_URL")
        if deployment.with_ngrok:
            parsed = urlsplit(deployment.public_webhook_url)
            media_base = f"{parsed.scheme}://{parsed.netloc}"
        if media_base:
            if any(character in media_base for character in ("\x00", "\n", "\r")):
                raise StartupError(
                    "environment value cannot be safely stored: TWILIO_PUBLIC_MEDIA_BASE_URL"
                )
            escaped = media_base.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'TWILIO_PUBLIC_MEDIA_BASE_URL="{escaped}"\n')
    return "".join(lines).encode()


def serialize_ngrok_environment(
    deployment: Deployment, environ: Mapping[str, str] = os.environ,
) -> bytes:
    value = environ.get("NGROK_AUTHTOKEN")
    if not value:
        raise StartupError("required environment variable is missing: NGROK_AUTHTOKEN")
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise StartupError("environment value cannot be safely stored: NGROK_AUTHTOKEN")
    return f'NGROK_AUTHTOKEN="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"\n'.encode()


def local_sms_upstream(host: str, port: int) -> str:
    normalized = host.strip().lower()
    if normalized in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return f"http://127.0.0.1:{port}"
    if normalized in {"::1", "::", "[::1]", "[::]"}:
        return f"http://[::1]:{port}"
    raise StartupError(f"sms.bind_host cannot be safely mapped to a local ngrok upstream: {host}")


def render_ngrok_config(deployment: Deployment) -> str:
    if not deployment.with_ngrok or not deployment.ngrok_domain:
        raise StartupError("ngrok ingress is not configured")
    upstream = local_sms_upstream(deployment.launch.sms_bind_host, deployment.launch.sms_bind_port)
    return (
        "version: 3\n\nendpoints:\n"
        f"  - name: {deployment.service_name}-sms\n"
        f'    url: "https://{deployment.ngrok_domain}"\n'
        "    upstream:\n"
        f'      url: "{upstream}"\n'
    )


def render_ngrok_state(deployment: Deployment) -> str:
    """Render non-secret state used to detect changes to the public webhook."""
    if deployment.public_webhook_url is None:
        raise StartupError("ngrok ingress is not configured")
    return f"public_webhook_url={deployment.public_webhook_url}\n"


def public_webhook_changed(deployment: Deployment) -> bool:
    """Compare configured public webhook state without inspecting secret files."""
    if not deployment.with_ngrok:
        return False
    installed = (deployment.ngrok_state_path.read_text()
                 if deployment.ngrok_state_path.exists() else None)
    return installed != render_ngrok_state(deployment)


def ngrok_environment_capture(
    deployment: Deployment, environ: Mapping[str, str] = os.environ,
) -> bytes | None:
    """Select a supplied token or a secure existing managed credential source."""
    managed = validate_secure_file(deployment.ngrok_environment_path)
    supplied = bool(environ.get("NGROK_AUTHTOKEN"))
    if not managed or (deployment.capture_env and supplied):
        return serialize_ngrok_environment(deployment, environ)
    return None


def _runtime_environment_capture(
    deployment: Deployment, *, webhook_changed: bool,
    environ: Mapping[str, str] = os.environ,
) -> bytes | None:
    """Allow token-only capture to reuse an existing runtime secret file."""
    required = required_environment(deployment)
    if not required:
        return None
    managed = validate_managed_environment_file(deployment)
    supplied_runtime_value = any(
        environ.get(name) for name in required if name != "TWILIO_WEBHOOK_URL"
    )
    if deployment.capture_env:
        token_only_capture = (
            deployment.with_ngrok and bool(environ.get("NGROK_AUTHTOKEN"))
            and managed and not webhook_changed and not supplied_runtime_value
        )
        if not token_only_capture:
            return serialize_environment(deployment, environ)
    if not managed:
        raise StartupError(
            "this launch requires service secrets; use --capture-env or install the "
            f"protected file {deployment.environment_path} first"
        )
    return None


def validate_ngrok_config(
    deployment: Deployment, *, environ: Mapping[str, str] = os.environ,
    runner: CommandRunner = run_command,
) -> str | None:
    """Ask ngrok v3 to parse endpoint configuration without starting a tunnel.

    Authentication is supplied independently through the process environment;
    it is never rendered into the temporary YAML or added to argv.
    """
    if not environ.get("NGROK_AUTHTOKEN"):
        return "ngrok config validation skipped; NGROK_AUTHTOKEN is not loaded"
    if deployment.ngrok is None:
        raise StartupError("ngrok ingress is not configured")
    with tempfile.NamedTemporaryFile("w", suffix=".yml") as temporary:
        temporary.write(render_ngrok_config(deployment))
        temporary.flush()
        runner([str(deployment.ngrok), "config", "check", "--config", temporary.name])
    return None


def render_ngrok_unit(deployment: Deployment) -> str:
    if not deployment.with_ngrok or deployment.ngrok is None:
        raise StartupError("ngrok ingress is not configured")
    command = " ".join(_unit_quote(arg) for arg in (
        str(deployment.ngrok), "start", f"{deployment.service_name}-sms",
        "--config", str(deployment.ngrok_config_path),
    ))
    return f"""[Unit]
Description={_unit_plain(deployment.profile.name)} ngrok SMS Ingress
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User={deployment.user}
EnvironmentFile={_unit_path(str(deployment.ngrok_environment_path))}
ExecStart={command}
Restart=on-failure
RestartSec=10s
TimeoutStopSec=20s
SyslogIdentifier={deployment.service_name}-ngrok
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def _unit_quote(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise StartupError("systemd arguments cannot contain control characters")
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unit_plain(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise StartupError("systemd directive values cannot contain control characters")
    return value.replace("%", "%%")


def _unit_path(value: str) -> str:
    """Escape a path for a scalar systemd directive (not a command argument)."""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise StartupError("systemd paths cannot contain control characters")
    escaped = []
    for character in value:
        if character == "%":
            escaped.append("%%")
        elif character in " \\\"'":
            escaped.append(f"\\x{ord(character):02x}")
        else:
            escaped.append(character)
    return "".join(escaped)


def render_unit(deployment: Deployment) -> str:
    """Render a complete systemd unit with each command argument independently quoted."""
    arguments = [
        str(deployment.python), str(deployment.repo / "main.py"),
        "--config", str(deployment.config), "--mode", "run", "--no-color",
    ]
    if deployment.with_sms:
        arguments.append("--sms")
    command = " ".join(_unit_quote(argument) for argument in arguments)
    environment_file = (
        f"EnvironmentFile={_unit_path(str(deployment.environment_path))}\n"
        if deployment.uses_environment_file else ""
    )
    template = (deployment.repo / "deploy/systemd/embodied-runtime.service.in").read_text()
    values = {
        "@DESCRIPTION@": _unit_plain(f"{deployment.profile.name} Embodied Runtime"),
        "@USER@": deployment.user,
        "@WORKING_DIRECTORY@": _unit_path(str(deployment.repo)),
        "@ENVIRONMENT_FILE@": environment_file.rstrip("\n"),
        "@EXEC_START@": command,
        "@SYSLOG_IDENTIFIER@": deployment.service_name,
    }
    for token, value in values.items():
        template = template.replace(token, value)
    return template.rstrip() + "\n"


def verify_unit(unit: str, runner: CommandRunner = run_command) -> str | None:
    executable = shutil.which("systemd-analyze")
    if executable is None:
        return "systemd-analyze is unavailable"
    with tempfile.NamedTemporaryFile("w", suffix=".service") as temporary:
        temporary.write(unit)
        temporary.flush()
        runner([executable, "verify", temporary.name])
    return None


def check_deployment(
    deployment: Deployment, *, environ: Mapping[str, str] = os.environ,
    runner: CommandRunner = run_command,
) -> str | None:
    """Validate a deployment without performing any machine mutation."""
    webhook_changed = public_webhook_changed(deployment)
    if webhook_changed and not deployment.capture_env:
        raise StartupError(
            "first ngrok setup or a changed public webhook requires --capture-env"
        )
    _runtime_environment_capture(
        deployment, webhook_changed=webhook_changed, environ=environ,
    )
    warnings = [verify_unit(render_unit(deployment), runner)]
    if deployment.with_ngrok:
        ngrok_environment_capture(deployment, environ)
        render_ngrok_config(deployment)
        warnings.append(verify_unit(render_ngrok_unit(deployment), runner))
        warnings.append(validate_ngrok_config(deployment, environ=environ, runner=runner))
    reported = [warning for warning in warnings if warning]
    return "; ".join(reported) if reported else None


def _privileged(command: Sequence[str], *, euid: int | None = None) -> list[str]:
    return list(command) if (os.geteuid() if euid is None else euid) == 0 else ["sudo", *command]


def _install_file(
    content: bytes, destination: Path, mode: str, *, runner: CommandRunner,
    euid: int | None = None,
) -> None:
    with tempfile.NamedTemporaryFile(delete=False) as temporary:
        temporary_path = Path(temporary.name)
        os.chmod(temporary_path, 0o600)
        temporary.write(content)
    try:
        runner(_privileged(["install", "-m", mode, str(temporary_path), str(destination)], euid=euid))
    finally:
        temporary_path.unlink(missing_ok=True)


def install(
    deployment: Deployment, *, environ: Mapping[str, str] = os.environ,
    force: bool = False, start: bool = False, runner: CommandRunner = run_command,
    euid: int | None = None,
) -> InstallResult:
    expected_state = render_ngrok_state(deployment) if deployment.with_ngrok else None
    installed_state = (deployment.ngrok_state_path.read_text()
                       if deployment.with_ngrok and deployment.ngrok_state_path.exists()
                       else None)
    webhook_changed = public_webhook_changed(deployment)
    if (deployment.with_ngrok and not deployment.capture_env
            and webhook_changed):
        raise StartupError(
            "first ngrok setup or a changed public webhook requires --capture-env"
        )
    environment_content = _runtime_environment_capture(
        deployment, webhook_changed=webhook_changed, environ=environ,
    )
    ngrok_environment_content = (
        ngrok_environment_capture(deployment, environ)
        if deployment.with_ngrok else None
    )
    unit = render_unit(deployment)
    verify_unit(unit, runner)
    ngrok_unit = render_ngrok_unit(deployment) if deployment.with_ngrok else None
    ngrok_config = render_ngrok_config(deployment) if deployment.with_ngrok else None
    if ngrok_unit:
        verify_unit(ngrok_unit, runner)
    existing = deployment.unit_path.read_bytes() if deployment.unit_path.exists() else None
    encoded = unit.encode()
    unit_changed = existing != encoded
    environment_updated = environment_content is not None
    ngrok_existing = (deployment.ngrok_unit_path.read_bytes()
                      if deployment.with_ngrok and deployment.ngrok_unit_path.exists() else None)
    ngrok_config_existing = (deployment.ngrok_config_path.read_bytes()
                             if deployment.with_ngrok and deployment.ngrok_config_path.exists() else None)
    ngrok_encoded = ngrok_unit.encode() if ngrok_unit else None
    ngrok_config_encoded = ngrok_config.encode() if ngrok_config else None
    ngrok_state_encoded = expected_state.encode() if expected_state else None
    ngrok_changed = deployment.with_ngrok and (
        ngrok_existing != ngrok_encoded or ngrok_config_existing != ngrok_config_encoded
    )
    if existing is not None and unit_changed and not force:
        raise StartupError("installed unit differs; review it and pass --force to replace")
    if deployment.with_ngrok and ngrok_existing is not None and ngrok_changed and not force:
        raise StartupError("installed ngrok artifacts differ; review them and pass --force to replace")
    status = "already current" if not unit_changed else "installed"
    active_before = (
        runner(["systemctl", "is-active", deployment.unit_name], check=False).returncode == 0
    )
    ngrok_active_before = bool(deployment.with_ngrok and
        runner(["systemctl", "is-active", deployment.ngrok_unit_name], check=False).returncode == 0)
    if environment_content is not None:
        runner(_privileged(["install", "-d", "-m", "0755", str(deployment.paths.environment_directory)], euid=euid))
        _install_file(
            environment_content, deployment.environment_path,
            "0600", runner=runner, euid=euid,
        )
    if ngrok_environment_content is not None:
        runner(_privileged(["install", "-d", "-m", "0755", str(deployment.paths.environment_directory)], euid=euid))
        _install_file(ngrok_environment_content, deployment.ngrok_environment_path,
                      "0600", runner=runner, euid=euid)
    if unit_changed:
        runner(_privileged(["install", "-d", "-m", "0755", str(deployment.paths.unit_directory)], euid=euid))
        _install_file(encoded, deployment.unit_path, "0644", runner=runner, euid=euid)
    if deployment.with_ngrok and ngrok_changed:
        runner(_privileged(["install", "-d", "-m", "0755", str(deployment.paths.unit_directory)], euid=euid))
        runner(_privileged(["install", "-d", "-m", "0755", str(deployment.paths.environment_directory)], euid=euid))
        if ngrok_existing != ngrok_encoded:
            _install_file(ngrok_encoded, deployment.ngrok_unit_path, "0644", runner=runner, euid=euid)
        if ngrok_config_existing != ngrok_config_encoded:
            _install_file(ngrok_config_encoded, deployment.ngrok_config_path, "0644", runner=runner, euid=euid)
    if deployment.with_ngrok and installed_state != expected_state:
        runner(_privileged(["install", "-d", "-m", "0755", str(deployment.paths.environment_directory)], euid=euid))
        _install_file(ngrok_state_encoded, deployment.ngrok_state_path,
                      "0644", runner=runner, euid=euid)
    runner(_privileged(["systemctl", "daemon-reload"], euid=euid))
    runner(_privileged(["systemctl", "enable", deployment.unit_name], euid=euid))
    if deployment.with_ngrok:
        runner(_privileged(["systemctl", "enable", deployment.ngrok_unit_name], euid=euid))
    if start and not active_before:
        runner(_privileged(["systemctl", "start", deployment.unit_name], euid=euid))
    if start and deployment.with_ngrok and not ngrok_active_before:
        runner(_privileged(["systemctl", "start", deployment.ngrok_unit_name], euid=euid))
    return InstallResult(
        status,
        restart_required=active_before and (unit_changed or environment_updated),
        ngrok_unit_status=("already current" if not ngrok_changed else "installed")
        if deployment.with_ngrok else None,
        ngrok_restart_required=ngrok_active_before and bool(
            ngrok_changed or ngrok_environment_content is not None),
    )


def uninstall(
    deployment: Deployment, *, purge_env: bool = False,
    runner: CommandRunner = run_command, euid: int | None = None,
) -> None:
    if deployment.with_ngrok:
        runner(_privileged(["systemctl", "stop", deployment.ngrok_unit_name], euid=euid), check=False)
        runner(_privileged(["systemctl", "disable", deployment.ngrok_unit_name], euid=euid), check=False)
        runner(_privileged(["rm", "-f", str(deployment.ngrok_unit_path),
                            str(deployment.ngrok_config_path),
                            str(deployment.ngrok_state_path)], euid=euid))
        if purge_env:
            runner(_privileged(["rm", "-f", str(deployment.ngrok_environment_path)], euid=euid))
    runner(_privileged(["systemctl", "stop", deployment.unit_name], euid=euid), check=False)
    runner(_privileged(["systemctl", "disable", deployment.unit_name], euid=euid), check=False)
    runner(_privileged(["rm", "-f", str(deployment.unit_path)], euid=euid))
    if purge_env:
        runner(_privileged(["rm", "-f", str(deployment.environment_path)], euid=euid))
    runner(_privileged(["systemctl", "daemon-reload"], euid=euid))


def service_status(
    deployment: Deployment, runner: CommandRunner = run_command,
) -> dict[str, str]:
    def state(command: list[str]) -> str:
        result = runner(command, check=False)
        return result.stdout.strip() or "unknown"
    return {
        "service": deployment.unit_name,
        "unit": str(deployment.unit_path),
        "installed": "yes" if deployment.unit_path.is_file() else "no",
        "enabled": state(["systemctl", "is-enabled", deployment.unit_name]),
        "active": state(["systemctl", "is-active", deployment.unit_name]),
        "repo": str(deployment.repo),
        "config": str(deployment.config),
    }


def ingress_status(deployment: Deployment, runner: CommandRunner = run_command) -> dict[str, str]:
    if not deployment.with_ngrok:
        return {}
    def state(command: list[str]) -> str:
        result = runner(command, check=False)
        return result.stdout.strip() or "unknown"
    return {
        "service": deployment.ngrok_unit_name,
        "unit": str(deployment.ngrok_unit_path),
        "installed": "yes" if deployment.ngrok_unit_path.is_file() else "no",
        "enabled": state(["systemctl", "is-enabled", deployment.ngrok_unit_name]),
        "active": state(["systemctl", "is-active", deployment.ngrok_unit_name]),
    }


def start_inactive_services(
    deployment: Deployment, *, runner: CommandRunner = run_command,
    euid: int | None = None,
) -> None:
    """Start sibling services in convenience order without coupling them."""
    runtime_active = runner(
        ["systemctl", "is-active", deployment.unit_name], check=False,
    ).returncode == 0
    if not runtime_active:
        runner(_privileged(["systemctl", "start", deployment.unit_name], euid=euid))
    if deployment.with_ngrok:
        ingress_active = runner(
            ["systemctl", "is-active", deployment.ngrok_unit_name], check=False,
        ).returncode == 0
        if not ingress_active:
            runner(_privileged(["systemctl", "start", deployment.ngrok_unit_name], euid=euid))


def build_wizard_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Embodied Runtime Startup Wizard")
    subparsers = parser.add_subparsers(dest="command")
    for command in ("check", "render", "install", "status", "uninstall"):
        child = subparsers.add_parser(command)
        child.add_argument("--python", type=Path)
        child.add_argument("--config", type=Path)
        child.add_argument("--service-name")
        child.add_argument("--user")
        child.add_argument("--with-sms", action="store_true")
        child.add_argument("--capture-env", action="store_true")
        child.add_argument("--with-ngrok", action="store_true")
        child.add_argument("--ngrok", type=Path)
        child.add_argument("--ngrok-domain")
        if command == "render":
            child.add_argument("--component", choices=("runtime", "ngrok", "all"), default="runtime")
        if command == "install":
            child.add_argument("--force", action="store_true")
            child.add_argument("--start", action="store_true")
        if command == "uninstall":
            child.add_argument("--yes", action="store_true")
            child.add_argument("--purge-env", action="store_true")
    return parser


def _print_summary(deployment: Deployment, environ: Mapping[str, str]) -> None:
    print(f"Repository\n  {deployment.repo}\n\nPython\n  {deployment.python}")
    print(f"\nConfiguration\n  {deployment.config}")
    print(f"\nProfile\n  {deployment.profile.identifier} — {deployment.profile.name}")
    print(f"\nService\n  {deployment.unit_name}\n\nService user\n  {deployment.user}")
    print("\nRuntime mode\n  run")
    print(f"\nSMS\n  {'enabled for service' if deployment.launch.sms_enabled else 'disabled'}")
    if deployment.with_ngrok:
        upstream = local_sms_upstream(deployment.launch.sms_bind_host, deployment.launch.sms_bind_port)
        print(f"\nngrok ingress\n  enabled\n\nngrok executable\n  {deployment.ngrok}")
        print(f"\nngrok version\n  {deployment.ngrok_version}")
        print(f"\nngrok domain\n  {deployment.ngrok_domain}")
        print(f"\nLocal SMS endpoint\n  {upstream}{deployment.launch.sms_webhook_path}")
        print(f"\nPublic SMS webhook\n  {deployment.public_webhook_url}")
    print("\nRequired environment")
    for name, present in environment_presence(deployment, environ).items():
        state = ("derived" if deployment.with_ngrok and name == "TWILIO_WEBHOOK_URL"
                 else "set" if present else "missing")
        print(f"  {name:<28} {state}")
    print(f"\nUnit\n  {deployment.unit_path}")


def wizard_main(
    argv: Sequence[str] | None = None, *, script_path: Path | None = None,
    environ: Mapping[str, str] = os.environ,
) -> int:
    parser = build_wizard_parser()
    args = parser.parse_args(argv)
    interactive = args.command is None
    if interactive:
        print("Embodied Runtime Startup Wizard\n")
        args = argparse.Namespace(
            command=None, python=None, config=None, service_name=None, user=None,
            with_sms=False, capture_env=False, with_ngrok=False, ngrok=None,
            ngrok_domain=None,
        )
    try:
        repo = discover_repository(script_path or Path(__file__))
        try:
            deployment = build_deployment(
                repo=repo, python=args.python, config=args.config,
                service_name=args.service_name, user=args.user,
                with_sms=args.with_sms, capture_env=args.capture_env,
                with_ngrok=args.with_ngrok, ngrok=args.ngrok,
                ngrok_domain=args.ngrok_domain,
                interactive=interactive, environ=environ,
            )
        except StartupError as error:
            if not interactive or "operator delivery route" not in str(error):
                raise
            print("Headless initiative messaging requires a delivery route.")
            if not input("Enable SMS for this service? [y/N] ").strip().lower().startswith("y"):
                raise
            deployment = build_deployment(
                repo=repo, python=args.python, config=args.config,
                service_name=args.service_name, user=args.user, with_sms=True,
                capture_env=args.capture_env, interactive=True, environ=environ,
                with_ngrok=args.with_ngrok, ngrok=args.ngrok,
                ngrok_domain=args.ngrok_domain,
            )
        if interactive:
            if (deployment.launch.sms_enabled and input(
                    "Configure stable ngrok SMS ingress? [y/N] "
            ).strip().lower().startswith("y")):
                discovered = shutil.which("ngrok")
                prompt = (f"ngrok executable [{discovered}]: " if discovered
                          else "ngrok executable path: ")
                entered_path = input(prompt).strip()
                ngrok_path = Path(entered_path) if entered_path else (
                    Path(discovered) if discovered else None)
                if ngrok_path is None:
                    raise StartupError("ngrok executable path is required")
                domain = input("Stable ngrok hostname: ").strip()
                deployment = build_deployment(
                    repo=repo, python=args.python, config=deployment.config,
                    service_name=args.service_name, user=args.user,
                    with_sms=deployment.with_sms, capture_env=False,
                    with_ngrok=True, ngrok=ngrok_path, ngrok_domain=domain,
                    interactive=True, environ=environ,
                )
            _print_summary(deployment, environ)
            if not input("\nInstall service? [y/N] ").strip().lower().startswith("y"):
                print("No changes made.")
                return 0
            if deployment.with_ngrok:
                capture_prompt = (
                    "Install separately scoped environment values into\n"
                    f"  {deployment.environment_path}\n"
                    f"  {deployment.ngrok_environment_path}\n"
                    "? [y/N] "
                )
            else:
                capture_prompt = (
                    "Install required currently-loaded environment values into "
                    f"{deployment.environment_path}? [y/N] "
                )
            capture = bool(required_environment(deployment)) and input(
                capture_prompt
            ).strip().lower().startswith("y")
            deployment = Deployment(**{**deployment.__dict__, "capture_env": capture})
            force = deployment.unit_path.exists() and input(
                "An existing unit may be replaced. Continue? [y/N] "
            ).strip().lower().startswith("y")
            result = install(deployment, environ=environ, force=force)
            print(f"Service {result.unit_status} and enabled.")
            restart_commands = []
            if result.restart_required:
                restart_commands.append(f"sudo systemctl restart {deployment.unit_name}")
            if result.ngrok_restart_required:
                restart_commands.append(f"sudo systemctl restart {deployment.ngrok_unit_name}")
            if restart_commands:
                print("Restart required:")
                for command in restart_commands:
                    print(f"  {command}")
                return 0
            if input(f"Start {deployment.profile.name} services now? [y/N] ").strip().lower().startswith("y"):
                start_inactive_services(deployment)
            else:
                print(f"Start later with: sudo systemctl start {deployment.unit_name}")
                if deployment.with_ngrok:
                    print(f"Start later with: sudo systemctl start {deployment.ngrok_unit_name}")
            return 0
        if args.command == "render":
            component = args.component
            if component in ("runtime", "all"):
                if component == "all":
                    print(f"--- {deployment.unit_path} ---")
                print(render_unit(deployment), end="")
            if component in ("ngrok", "all"):
                if not deployment.with_ngrok:
                    raise StartupError("ngrok rendering requires --with-ngrok")
                print(f"--- {deployment.ngrok_unit_path} ---")
                print(render_ngrok_unit(deployment), end="")
                print(f"--- {deployment.ngrok_config_path} ---")
                print(render_ngrok_config(deployment), end="")
        elif args.command == "check":
            _print_summary(deployment, environ)
            print("\nHost capabilities")
            systemctl = shutil.which("systemctl")
            print(f"  systemctl                    {'available' if systemctl else 'missing (warning)'}")
            systemd_host = Path("/run/systemd/system").is_dir()
            print(f"  systemd host                 {'available' if systemd_host else 'not running (warning)'}")
            print(f"  config readable              {'yes' if os.access(deployment.config, os.R_OK) else 'no'}")
            print(f"  working directory accessible {'yes' if os.access(deployment.repo, os.R_OK | os.X_OK) else 'no'}")
            warning = check_deployment(deployment, environ=environ)
            if (required_environment(deployment) and not deployment.capture_env
                    and deployment.uses_environment_file):
                print(f"  service secrets              managed file {deployment.environment_path}")
            print(f"\n{'WARNING: ' + warning if warning else 'Checks passed.'}")
        elif args.command == "install":
            result = install(
                deployment, environ=environ, force=args.force, start=args.start,
            )
            print(f"Service {result.unit_status} and enabled.")
            if result.restart_required:
                print(
                    "The service is active; restart explicitly to apply updated "
                    "service configuration/environment."
                )
                print(f"sudo systemctl restart {deployment.unit_name}")
            if result.ngrok_restart_required:
                print("The ngrok service is active; restart it explicitly to apply updates.")
                print(f"sudo systemctl restart {deployment.ngrok_unit_name}")
        elif args.command == "status":
            print("runtime")
            for name, value in service_status(deployment).items():
                print(f"  {name}: {value}")
            if deployment.with_ngrok:
                print("\ningress")
                for name, value in ingress_status(deployment).items():
                    print(f"  {name}: {value}")
                print(f"\nconfigured public webhook\n  {deployment.public_webhook_url}")
            print(f"sudo systemctl start {deployment.unit_name}")
            print(f"sudo systemctl restart {deployment.unit_name}")
            print(f"sudo systemctl stop {deployment.unit_name}")
            print(f"journalctl -u {deployment.unit_name} -f")
            if deployment.with_ngrok:
                print(f"sudo systemctl restart {deployment.ngrok_unit_name}")
                print(f"journalctl -u {deployment.ngrok_unit_name} -f")
        elif args.command == "uninstall":
            if not args.yes:
                raise StartupError("uninstall requires --yes")
            uninstall(deployment, purge_env=args.purge_env)
            print("Service uninstalled; environment file preserved." if not args.purge_env else "Service and environment file removed.")
        return 0
    except StartupError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
