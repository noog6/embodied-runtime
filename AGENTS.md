# Agent Guidance

`embodied-runtime` is a reusable software runtime for embodied agents. Mira is
the working name of its initial reference/demo robot, not the framework itself.

- The initial reference platform is Python 3.13 on Raspberry Pi OS, running on
  a Raspberry Pi Zero 2 W; the SunFounder Fusion HAT+ is the first hardware
  backend.
- Keep vendor hardware behind clear adapters/interfaces. Application logic must
  not call Fusion HAT+ APIs directly.
- Separate generic runtime behavior from robot/body-specific configuration.
- Do not use Mira-specific names in generic runtime abstractions unless the
  code is genuinely specific to the reference robot.
- Core logic and unit tests must run without physical hardware.
- Prefer simple, maintainable code and minimal dependencies. Add frameworks or
  abstractions only when a concrete need exists.
- Theo may inform design decisions, but do not copy or port Theo wholesale;
  re-evaluate designs for this project.
- Inspect existing code and documentation before editing. Keep changes small
  and focused.
- Do not commit, push, or create pull requests unless explicitly requested.
- Do not run hardware-actuating commands, vendor installation scripts, system
  package changes, kernel/device-tree changes, or other host configuration
  without explicit instruction.

Keep design and hardware knowledge under `docs/`; do not expand this file into
detailed documentation. See `docs/design-principles.md` for durable project
constraints, `docs/ai-stack.md` for AI-related work, `docs/architecture.md`,
and `docs/hardware/fusion-hat-plus.md`.
