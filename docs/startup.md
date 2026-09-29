# Startup Wizard and systemd

The runtime remains an ordinary foreground Python process. `--mode run` waits
for work, handles systemd's normal `SIGTERM`, shuts down gracefully, and exits
zero. Console and diagnostics modes remain operator-facing alternatives. The
**Startup Wizard** only deploys and manages that process; the runtime does not
know that systemd is its parent.

## Discover and review an installation

Run the wizard from any working directory:

```sh
python /path/to/embodied-runtime/scripts/startup_wizard.py
```

The script finds the repository by walking upward from its own resolved path.
It prefers the executable `<repo>/.venv/bin/python` and never silently selects
a system Python. Override it with `--python /absolute/path/python`. Config paths
may be supplied with `--config`; interactive setup prefers
`config/mira-agentic.toml`, while automation refuses an ambiguous set of config
files. All service paths are absolute, and paths containing spaces are quoted
as individual systemd command arguments.

The resolved runtime profile supplies the default service name and description
(`mira.service` and `Mira Embodied Runtime`). `--service-name` accepts a
conservative lowercase identifier. `--user` selects an existing non-root Unix
account. Otherwise the current non-root user is used; under sudo, `SUDO_USER`
is preferred. Direct root use requires an explicit non-root user.

## Automation commands

Each command accepts the common `--config`, `--python`, `--service-name`,
`--user`, `--with-sms`, and `--capture-env` options where relevant:

```sh
python scripts/startup_wizard.py check --user pi --with-sms
python scripts/startup_wizard.py render --user pi --with-sms --capture-env
python scripts/startup_wizard.py install --user pi --with-sms --capture-env
python scripts/startup_wizard.py status --user pi
python scripts/startup_wizard.py uninstall --user pi --yes
```

`check` validates the repository, interpreter, config/profile, account, exact
headless launch dependencies, required environment-variable presence, and the
rendered unit. It uses `systemd-analyze verify` when available, without writing
under `/etc` or changing a service. `render` prints only the unit, never secret
values. `status` performs read-only systemctl queries and prints useful
follow-up commands.

`install` writes the local administrator unit to
`/etc/systemd/system/<name>.service`, reloads systemd, and enables it at boot.
It does **not** start or restart the robot unless `--start` is explicitly
provided. An identical unit is current and idempotent. Replacing a different
unit requires `--force`; an already active changed service is not silently
restarted. Non-root operation invokes `sudo` only for installation and systemctl
mutations, using argument arrays rather than a shell.

`uninstall --yes` stops and disables the service, removes its unit, and reloads
systemd. It does not remove the checkout, configuration, databases, workspaces,
or run history. It also preserves the managed environment file unless the
separate `--purge-env` option is given.

## Service policy

The generated service directly executes:

```text
<venv-python> <repo>/main.py --config <absolute-config> --mode run --no-color
```

`--with-sms` adds `--sms` as a launch override without editing TOML. A config
that already enables SMS is treated as effectively enabled, though the command
line contains `--sms` only for the explicit override. Headless initiative
messaging is checked with the same launch dependency validator used by the
normal CLI.

The unit uses `Type=simple`, waits for network-online and sound targets, writes
stdout/stderr to journald, allows 30 seconds to stop, and uses
`Restart=on-failure` with a 10-second delay. Three starts within 300 seconds
trigger systemd's restart-loop protection. There is no `ExecStop`, alternate
kill signal, PID file, daemonization, session UID assumption, repository log,
or OOM policy. A clean stop therefore follows the Phase 1 SIGTERM path and
stays stopped.

Startup never runs `git pull`: restarting supervision must not deploy a
different revision. Updating the checkout remains an explicit operator action.

## Secrets and SMS

Provider requirements are calculated from the effective launch:

- OpenAI cognition, vision, or enabled OpenAI TTS requires `OPENAI_API_KEY`;
- enabled ElevenLabs TTS requires `ELEVENLABS_API_KEY`;
- effective SMS requires `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
  `TWILIO_PHONE_NUMBER`, `MIRA_SMS_OPERATOR_NUMBER`, and `TWILIO_WEBHOOK_URL`.

Diagnostics report only `set` or `missing`. `--capture-env` is the explicit
request to copy only those allowlisted, required values from the current
process into `/etc/embodied-runtime/<name>.env`. The wizard rejects newline,
NUL, and other unsafe representations, stages the data in a mode-0600 temporary
file, and installs the final file mode 0600. Secret data is never placed in the
unit, subprocess arguments, output, or comparisons. The unit references the
file with `EnvironmentFile=` only when the effective launch needs provider
secrets and either capture was requested or a secure existing managed file is
being reused. A stale file is preserved but omitted when the launch needs no
secrets. Reused files must be regular, non-symlink files with no group/world
read or write permissions. With a secure managed file, later checks and
installs do not require the operator to re-source its secret values.

The application-facing interface remains environment variables. systemd
credentials could provide later hardening, but Phase 2 does not add
`LoadCredential` or provider-specific file APIs.

SMS deployment starts Mira's HTTP/Twilio transport only. Inbound SMS still
needs an externally available webhook or tunnel. ngrok installation and
supervision remain Phase 3.

## Operation and logs

Installing is deliberately non-actuating. Start only after review:

```sh
sudo systemctl start mira.service
sudo systemctl restart mira.service
sudo systemctl stop mira.service
systemctl status mira.service
journalctl -u mira.service
journalctl -u mira.service -f
```

systemd owns supervisor output in journald. The runtime continues to own its
per-run records under `data/runs/Rxx/`; no additional daemon log is created.
