# Decisions

## Scope

Design reasoning that is either still open or already decided against. Nothing here describes behavior you can observe in the tree. For that, read [`../running-system/`](../running-system/README.md).

## Contents

| File | Kind | State |
|---|---|---|
| [`approaches/byom-self-service.md`](approaches/byom-self-service.md) | Design | SHIPPED 2026-11-18; kept for the reasoning and the record of where the code departed |
| [`approaches/connector-owned-enrollment.md`](approaches/connector-owned-enrollment.md) | Requirement | IMPLEMENTED 2026-11-18: user identity, not a team key, and the connector owns the registration calls; approved live read on dev still blocked |
| [`approaches/lifecycle-and-pooling-questions.md`](approaches/lifecycle-and-pooling-questions.md) | Open questions | Mostly closed with §2 partly answered; identity/bootstrap remains in `connector-owned-enrollment.md` |
| [`approaches/multi-replica-scaling.md`](approaches/multi-replica-scaling.md) | Debate record | UNFINALIZED, parked by design |

## How to read these

These files are records of reasoning, not instructions. They carry dates and stated positions because the value is in knowing which arguments were made and which were refuted. Do not implement from any of them without first checking [`../running-system/README.md`](../running-system/README.md), which states what actually shipped.

When a decision in this folder is settled and built, move the conclusion into `running-system/` and leave only the reasoning here.
