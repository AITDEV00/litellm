# Frontend Understanding Technique: How to Map and Reason About the LiteLLM Dashboard Codebase

Status: reference.
Date: 2026-07-30.

This document is a comprehensive strategy for understanding the LiteLLM dashboard
frontend (`ui/litellm-dashboard/src/`) from scratch. It is designed for an
engineer who needs to make changes to an unfamiliar React/Next.js codebase of
this scale (~80 top-level components, ~280 API wrappers, ~30 feature routes,
~150 test files) without breaking existing behavior.

It complements three sibling documents in this folder:

- `FRONTEND-ANALYSIS-PROCESS.md` — the 5-layer static analysis process for
  finding rendering bugs, redundancy, reuse gaps, and extensibility issues
- `LOGIC-MAPPING-TECHNIQUE.md` — the 4-phase trace/test/build/verify workflow
  for backend or full-stack features
- `UI-LINT-AND-CHANGE-PROCESS.md` — the lint, type, and CI gating rules that
  every UI change must pass

This document is the top-level entry point. It tells you how to orient yourself
in the codebase, what to read first, what to ignore until later, and how to
combine the sibling techniques into a workflow.

---

## 1. Why a technique is needed

The LiteLLM dashboard is not a small app. A naive approach (open a file, start
reading, follow imports) gets lost quickly because:

- The API layer (`networking.tsx`) is a single 5,000+ line file with ~280
  exported functions. There is no index. Finding the right function requires
  knowing the naming convention.
- Components are organized by feature, not by type. A "key" feature touches
  files in `app/(dashboard)/api-keys/`, `components/key_team_helpers/`,
  `components/VirtualKeysPage/`, `app/(dashboard)/hooks/keys/`, and
  `components/organisms/create_key_button/` simultaneously.
- There are two generations of patterns coexisting: legacy `fetch()` calls
  grandfathered in `networking.tsx`, and the modern `apiClient` in `lib/http/`.
  New code must use the modern pattern, but you will read both.
- Render conditions are buried in nested JSX. A component that doesn't appear on
  screen is gated by an expression several levels up the tree. Manual tracing
  is unreliable.
- Tests are colocated but inconsistent in depth. Some verify behavior; others
  only check that a component renders. You need to know which is which before
  trusting them.

The technique below addresses each of these.

---

## 2. The codebase map (read this first)

Before reading any code, internalize this structural map. It tells you where
each kind of thing lives, so you can navigate by purpose rather than by
grep.

```
ui/litellm-dashboard/src/
│
├── app/                          Next.js App Router (routing + page entry points)
│   ├── layout.tsx                Root layout: ReactQueryProvider → AntdGlobalProvider → AuthProvider
│   ├── (dashboard)/              Authenticated admin shell (the main app)
│   │   ├── layout.tsx            Navbar, Sidebar, ThemeProvider, PluginModeProvider
│   │   ├── page.tsx              Home: routes ApiKeysDashboard vs UserDashboard
│   │   ├── hooks/                Feature-scoped React Query hooks (keys/, teams/, models/, etc.)
│   │   └── <feature>/page.tsx    ~30 feature routes (api-keys, usage, logs, teams, etc.)
│   ├── chat/                     Separate chat UI (own layout)
│   ├── login/                    Authentication page
│   ├── onboarding/               Invitation-based key creation
│   └── model_hub/                Public model hub
│
├── components/                   The bulk of the UI
│   ├── networking.tsx            THE API LAYER (~280 exported functions, 5000+ lines)
│   ├── ui/                       Radix/shadcn design-system primitives (button, dialog, select, etc.)
│   ├── atoms/                    Single-purpose atomic components
│   ├── molecules/                Composite widgets (filter, message_manager, notifications)
│   ├── organisms/                Higher-level composites (RegenerateKeyModal, create_key_button)
│   ├── common_components/        Shared building blocks (LoadingScreen, ModelSelector, dropdowns, filters)
│   ├── shared/                   Smaller shared set (chart_loader, date pickers, errorUtils)
│   ├── Navbar/                   Top navigation
│   ├── view_logs/                Log viewer system (table, columns, filters, drawers)
│   ├── add_model/                Add-model form system
│   ├── guardrails/               Guardrail management
│   ├── policies/                 Policy management
│   ├── mcp_tools/                MCP server/tool management
│   ├── team/                     Team management tabs
│   ├── UsagePage/                Usage analytics (components, hooks, utils)
│   ├── <feature>/                Other feature directories (agents, router_settings, etc.)
│   └── <standalone>.tsx          ~80 top-level component files
│
├── contexts/                     7 React Context providers
│   ├── AuthContext.tsx           Token, userID, userRole, accessToken, premiumUser
│   ├── ReactQueryProvider.tsx    TanStack Query client
│   ├── AntdGlobalProvider.tsx    Ant Design ConfigProvider + global notification/message
│   ├── ThemeContext.tsx          Logo, favicon URLs
│   ├── PluginModeContext.tsx     Plugin mode switching
│   └── ChatShellContext.tsx      Chat UI state
│
├── hooks/                        Cross-cutting hooks (MCP OAuth, worker switching, etc.)
├── data/                         Static data (compliance prompts)
├── lib/                          Infrastructure utilities
│   ├── http/client.ts            THE HTTP CLIENT (only file allowed to call fetch())
│   ├── http/schema.d.ts          AUTO-GENERATED from proxy OpenAPI spec (do not hand-edit)
│   ├── http/resolveApiBase.ts    Base URL resolution
│   ├── serverRootPath.ts         Proxy server root path holder
│   └── assetPaths.ts             Asset URL helpers
├── utils/                        20 pure utility files (each with colocated tests)
└── types.ts                      Shared TypeScript types
```

### Key mental model

The data flow is unidirectional:

```
page.tsx (route entry)
    │
    ▼
context provider (AuthContext gives accessToken)
    │
    ▼
feature component (e.g. UsagePageView, ApiKeysDashboard)
    │
    ▼
hook (e.g. useModels, usePaginatedDailyActivity) ─── React Query cache
    │
    ▼
networking.tsx function (e.g. modelInfoCall, keyListCall)
    │
    ▼
lib/http/client.ts (apiClient.get / apiClient.post)
    │
    ▼
fetch() → proxy backend
```

Every feature follows this chain. If you understand one feature end-to-end, you
understand the pattern for all of them.

---

## 3. The 6-step understanding workflow

### Step 1: Orient (5 minutes)

Read the structural map above. Then open these three files in order to calibrate
your expectations:

1. `src/app/layout.tsx` — see the root providers and understand what global
   state exists (React Query, Ant Design, Auth)
2. `src/app/(dashboard)/layout.tsx` — see the app shell (Navbar, Sidebar,
   Theme, PluginMode)
3. `src/lib/http/client.ts` — see the HTTP client. This is the only file that
   calls `fetch()`. Understanding its `createApiClient()` factory, base URL
   resolution, and error handling tells you how every API call works.

Do not read `networking.tsx` yet. It is too large to read linearly. You will
grep it for specific functions in Step 3.

### Step 2: Pick one feature and trace it end-to-end

Choose the feature you need to change (or, if learning the codebase, pick
`api-keys` because it exercises every layer). Trace it from route to fetch:

1. **Route entry**: open `src/app/(dashboard)/api-keys/page.tsx`. This is a thin
   file that renders a dashboard component. Note what props it passes and what
   context it reads.

2. **Dashboard component**: open the component it renders (e.g.
   `ApiKeysDashboard.tsx`). Note what hooks it calls, what state it manages,
   what child components it renders.

3. **Hook layer**: find the hooks it calls. These may be in
   `src/app/(dashboard)/hooks/keys/` or in `src/components/`. Hooks wrap
   React Query `useQuery` / `useMutation` calls. Each hook calls one or more
   functions from `networking.tsx`.

4. **API wrapper**: open `networking.tsx` and find the function the hook calls.
   Use grep, not scrolling:
   ```bash
   grep -n "export const keyListCall" src/components/networking.tsx
   ```
   Note the URL path, the HTTP method, the params, and the return type.

5. **HTTP client**: confirm the function uses `apiClient.get()` or
   `apiClient.post()`. If it uses raw `fetch()`, it is legacy code; note this
   but do not replicate the pattern.

You now understand one complete vertical slice. Every other feature follows the
same shape.

### Step 3: Map the API surface for your feature

`networking.tsx` has ~280 functions with no index. To find the ones relevant to
your feature, grep by domain prefix:

```bash
# Keys
grep -n "export const key.*Call" src/components/networking.tsx

# Teams
grep -n "export const team.*Call" src/components/networking.tsx

# Models
grep -n "export const model.*Call" src/components/networking.tsx

# Users
grep -n "export const user.*Call" src/components/networking.tsx

# Spend / usage
grep -n "export const.*spend\|export const.*usage\|export const.*activity" src/components/networking.tsx
```

Record each function name, the URL path it hits, and the HTTP method. This is
your feature's API contract. If a backend endpoint exists that has no wrapper
here, you will need to add one (following the `apiClient` pattern, not `fetch()`).

Also check `src/lib/http/schema.d.ts` for the auto-generated types of these
endpoints. If the type you need is there, import it rather than hand-writing a
duplicate.

### Step 4: Identify render conditions (gating analysis)

Before changing any JSX, determine what gates the components you care about.
This prevents the most common frontend bug: a component that doesn't render
because a condition several levels up evaluates to false.

Use the gating extractor:

```bash
cd ui/litellm-dashboard
node scripts/extract-gating.mjs "src/components/<feature>/**/*.tsx"
```

The output is a JSON array. For each JSX element, it records:
- `gatedBy`: the condition text (e.g. `usageView === "global"`)
- `gateKind`: `logical-and` or `ternary`
- `enclosingJsx`: the parent element
- `childElements`: what's inside

Filter for the component you plan to change. If it is gated, evaluate the
condition against the runtime states where it should appear. If the condition
is wrong or too narrow, that is a bug to fix before your feature work.

This step is described in full in `FRONTEND-ANALYSIS-PROCESS.md` Layer 1.

### Step 5: Audit reuse opportunities before writing new code

Before creating a new component, hook, or utility, check whether one already
exists:

1. **Shared components**: list `src/components/shared/` and
   `src/components/common_components/`. These are the reusable building blocks.
   Current inventory includes `ChartLoader`, `AdvancedDatePicker`,
   `ModelSelector`, `OrganizationDropdown`, `FilterTeamDropdown`,
   `DeleteResourceModal`, `LabeledField`, `DurationSelect`, and more.

2. **Hooks**: list `src/app/(dashboard)/hooks/` for feature-scoped hooks, and
   `src/hooks/` for cross-cutting ones. If you need model data, use `useModels`,
   not a hand-rolled `modelInfoCall` fetch. If you need team data, use the
   existing team hooks.

3. **Utilities**: grep `src/utils/` for the operation type before writing a new
   helper. Existing utilities cover: cookie management, JWT decoding, role
   formatting, error extraction, key expiry/update, localStorage, MCP
   headers/tokens, team operations, text formatting, budget calculations, PKCE,
   and page migration mapping.

4. **API wrappers**: grep `networking.tsx` before adding a new wrapper. If a
   wrapper exists for the endpoint you need, reuse it. If not, add one following
   the `apiClient.get/post/put/delete` pattern.

This step is described in full in `FRONTEND-ANALYSIS-PROCESS.md` Layer 3.

### Step 6: Verify after every change

After making changes, run the fix-then-recheck loop:

1. Type-check: `npx tsc --noEmit`
2. Lint: `npx eslint src/components/<dir>/**/*.tsx`
3. Unit tests: `npx vitest run src/components/<dir>/<Component>.test.tsx`
4. Re-run the gating extractor if you changed JSX structure
5. Pre-commit: `make pre-commit` from the repo root
6. Live verify in the browser (port-forward the proxy, navigate to the affected
   page, check all state variants)

This loop is described in full in `FRONTEND-ANALYSIS-PROCESS.md` Layer 4 and 5,
and the lint/type rules are in `UI-LINT-AND-CHANGE-PROCESS.md`.

---

## 4. Naming conventions (how to find things)

The codebase follows consistent naming patterns. Knowing them lets you find
any function or component by guessing its name:

| Thing | Convention | Example |
|-------|-----------|---------|
| API wrapper function | `<entity><Action>Call` | `keyCreateCall`, `teamListCall`, `modelInfoCall` |
| React Query hook | `use<Entity>` or `use<Action><Entity>` | `useModels`, `useCustomers`, `useDeletePolicyAttachment` |
| Feature route | `app/(dashboard)/<kebab-name>/page.tsx` | `api-keys`, `models-and-endpoints`, `mcp-servers` |
| Feature component dir | `components/<PascalName>/` | `UsagePage`, `VirtualKeysPage`, `add_model` |
| Test file | `<source>.test.tsx` (colocated) | `UsagePageView.test.tsx` next to `UsagePageView.tsx` |
| Shared component | `components/shared/` or `components/common_components/` | `chart_loader.tsx`, `ModelSelector.tsx` |
| Context | `<Name>Context.tsx` in `contexts/` | `AuthContext.tsx`, `ThemeContext.tsx` |
| Utility | `src/utils/<name>Utils.ts` | `cookieUtils.ts`, `jwtUtils.ts`, `teamUtils.ts` |

When looking for a function, grep with the convention:

```bash
# "I need to create a key" →
grep -n "keyCreate" src/components/networking.tsx

# "I need model info" →
grep -n "modelInfo" src/components/networking.tsx

# "I need a hook for teams" →
find src/app/\(dashboard\)/hooks/teams -name "*.ts" -o -name "*.tsx"
```

---

## 5. The two generations of patterns

The codebase has legacy and modern patterns coexisting. You must use the modern
pattern for new code, but you will read both when tracing existing features.

### API calls

| Aspect | Legacy (do not replicate) | Modern (use this) |
|--------|--------------------------|-------------------|
| HTTP method | Raw `fetch()` with manual headers | `apiClient.get/post/put/delete` |
| Location | `networking.tsx` (grandfathered) | `networking.tsx` (new functions) using `apiClient` from `lib/http/client.ts` |
| ESLint | Suppressed via `eslint-suppressions.json` | No suppression needed |
| Error handling | Manual try/catch with `console.error` | `ApiError` thrown by `apiClient`, caught by caller |

### Charts

| Aspect | Legacy | Modern |
|--------|--------|--------|
| Library | `@tremor/react` (grandfathered via suppressions) | Ant Design components |
| Rule | `no-restricted-imports` flags Tremor as "being phased out" | New components must use antd |
| Exception | If visual consistency within a page requires Tremor, add a suppression | N/A |

### Component structure

| Aspect | Legacy | Modern |
|--------|--------|--------|
| File size | Some files are 500+ lines (e.g. `networking.tsx`, `UsagePageView.tsx`) | Prefer smaller, focused components |
| Props | Some components hardcode data sources | Accept data via props; use hooks for fetching |
| State | Some components manage state with `useState` chains | Prefer React Query for server state, `useState` only for UI state |

---

## 6. Context providers: what global state exists

Before reading any component, know what global state is available via context:

| Context | What it provides | When to use it |
|---------|-----------------|----------------|
| `AuthContext` | `token`, `userID`, `userRole`, `userEmail`, `accessToken`, `premiumUser`, `disabledPersonalKeyCreation` | Any component that needs auth token for API calls or needs to check user role |
| `ReactQueryProvider` | Shared `QueryClient` | Automatically available; hooks use `useQuery` / `useMutation` |
| `AntdGlobalProvider` | Global `notification` and `message` instances | For toast/notification messages via `notifications_manager` and `message_manager` |
| `ThemeContext` | Logo URL, favicon URL | For branding customization |
| `PluginModeContext` | `mode`, `plugins[]`, `activePlugin` | For plugin mode switching |
| `ChatShellContext` | Chat conversations, active conversation, MCP selection | Only for chat UI components |

To consume a context:
```tsx
const { accessToken, userID, userRole } = useContext(AuthContext);
```

Or via the convenience hook (if the context exports one, e.g. `useTheme()`).

---

## 7. Testing strategy

### What tests exist

- **Unit tests** (Vitest, jsdom): ~150+ colocated `.test.tsx` / `.test.ts` files.
  Config: `vitest.config.ts`. Utils: `tests/test-utils.tsx` provides
  `renderWithProviders()` which wraps in `QueryClientProvider` with a test
  `QueryClient`.
- **E2E tests** (Playwright): `e2e_tests/tests/` organized by role/scenario.
  Config: `e2e_tests/playwright.config.ts`.
- **Static analysis**: gating extractor (`scripts/extract-gating.mjs`), ESLint
  budgets, Knip dead-code detection.

### How to evaluate existing tests

Not all tests are equal. Before trusting a test file, check:

1. Does it mock the networking calls? (If not, it may be hitting a live API or
   failing silently.)
2. Does it assert on user-visible output (text, behavior) or just on "it
   renders without crashing"? The latter provides minimal signal.
3. Does it cover the edge cases (empty data, error states, loading states) or
   only the happy path?

### How to write a good test

Follow the pattern in the strongest existing test files:

1. Mock networking functions at the module level:
   ```tsx
   vi.mock("../../../components/networking", () => ({
     keyListCall: vi.fn(),
     keyCreateCall: vi.fn(),
   }));
   ```
2. Use `renderWithProviders()` from `tests/test-utils.tsx`.
3. Assert on user-visible output (text content, element presence/absence), not
   on internal state or implementation details.
4. Test the edge cases: null `accessToken`, empty data, API error, loading
   state.
5. Test the `enabled` guard on `useQuery` hooks (verify no API call is made when
   prerequisites are missing).

---

## 8. Common pitfalls

### 8.1 Assuming a component renders

The most common bug is a component that doesn't appear because a gating
condition upstream is false. Always run the gating extractor before and after
changes (Step 4 above).

### 8.2 Using raw `fetch()`

ESLint bans `fetch()` everywhere except `src/lib/http/`. New API wrappers must
use `apiClient`. If you copy an existing wrapper that uses `fetch()`, you are
copying legacy code; rewrite it with `apiClient`.

### 8.3 Adding `any` types

The lint budget tracks `@typescript-eslint/no-explicit-any` usage. Adding `any`
pushes toward the ceiling. If you need to type an untyped API response, validate
it with a schema or `TypeAdapter` in the caller and pass the typed variable in.
This matches the Python-side convention in `CLAUDE.md`.

### 8.4 Forgetting to regenerate API types

If a backend endpoint changes, `src/lib/http/schema.d.ts` must be regenerated:

```bash
cd ui/litellm-dashboard
LITELLM_PYTHON="uv run --no-sync python" npm run gen:api
```

CI checks for drift. If the committed `schema.d.ts` is stale, the
`check-ui-api-types.yml` workflow fails.

### 8.5 Breaking lint budgets

Lint budgets ratchet down. If you fix violations (remove `console.log`,
eliminate `any`), run `npm run lint:metrics` and commit the updated
`eslint-metrics.json`. If you add violations, you may exceed the ceiling and
the build fails.

### 8.6 Not running `make pre-commit`

`make pre-commit` runs prettier, eslint, and budget checks on staged files. It
is the last gate before CI. Run it always, after staging your changes. If it
fails because `eslint-metrics.json` is stale, run `npm run lint:metrics`, stage
the file, and re-run `make pre-commit`.

### 8.7 Missing live verification

Static analysis and unit tests verify structure and logic, but not visual
rendering. Always verify in the browser after deploying. Port-forward the proxy
(`kubectl -n mlops port-forward svc/litellm-proxy 4000:4000`), navigate to the
affected page, and check all state variants (e.g. all 9 usage views, all user
roles, empty vs populated data).

---

## 9. Quick reference: commands

```bash
# Orientation
cd ui/litellm-dashboard

# Find an API wrapper
grep -n "export const <name>" src/components/networking.tsx

# Extract render conditions
node scripts/extract-gating.mjs "src/components/<feature>/**/*.tsx"

# Type-check
npx tsc --noEmit

# Lint a specific directory
npx eslint src/components/<dir>/**/*.tsx

# Run a specific test file
npx vitest run src/components/<dir>/<Component>.test.tsx

# Run all tests
npm run test

# Regenerate API types (after backend endpoint changes)
LITELLM_PYTHON="uv run --no-sync python" npm run gen:api

# Update lint metrics (after reducing violations)
npm run lint:metrics

# Pre-commit gate (run from repo root, after staging)
cd ../..
make pre-commit

# Format all files
cd ui/litellm-dashboard && npm run format

# Dead code detection
npm run knip
```

---

## 10. How this document relates to the siblings

```
FRONTEND-UNDERSTANDING-TECHNIQUE.md   ← you are here (top-level strategy)
    │
    ├── FRONTEND-ANALYSIS-PROCESS.md      (5-layer bug/redundancy/reuse analysis)
    │       Layer 1: gating extraction     → used in Step 4 above
    │       Layer 2: redundancy detection  → used when consolidating
    │       Layer 3: reuse audit            → used in Step 5 above
    │       Layer 4: fix-then-recheck       → used in Step 6 above
    │       Layer 5: live verification      → used in Step 6 above
    │
    ├── LOGIC-MAPPING-TECHNIQUE.md         (4-phase trace/test/build/verify)
    │       Phase 1: trace the call chain   → used in Step 2 above
    │       Phase 2: test with real data    → used for full-stack features
    │       Phase 3: build modular slices   → used when implementing
    │       Phase 4: verify against map     → used in Step 6 above
    │
    └── UI-LINT-AND-CHANGE-PROCESS.md      (lint, type, CI rules)
            Section 1: tooling inventory    → referenced throughout
            Section 2: CI workflows         → referenced in Step 6
            Section 3: pre-commit script    → referenced in Step 6
            Section 5: rules that matter    → referenced in pitfalls
```

Read this document first for orientation. Then consult the siblings for depth on
specific techniques.
