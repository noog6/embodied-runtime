# Contributor guidance

`embodied-runtime` is a reusable runtime for embodied agents. Mira is its
reference robot, not the framework's name.

## Find the relevant context

- Start with [the documentation guide](docs/README.md) to locate the topic's
  detailed reference. Read the affected implementation and tests before editing.
- Follow [design principles](docs/design-principles.md) and
  [architecture](docs/architecture.md) for runtime boundaries. Keep detailed
  architecture, hardware facts, and deployment instructions under `docs/`.
- Use [pyproject.toml](pyproject.toml) as the source for supported Python versions
  and dependencies. See [startup](docs/startup.md) for environment setup.
  Do not assume the development host has Mira's hardware or deployment settings.

## Make focused changes

- Preserve the distinction between generic runtime behavior and robot-specific
  profiles, configuration, and adapters. Use Mira-specific names only for
  genuinely reference-robot-specific behavior.
- Keep vendor hardware APIs behind adapters; application logic must not call
  Fusion HAT+ APIs directly. Core logic and unit tests must run without physical
  hardware.
- Prefer existing interfaces and minimal dependencies. Add abstractions only
  for a concrete requirement; avoid unrelated refactors or speculative features.
- Keep resource use appropriate for constrained deployments, including the
  Raspberry Pi Zero 2 W. Consult the relevant feature contract before changing
  execution bounds, ownership, persistence, or resource arbitration.
- When behavior changes, update its detailed reference and affected summaries
  or examples. Preserve unique rationale, edge cases, and validation evidence.
  Resolve documentation contradictions against implementation and tests rather
  than copying an older statement into another page.

## Validate and report

Use a supported Python interpreter and an isolated development environment.
Install the declared test extra there when needed:

```sh
python -m pip install -e '.[test]'
```

Run the relevant existing `unittest` modules for a focused change, for example:

```sh
python -m unittest tests.test_attention tests.test_job_concurrent_slots
```

For changes that warrant a broader regression check, run from the repository root:

```sh
python -m unittest discover -s tests -t .
```

Add or adjust tests when behavior changes, including relevant failure and cleanup
paths. For documentation-only edits, check factual claims, links, and examples;
a full runtime test run is not required solely for prose changes.

Report what changed, what was checked, and any failures or environment limits.
Distinguish offline tests from live-provider and physical-hardware validation;
do not describe unperformed checks as passing.

## Respect the requested scope

- Preserve unrelated user changes.
- Commit, push, or create/update a PR when the user's request authorizes that
  workflow. A request to create or update a PR includes the necessary commits
  and branch push; do not ask for the same permission again. For review-only
  work, leave the repository unchanged. Do not merge without authorization.
- Code edits and offline tests do not authorize deployment or hardware
  operation. Run hardware-actuating commands, vendor installation scripts,
  system package changes, kernel/device-tree changes, or other host
  configuration only when explicitly instructed. Reuse authorization already
  given for the current task.
