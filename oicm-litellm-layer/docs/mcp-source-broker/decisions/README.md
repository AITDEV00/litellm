# Decisions

## Scope

Design reasoning that is either still open or already decided against. Nothing here describes behavior you can observe in the tree. For that, read [`../running-system/`](../running-system/README.md).

## Contents

| File | Kind | State |
|---|---|---|
| [`approaches/lifecycle-and-pooling-questions.md`](approaches/lifecycle-and-pooling-questions.md) | Open questions | Partly superseded, §2 credential bootstrap still open |
| [`approaches/multi-replica-scaling.md`](approaches/multi-replica-scaling.md) | Debate record | UNFINALIZED, parked by design |

## How to read these

Both files are records of reasoning, not instructions. They carry dates and stated positions because the value is in knowing which arguments were made and which were refuted. Do not implement from either without first checking [`../running-system/README.md`](../running-system/README.md), which states what actually shipped.

When a decision in this folder is settled and built, move the conclusion into `running-system/` and leave only the reasoning here.
