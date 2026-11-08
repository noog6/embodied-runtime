# embodied-runtime

embodied-runtime gives robot builders reusable software for conversation,
camera vision, and scheduled work. Mira is the robot we built to show it in
action.

You can talk to your robot by voice or text, ask it what it sees, or set up a
scheduled health check that leaves a report you can inspect later.

What works depends on the hardware and setup. Voice needs supported audio
hardware; live camera questions need a camera and a configured vision service.
AI features need a configured model provider. You can also run with virtual
hardware for development.

- **[What you can do with Mira](docs/tested-jobs.md)** — for now, explore
  **Tested Job patterns**, examples exercised during Mira development. These
  examples are not installed automatically.
- **[Run it on your robot](docs/startup.md#quick-start)** — prepare Python,
  choose your robot's configuration, and launch the runtime.
- **[Browse the technical docs](docs/README.md)** — find setup, operation,
  hardware support, architecture, and feature references.
