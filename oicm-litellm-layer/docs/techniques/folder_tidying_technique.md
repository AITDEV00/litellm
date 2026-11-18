# Vertical Slice Technique — Tidying a Messy Folder

Use this when a folder has accumulated documents that overlap, contradict each other, or mix finished work with history and proposals. The goal is a folder where each document has exactly one job, exactly one owner of each fact, and a reader can tell what is current without reading everything.

This is a **refactoring procedure for an existing mess**, not a greenfield layout rule. Apply it one folder at a time, and commit after each step so the improvement is durable even if you stop early.

The procedure rests on two ideas. Diátaxis holds that documents serve different needs, and that mixing those needs inside one file is the root cause of drift. The second is empirical: most real documentation messes are caused by **one fact written in several places**, not by bad individual documents.

## Step 0 — Do not start with structure

The instinct when facing a messy folder is to invent a new layout and move everything into it. Resist it. Moving files before understanding what each one is produces a new mess and breaks every inbound link.

Measure first. Three commands give almost everything:

```bash
wc -l *.md | sort -n                    # which docs are sprawling
grep -lc '<topic>' *.md                 # which docs cover the same topic
grep -rn 'Status:\|DONE\|IN PROGRESS' *.md   # which docs claim status
```

A document much larger than its peers is usually two or three documents that were never split. A topic appearing in four files is a fact with four owners, which means three are already stale.

## Step 1 — Classify every document by its single job

Read the first twenty lines and the headings of each file, then assign exactly one class. If a file needs two classes, that is a split candidate, recorded in Step 3.

| Class | Contains | Test question | Example |
|---|---|---|---|
| **Reference** | what exists now: paths, ports, constants, contracts | "if I change the code, must this change?" | owning implementation record |
| **Explanation** | why it is built this way, tradeoffs, dead ends | "does this stay true even if code changes?" | design debate record |
| **How-to** | steps to accomplish a task | "is this a sequence of actions?" | runbook, deploy guide |
| **History** | what was true on a date: measurements, incident narrative | "must this never be edited again?" | dated perf record |
| **Proposal** | what we intend to build, superseded or not | "is this describing something unbuilt?" | design plan marked superseded |

Two rules make the classification deterministic:

- **A file with a `Status:` marker, a date in its title, or measured numbers is History.** It is append-only. Never correct its facts to match today.
- **A file answering "where is X" for current code is Reference.** Exactly one per folder. If two claim that role, one is wrong and Step 2 decides which.

## Step 2 — Build the overlap matrix

For each pair of documents, name the fact or topic they both cover, and write it down. This is the only analysis step that costs real time, and the one that stops the new layout from being as ambiguous as the old one.

```markdown
| Fact | Docs that state it | Which one is current | Action |
|---|---|---|---|
| supported transports | README, impl, research | impl | delete from the others, link instead |
| port map | README, impl, structure | impl | keep in impl, delete elsewhere |
| enrollment routes | README §5.1, impl | impl (README is proposal) | mark README section superseded |
```

Resolution rules, in order of preference:

1. **One owner per fact.** Elect the document classed as Reference in Step 1. Every other copy is deleted and replaced with a link.
2. **If the other copy is History, do not delete it.** History records what was believed then. Mark it superseded and point at the current owner.
3. **If two Reference documents disagree, the code wins.** Verify against the tree, correct both, then delete the duplicate.

## Step 3 — Split the documents that serve multiple classes

A file that is both Reference and History will eventually lie, because Reference must be edited and History must not. Split it.

| Symptom | Split into |
|---|---|
| 600+ lines with an append-only tail of results and a "current state" head | `implementation.md` (Reference) plus `perf/YYYY-MM-DD-topic.md` (History) |
| A status register plus hundreds of lines of step-by-step evidence | a status section at the top (Reference), evidence moved to an appendix explicitly labelled historical |
| A proposal in which some mechanisms were later replaced | keep the file, add a superseded banner naming the replacements, and stop editing the body |

The test: after the split, can you say of each paragraph either "this must change when the code changes" or "this must never change again"? If one file needs both answers, it is not split enough.

## Step 4 — Name each file for its class and job

Names decide whether the next person finds the right file without opening it.

| Class | Name pattern | Good | Bad |
|---|---|---|---|
| Reference | the thing it documents | `dev-implementation.md` | `README.md` for a design proposal |
| History | `YYYY-MM-DD-topic.md` | `2026-11-18-go-driver-concurrency.md` | `concurrency.md` |
| Proposal | a plain plan name plus a superseded banner | `PLAN.md` | `VSA-PLAN.md` for something already built |
| How-to | verb-noun name inside `runbooks/` | `postgres-backup-and-recovery.md` | `NOTES.md` |

One trap deserves naming. A folder's `README.md` is read first and trusted most. If it is a superseded proposal, it becomes the most detailed and least true document in the folder. Either rename it, or open it with a banner stating what is current and where it lives, before a reader sees anything else.

## Step 5 — Fix every inbound reference in the same commit

Moving or renaming a file silently breaks links. Do this as part of the same change, not later.

```bash
grep -rn 'old-name' --include=*.md --include=*.yml . | grep -v '/site/'
```

Then verify mechanically that the documentation site still resolves:

```bash
python3 - <<'PY'
import yaml,os
nav=yaml.safe_load(open("mkdocs.yml"))["nav"]
missing=[]
def walk(items):
    for i in items:
        if isinstance(i,dict):
            for _,v in i.items():
                walk(v) if isinstance(v,list) else (None if v.startswith("http") or os.path.exists(os.path.join("docs",v)) else missing.append(v))
        elif isinstance(i,str) and not i.startswith("http") and not os.path.exists(os.path.join("docs",i)):
            missing.append(i)
walk(nav); print("missing:", missing or "none")
PY
```

Expect `missing: none` before committing. One deliberate exception: a changelog records what was true when written, so historical paths inside it stay as they were.

## Step 6 — Leave a map so the next pass is cheaper

Add one index line per document stating its class and its job. Without this the folder re-messes within months, because the classification you just did exists only in this session.

Test of success: a new reader finds the current owner of any fact from the index alone, without opening a document.

## Done criteria

1. Every document has exactly one class, visible from its banner, name, or date.
2. Exactly one Reference document per folder. Any second candidate is deleted or explicitly marked superseded.
3. Every fact stated more than once has a single named owner, and the other copies are links or marked superseded.
4. No file mixes Reference and History.
5. Zero stale references, and the nav check prints `missing: none`.
6. The index states the class and job of each document.

## Anti-patterns this prevents

| Anti-pattern | Why it hurts | Detection |
|---|---|---|
| A superseded proposal as `README.md` | the most-read file is the least true | name check in Step 2 |
| Status markers inside a measurement record | forces edits to history | `Status:` in a dated file |
| The same port map in three files | they diverge silently | overlap matrix |
| "Next steps" duplicating a status register | two authorities on what is done | `IN PROGRESS` in more than one file |
| Deleting history because it contradicts current code | destroys the record of why | History class rule |
| Renaming before measuring | new mess, broken links | Step 0 |

## Worked example: `docs/mcp-source-broker`

The folder as measured on 2026-11-18, before any cleanup:

| File | Lines | Class (by rule) | Problem |
|---|---:|---|---|
| `README.md` | 612 | Proposal | read first, most detailed, least true; specifies routes and a transport that do not exist |
| `LiteLLM-v1.108.5-Unified-MCP-Implementation-Guide.md` | 1047 | Explanation | longest file, mixes design with claims about current behavior |
| `dev-implementation.md` | 230 | Reference | the actual owner, but buried below two larger files |
| `LiteLLM-Reverse-Tunnel-Nginx-Pilot.md` | 295 | History | correct as history, but named like a current guide |
| `MULTI-REPLICA-DESIGN-DEBATE.md` | 127 | Explanation | correctly scoped |
| `research-questions.md` | 85 | Proposal | correct, but its open question is duplicated in the README as if decided |

Applying the steps produced: one Reference owner (`dev-implementation.md`) carrying a status register and a known-gaps section, a superseded banner on the Proposal that names the routes and transport it got wrong, a header on the research file marking it partly superseded, and this procedure.

## Scope boundary

This technique cleans and classifies documents. It does not decide whether a document should exist, resolve genuine design disagreements, or restore missing evidence. When a fact cannot be settled from the tree, record it as an open question inside the Reference document rather than letting a History or Proposal file imply an answer.

For where new **code** belongs in this fork, that is a separate problem with a separate rule; see [`logic_mapping_technique.md`](logic_mapping_technique.md) Phase 3 and [`additional-features-origin.md`](../additional-features-origin.md).
