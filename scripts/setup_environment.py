#!/usr/bin/env python3
"""Install the checkout and configured provider extras into a virtual environment."""

import argparse
from pathlib import Path
import subprocess
import sys
import venv

REPOSITORY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--venv", type=Path, default=REPOSITORY / ".venv")
    parser.add_argument("--with-sms", action="store_true")
    parser.add_argument("--system-site-packages", action="store_true",
                        help="Expose OS-installed Pi camera/HAT packages in a NEW venv")
    parser.add_argument("--check", action="store_true", help="Check an existing install without installing")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 13):
        parser.error("Python 3.13 or later is required; run this script with that interpreter")
    config = args.config.expanduser().resolve()
    if not config.is_file():
        parser.error(f"config file does not exist: {config}")
    environment = args.venv.expanduser().absolute()
    python = environment / "bin/python"
    try:
        if not (environment / "pyvenv.cfg").is_file():
            if args.check:
                parser.error(f"virtual environment not found: {environment}")
            if environment.exists():
                parser.error(f"refusing to overwrite a non-venv directory: {environment}")
            venv.EnvBuilder(with_pip=True, system_site_packages=args.system_site_packages).create(environment)
        elif args.system_site_packages:
            parser.error("--system-site-packages applies only when creating a new virtual environment")
        subprocess.run([str(python), "-c",
                        "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) "
                        "and sys.prefix != sys.base_prefix else 'Target must be a Python 3.13+ venv')"], check=True)
        if not args.check:
            subprocess.run([str(python), "-m", "pip", "install", "-e", str(REPOSITORY)], check=True)
        # Resolve configuration only after core dependencies are installed. The
        # bootstrap itself must run in a bare Python environment.
        subprocess.run([str(python), "-c",
                        "import sys; from embodied_runtime.environment_setup import configure_environment; "
                        "configure_environment(sys.argv[1], with_sms=sys.argv[2] == '1', "
                        "check_only=sys.argv[3] == '1')",
                        str(config), str(int(args.with_sms)), str(int(args.check))],
                       cwd=REPOSITORY, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Environment setup failed: {error}", file=sys.stderr)
        return 1
    print(f"Environment ready: {python}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
