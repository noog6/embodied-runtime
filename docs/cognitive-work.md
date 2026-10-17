# Mira's cognitive work

This map shows one layer of the runtime: how Mira's bounded cognitive episodes
relate to evidence, durable work, memory, activation, and communication. It is a
capability map, not a promise that every capability is available in every episode.
The runtime projects and validates tools according to current configuration,
authority, resources, ownership, and budgets.

![Mira's cognitive episode connected to Jobs, evidence and history, semantic memory,
Findings, Job Workspaces, the shared image pipeline, communication channels, and
one-shot follow-ups.](diagrams/cognitive-work.svg)

Download the [vector image](diagrams/cognitive-work.svg) or the
[PNG image](diagrams/cognitive-work.png). The editable
[Mermaid source](diagrams/cognitive-work.mmd) preserves the diagram separately
from its rendered images.

## Reading the map

- **Purple:** one bounded cognitive episode. A goal and Job execution context can
  connect episodes; no model session stays alive continuously between them.
- **Blue:** durable responsibilities or stored material. Jobs, Workspaces,
  Findings, and semantic memory serve different purposes and have different
  authority rules.
- **Green:** evidence, historical context, and image processing. Visual
  interpretation is fallible. Reading old material does not make it a fresh
  observation.
- **Orange:** interaction with the operator through configured channels.
  Availability and provider acceptance do not establish human receipt.
- **Gray:** runtime activation and bounded timing. Daily Job schedules are
  durable; the single goal-bound follow-up is session-local.

Arrows describe permitted relationships, not automatic activity. Capturing or
loading an image does not save, interpret, or send it automatically. Saved-image
interpretation and delivery use retained snapshots; saving and sending require
no vision call. The Gemini/OpenAI label illustrates Mira's configured deployment;
the runtime supports an explicitly configured ordered provider chain.

Job work accesses only its own Workspace through the exact live execution
binding. Operator access resolves an exact existing Job. Durable writes require
the applicable authority; operator image saves use an exact request-scoped grant.
Jobs cannot send unsolicited images.

The history box combines several distinct sources for readability: working memory
is volatile, conversation history is durable completed operator dialogue, and
run history is operational evidence. Their selection and tool availability differ
by episode type. Explicit operator dialogue can deliberately search retained older
conversation turns, including same-process evictions, and inspect exact request
links to JobRuns and delivery attempts. These acquisitions share the existing
budgets and are unavailable to Job work. Authored output, transport acceptance,
playback completion, and human receipt remain separate facts; see
[conversation continuity](conversation-history.md) for examples and bounds.

Semantic-memory recall is read-only. Operator dialogue may request one bounded
`remember` admission grounded in the current operator statement about an existing
entity. Autonomous conclusions are not admitted as semantic memory. Jobs can
instead write authorized Workspace artifacts and publish bounded Findings.
Completed-run Findings can be searched across Jobs without granting access to
another Job's Workspace.

An explicitly maintained handoff note is an ordinary text artifact, not a new
memory mechanism. It can carry an agreed objective, a dated result with its
evidence source, and an unresolved decision. It remains historical working
material and grants no new action authority.

This layer sits above deterministic sensing, state ownership, reflexes, resource
arbitration, hardware adapters, and transport services. Those layers continue to
have their own lifecycles; this diagram does not describe the whole runtime.

## Related documentation

- [Architecture and bounded episode grammar](architecture.md)
- [Jobs, progress, triggers, and daily schedules](jobs.md)
- [Job Workspaces](job-workspaces.md) and [Workspace images](workspace-images.md)
- [Persistent memory](persistent-memory.md) and [Findings](findings.md)
- [Conversation history](conversation-history.md) and [run history](run-history.md)
- [Visual perception](visual-perception.md) and [self-inspection](self-inspection.md)
- [Bounded temporal follow-up](temporal-followup.md)
- [Console](console.md), [SMS/MMS](sms.md), and [voice](voice-conversation.md)

## Updating the diagram

Edit `diagrams/cognitive-work.mmd`, then render both images with Mermaid CLI:

```sh
mmdc -i docs/diagrams/cognitive-work.mmd -o docs/diagrams/cognitive-work.svg -b white
mmdc -i docs/diagrams/cognitive-work.mmd -o docs/diagrams/cognitive-work.png -b white -s 2
```

Mermaid rendering is documentation tooling, not a runtime dependency. Keep the
source and images together when changing the map.
