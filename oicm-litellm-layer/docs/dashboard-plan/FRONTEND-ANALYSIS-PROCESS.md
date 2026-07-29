# Frontend Analysis Process — Bugs, Redundancy, Reuse, Extensibility

Status: reference.
Date: 2026-07-29.

This document codifies the strategy used to find and fix the TSX tab-gating bug
(commit `f2e3489bb2` diagnosed it; this process applied the fix). It is the
frontend counterpart to the backend `logic_mapping_technique.md` and
`code_smell_detection_technique.md`. The goal: a repeatable, tool-driven process
for finding rendering bugs, redundant code, missed reuse opportunities, and
extensibility gaps in the `.tsx` codebase, without relying on manual code
inspection.

---

## Core principle

The TypeScript AST already encodes the cause of most frontend bugs. A component
that did not render is gated by a condition somewhere up the tree. A redundant
component duplicates a prop interface that already exists. A missed reuse
opportunity is a function that could call an existing hook but doesn't. Manual
inspection finds these slowly and inconsistently. Static analysis reads them out
directly.

The process has four layers, run in order. Each layer has a tool, a procedure,
and a verification step.

---

## Layer 1 — Render-condition extraction (gating analysis)

### Problem it solves

A component is missing from the screen. The symptom is visible; the cause (which
`{cond && <X/>}` or `{cond ? <A/> : <B/>}` expression hides it) is buried in the
JSX tree, possibly several levels up. Manual tracing through nested JSX is
error-prone and slow.

### Tool

`ui/litellm-dashboard/scripts/extract-gating.mjs`

Walks the TypeScript AST. For every JSX element in a `.tsx` file, determines
whether it is gated by a logical-and (`{cond && <X/>}`) or ternary
(`{cond ? <A/> : <B/>}`) expression, and records the condition text, gate kind,
branch, enclosing JSX element, and direct child element names.

### Procedure

1. Run the extractor on the file or glob where the symptom appears:

   ```bash
   node scripts/extract-gating.mjs "src/components/<dir>/**/*.tsx"
   ```

2. Filter the JSON output for the element that is missing. The `gatedBy` field
   gives the exact condition. The `enclosingJsx` and `childElements` fields give
   the surrounding context.

3. Evaluate the condition against the runtime state that triggers the bug. If
   the condition evaluates to `false` in that state, you have the root cause.

4. Before fixing, check whether other elements share the same gate. A single
   gate often hides multiple elements (the tab bug hid two tabs behind one
   condition). Extracting the full list prevents partial fixes.

### Verification

After the fix, re-run the extractor. The previously-gated element should either
disappear from the output (if the gate was removed) or show a corrected
condition. If the element still appears with the old condition, the fix is
incomplete.

### Worked example (the tab-gating bug)

The extractor identified that the `TabGroup` at `UsagePageView.tsx:531` was
gated by `usageView === "global" || usageView === "my-usage"`. This meant the
Model Analytics and Real-Time Per Model tabs were invisible in 7 of 9 usage
views. The fix extracted those two tabs (which fetch their own data and don't
depend on `userSpendData`) into a separate ungated `TabGroup`, leaving the 5
spend-data-dependent tabs under the original gate. Re-running the extractor
confirmed the operational `TabGroup` no longer appears in the gated set.

---

## Layer 2 — Redundancy detection (duplicate logic and props)

### Problem it solves

Two components implement the same logic with slightly different prop names. Two
`useMemo` hooks compute the same derivation from the same source data. A
hand-rolled formatter duplicates a utility that already exists in `utils/`. These
redundancies are invisible to linters because the code is syntactically valid.

### Procedure

1. **Prop interface comparison.** For a set of sibling components in the same
   feature area, extract their prop interfaces (TypeScript `interface` or `type`
   declarations). Compare field-by-field. Components that share 70%+ of their
   prop shape are candidates for a shared base or a composition.

2. **Hook derivation audit.** For each `useMemo` or `useCallback` in a component,
   record the dependency array and the computation. Group by dependency
   signature. Derivations with identical dependencies and similar computations
   are candidates for extraction into a shared hook.

3. **Formatter/utility grep.** Before writing a new formatting or transformation
   function, grep `utils/` and `shared/` for the operation type (format, parse,
   normalize, convert). The existing dashboard has `valueFormatterSpend`,
   `formatNumberWithCommas`, `processActivityData`, and other utilities that are
   frequently reinvented.

4. **Conditional-rendering pattern grep.** Grep for repeated gating patterns:

   ```bash
   grep -rn "usageView ===" src/components/UsagePage/
   ```

   If the same variable is compared against discrete values in 9+ separate
   `{cond && <X/>}` blocks (as `usageView` is in `UsagePageView.tsx`), the
   component is a switch-statement disguised as JSX. This is a redundancy smell:
   each block duplicates the gating boilerplate. Consider a lookup table or a
   `<UsageViewRouter>` component that maps `usageView` to a component.

### Verification

After consolidating, the grep count for the pattern should drop. The extracted
shared hook or component should have its own test file. The original components
should pass their existing tests unchanged.

---

## Layer 3 — Component reuse and extensibility audit

### Problem it solves

A new feature is built from scratch when an existing component could have been
extended. Or a component is extended by copy-pasting it and modifying two lines,
creating a maintenance fork. The codebase has a `shared/` directory and a set of
hooks under `hooks/` that are designed for reuse, but they are not always
discovered.

### Procedure

1. **Shared component inventory.** Before building a new UI element, list the
   contents of `src/components/shared/` and `src/components/UsagePage/shared/`.
   Current inventory includes `ChartLoader`, `AdvancedDatePicker`,
   `value_formatters`, and the `DataTable` system. If a new card, chart, or
   picker overlaps with any of these, extend rather than create.

2. **Hook inventory.** List hooks under `src/app/(dashboard)/hooks/`. Current
   inventory includes `useModels`, `useCustomers`, `useAgents`, `useUsers`,
   `useCurrentUser`, `useAuthorized`. Any component that needs model data should
   call `useModels`, not hand-roll a `modelInfoCall` fetch.

3. **Extensibility check.** For a component under audit, ask:
   - Does it accept a `className` prop? (If not, it can't be restyled by callers.)
   - Does it accept an `onSelect` / `onChange` callback? (If not, it can't be
     composed into a parent's state.)
   - Does it hardcode a data source, or does it accept data via props? (Hardcoded
     sources prevent reuse in different views.)
   - Does it hardcode a time window, or does it accept a `window` prop?

   Components that fail these checks are not extensible. Fixing them is usually a
   small change (add a prop, default to the current value) that unlocks reuse.

4. **Composition-over-inheritance check.** Verify that the component composes
   smaller pieces rather than monolithically rendering everything. A component
   that renders 7 tabs in one `TabGroup` (as `UsagePageView` did) is a
   monolith. Splitting operational tabs from spend-data tabs (the tab-gating fix)
   is an application of this principle.

### Verification

The new or modified component should be importable from at least two call sites
without prop changes. If it can only be used in one place, it is not reusable and
the design should be reconsidered.

---

## Layer 4 — Fix-then-recheck loop

### Problem it solves

Fixes cascade. Removing a gated element may orphan an import. Extracting a
component may leave a dead variable in the original. Splitting a `TabGroup`
changes tab indices, which can break tests that assert on tab order.

### Procedure

1. After each fix, run the type checker:

   ```bash
   npx tsc --noEmit
   ```

2. Run ESLint on the modified files:

   ```bash
   npx eslint src/components/<dir>/**/*.tsx
   ```

3. Run the component's test file:

   ```bash
   npx vitest run src/components/<dir>/<Component>.test.tsx
   ```

4. Re-run the gating extractor to confirm the structural change is what you
   intended:

   ```bash
   node scripts/extract-gating.mjs "src/components/<dir>/**/*.tsx"
   ```

5. If any step fails, fix the cascade before moving on. Do not batch multiple
   structural fixes without rechecking between them; cascading errors compound
   and become hard to attribute.

### Verification

All four steps pass with zero errors. The gating extractor output matches the
expected post-fix structure.

---

## Layer 5 — Live verification (manual, browser-based)

### Problem it solves

Static analysis and unit tests verify structure and logic, but not visual
rendering. A component can pass all tests and still render blank if a CSS class
hides it or a parent layout clips it.

### Procedure

1. Start the dashboard dev server (or port-forward the deployed proxy).

2. Navigate to the page where the bug was reported.

3. For each usage view or state variant affected by the fix, verify visually:
   - The previously-missing element now renders.
   - No existing element disappeared as a side effect.
   - Layout is not broken (no horizontal overflow, no overlapping elements).

4. For the tab-gating fix specifically, the checklist is:
   - Open `http://localhost:4000/ui/?page=usage`.
   - For each of the 9 usage views (global, my-usage, organization, team,
     customer, tag, agent, user, user-agent-activity), verify the Model Analytics
     and Real-Time Per Model tabs are visible at the bottom of the page.
   - Click each tab and verify it renders its content without errors.
   - In the global and my-usage views, verify the original 5 tabs (Cost, Model
     Activity, Key Activity, MCP Server Activity, Endpoint Activity) are still
     present and functional.

### Verification

All 9 views show the operational tabs. No regressions in the original 5 tabs.

---

## Tooling summary

| Layer | Tool | What it catches | Automated? |
|-------|------|-----------------|------------|
| 1. Gating | `scripts/extract-gating.mjs` | Hidden render conditions | Yes |
| 2. Redundancy | grep + manual prop/hook audit | Duplicate logic, switch-statement JSX | Semi |
| 3. Reuse | `shared/` and `hooks/` inventory | Missed extension opportunities | Manual |
| 4. Recheck | `tsc` + `eslint` + `vitest` + extractor | Cascading side effects | Yes |
| 5. Live | Browser | Visual rendering, layout | Manual |

---

## Application to the tab-gating bug (end-to-end trace)

This section traces the full process as applied to the actual bug, so it can be
used as a reference for future frontend issues.

### Step 1: Diagnose (Layer 1)

Ran the gating extractor on `UsagePageView.tsx`. Output showed:

```
line 531 <TabGroup> gated by: usageView === "global" || usageView === "my-usage"
  childElements: ["div", "TabPanels"]
```

The `TabGroup` contained all 7 tabs, including Model Analytics and Real-Time Per
Model. The gate meant those tabs only rendered in 2 of 9 views.

### Step 2: Analyze dependencies (Layer 3)

Inspected the two operational tab components:

- `ModelAnalyticsView` takes `accessToken`, `modelGroups`, `startTime`,
  `endTime`. It fetches its own data via `modelExceptionCall`,
  `modelLatencyMetricsCall`, etc. The `modelGroups` prop (derived from
  `userSpendData`) is only used for a dropdown selector, not for the data fetch.
- `PerModelRealTimeView` takes `accessToken`, `userID`, `userRole`. It queries
  Prometheus directly. No dependency on `userSpendData`.

Both components are self-sufficient. The gate was wrong: it assumed all 7 tabs
needed `userSpendData`, but only 5 did.

### Step 3: Fix

Removed the two operational tabs from the gated `TabGroup` (lines 539-540 for
the `TabList`, lines 850-860 for the `TabPanel`s). Added a new ungated
`TabGroup` after the last entity-specific panel (after the `user-agent-activity`
block), containing only the two operational tabs.

### Step 4: Recheck (Layer 4)

- `tsc --noEmit`: no errors.
- `vitest run UsagePageView.test.tsx`: 27 tests passed.
- `vitest run extract-gating.test.ts`: 20 tests passed.
- Re-ran the gating extractor: the operational `TabGroup` no longer appears in
  the gated set. The gated `TabGroup` at line 531 now has 5 children, not 7.

### Step 5: Live verify (Layer 5)

Pending deployment. The checklist above defines what to verify in the browser
after the next deploy.
