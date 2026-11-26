# MCP source broker

The broker lets LiteLLM call MCP tools that live on hosts it cannot reach, by having those hosts dial out to the gateway and keeping that reverse channel open.

This folder is organized as vertical slices: one folder per behavior. Read the folder that matches your question, not the whole tree.

## Slices

| Folder | Behavior | Start here when |
|---|---|---|
| [`running-system/`](running-system/README.md) | What is built and running today | You need current behavior, the status register, or the known gaps |
| [`decisions/`](decisions/README.md) | Undecided and superseded design reasoning | You are changing the design or resuming parked work |
| [`measurements/`](measurements/README.md) | Empirical load and concurrency results | You need numbers, evidence, or what a test proved |
| [`transport/`](transport/README.md) | How data crosses the network boundary | You need transport mechanics or the historical pilot |
| [`history/`](history/README.md) | Retired designs and out-of-scope material | You need why something was decided, not what it does now |

## Which question maps to which slice

| Question | Slice |
|---|---|
| How does a call reach a source host? | `running-system/` then `transport/` |
| Is feature X implemented? | `running-system/` status register |
| Why was Chisel abandoned? | `history/` |
| How many concurrent requests were proven? | `measurements/` |
| Should we add a shared store for replicas? | `decisions/` |
| Why does the folder look like this? | [`../techniques/vertical_slice_technique.md`](../../engineering/techniques/vertical_slice_technique.md) |

## Authority order

Code wins over every document here. Among the documents, `running-system/` is the owning record for current behavior, `measurements/` is the owning record for numbers, and `decisions/` and `history/` never describe current behavior.

## Related

- [`../techniques/vertical_slice_technique.md`](../../engineering/techniques/vertical_slice_technique.md) — the layout rule this folder follows
- [`../additional-features-origin.md`](../../platform/additional-features-origin.md) — where this feature sits among the fork's other slices
