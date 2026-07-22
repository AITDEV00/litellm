# UI Linting and Source Code Change Process

Status: reference.
Date: 2026-07-22.

This documents the existing linting, type-checking, and quality tooling that gates UI changes in `ui/litellm-dashboard/`. Every Tier 2 / Tier 0 change must pass all of these before commit.

---

## 1. Tooling inventory

### 1.1 ESLint (`eslint.config.mjs`)

Flat config. Layers (in order):

1. `@eslint/js` recommended
2. `typescript-eslint` recommended
3. `eslint-config-next/core-web-vitals` (Next.js rules)
4. `eslint-config-prettier` (disables format rules that conflict with Prettier)
5. `eslint-plugin-unused-imports` (errors on unused imports)

Custom rules that matter for new code:

| Rule | Level | Constraint |
|------|-------|-----------|
| `no-restricted-syntax` (raw `fetch()`) | error | `fetch()` is banned everywhere except `src/lib/http/**`. All API calls must go through `apiClient` from `src/lib/http/client.ts` |
| `no-restricted-imports` (`@tremor/react`) | error | Tremor is being phased out. New components must use antd. Existing Tremor usage is grandfathered via `eslint-suppressions.json` |
| `no-console` | warn | `console.warn` and `console.error` are allowed; `console.log` is not |
| `@typescript-eslint/no-explicit-any` | warn | Counts toward the lint budget |
| `complexity` | warn at 20 | Counts toward the lint budget |
| `max-depth` | warn at 4 | Counts toward the lint budget |
| `max-params` | error at 4 | Hard limit |
| `max-nested-callbacks` | error at 4 | Hard limit |
| `unused-imports/no-unused-imports` | error | No unused imports |
| `react/no-danger` | error | No `dangerouslySetInnerHTML` |

The `no-restricted-syntax` override for `src/lib/http/**` means the shared HTTP client at `src/lib/http/client.ts` is the only file that may call `fetch()` directly.

### 1.2 Prettier (`.prettierrc`)

```json
{
  "semi": true,
  "singleQuote": false,
  "tabWidth": 2,
  "printWidth": 120,
  "trailingComma": "all"
}
```

`.prettierignore` excludes `node_modules`, `.next`, `coverage/`, `eslint-suppressions.json`, and `src/lib/http/schema.d.ts`.

### 1.3 TypeScript (`tsconfig.json`)

- `strict: true`
- Path alias: `@/*` maps to `./src/*`
- `noEmit: true` (type-check only; Next.js handles compilation)
- `skipLibCheck: true`

### 1.4 Lint budgets (`eslint-budgets.json` + `eslint-metrics.json`)

A ratcheting budget system that caps the total count of specific lint violations across the entire `src/` tree. If any rule exceeds its `max`, the build fails.

| Rule | Current Max | Target |
|------|-------------|--------|
| `@typescript-eslint/no-explicit-any` | 2040 | 1500 |
| `no-console` | 484 | 0 |
| `complexity` | 140 | 80 |
| `max-depth` | 70 | 30 |

`scripts/check-lint-budgets.mjs` runs ESLint on the whole project, counts violations per rule, and fails if any count exceeds its `max`. It also checks `eslint-metrics.json` for drift: the committed metrics file must match the actual violation counts. If you reduced violations, run `npm run lint:metrics` to regenerate the metrics file and commit it so the ceiling ratchets down.

### 1.5 ESLint suppressions (`eslint-suppressions.json`)

Legacy files that violate the `no-restricted-imports` rule (Tremor imports) or `unused-imports` rule have suppression entries here. These are pre-existing violations that are accepted but tracked. New files should not add suppressions unless there is a deliberate reason (e.g., the Tier 1 `ModelAnalyticsView.tsx` uses Tremor to match the surrounding Usage page components).

Run `npx eslint --suppress-all <file>` to generate suppression entries for a new file that intentionally violates a rule.

### 1.6 Knip (`knip.json`)

Detects unused exports, files, and dependencies. Run via `npm run knip` or `npm run knip:fix`. Not gating in CI but useful for catching dead code.

### 1.7 Vitest (`vitest.config.ts`)

- Environment: jsdom
- Globals: enabled
- Test files: `src/**/*.test.ts(x)` and `tests/**/*.test.ts(x)`
- Coverage: v8 provider, includes `src/**/*.{ts,tsx}`
- Timeout: 30s per test
- Setup file: `tests/setupTests.ts`

### 1.8 Playwright E2E (`e2e_tests/`)

- Config: `e2e_tests/playwright.config.ts`
- Base URL: `http://localhost:4000`
- Run via `npm run e2e` or `npm run e2e:ui`
- Migration suite: `npm run e2e:migration`

### 1.9 API type generation (`scripts/gen-api-types.mjs`)

Regenerates `src/lib/http/schema.d.ts` from the proxy's OpenAPI spec. Run via `npm run gen:api`. This file is auto-generated and must not be hand-edited. CI checks for drift: if a backend endpoint changes without regenerating the types, `check-ui-api-types.yml` fails.

### 1.10 Shared HTTP client (`src/lib/http/client.ts`)

The `apiClient` is the single entry point for all dashboard API calls. It handles:

- Base URL resolution (from `getProxyBaseUrl()`)
- Auth header injection (configurable header name)
- Query param serialization
- Error parsing (`deriveErrorMessage`)
- Response deserialization

Created via `createApiClient()` with injectable config. Framework-agnostic (no React imports) so it can run in both client and server components.

---

## 2. CI workflows that gate UI PRs

### 2.1 `test-litellm-ui-build.yml` (two jobs)

**`build-ui`**: `npm ci` + `npm run build`. This runs the full Next.js production build, which includes TypeScript type-checking. If types don't compile, this fails.

**`frontend-lint`**: Runs only on changed files (diff-scoped against the PR base SHA). Three steps:
1. `npx prettier --check` on changed files (`.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs`, `.json`, `.css`, `.scss`, `.md`, `.mdx`, `.yml`, `.yaml`, `.html`)
2. `npx eslint` on changed files (`.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs`)
3. Lint budget check on the whole project (not diff-scoped)

### 2.2 `check-ui-api-types.yml`

Triggers on changes to `litellm/proxy/**`, `litellm/types/**`, or the type generation files. Regenerates `schema.d.ts` from the live proxy OpenAPI spec and fails if the committed file is stale.

---

## 3. Pre-commit script (`scripts/pre_commit_lint.sh`)

Run via `make pre-commit` from the repo root. It inspects staged files (`git diff --cached`) and runs only the relevant checks:

- **Dashboard files staged** (`ui/litellm-dashboard/**/*.{js,jsx,ts,tsx,mjs,cjs,json,css,...}`):
  1. `npx prettier --check` on staged files
  2. `npx eslint` on staged files
  3. Full-project lint budget check (counts + drift detection)
- **`litellm/proxy/` or `litellm/types/` staged**: Regenerates `schema.d.ts` and fails if it drifted
- **`litellm/**/*.py` staged**: Runs `make lint` (Python ruff + type checks)

Not auto-installed as a git hook. Run it manually before committing. It also warns about unstaged/untracked changes that could cause the local result to differ from CI.

---

## 4. Required workflow before committing UI changes

```bash
cd ui/litellm-dashboard

npm run format          # prettier --write . (formats all files)
npm run lint            # eslint . (lints all files)
npm run test            # vitest (runs all unit tests)
npm run lint:metrics    # regenerate eslint-metrics.json if you reduced violations

cd ../..
make pre-commit         # runs prettier + eslint + budget check on staged files only
```

If `make pre-commit` fails:
- Prettier failures: run `cd ui/litellm-dashboard && npm run format`, then re-stage
- ESLint failures: fix the errors, then re-run `make pre-commit`
- Budget failures: if you reduced violations, run `npm run lint:metrics` and commit the updated `eslint-metrics.json`
- API type drift: run `npm run gen:api` and commit the updated `schema.d.ts`

---

## 5. Rules that matter most for Tier 2 / Tier 0

### 5.1 No raw `fetch()`

All new API wrappers in `networking.tsx` must use `apiClient.get()` / `apiClient.post()` etc. The Tier 1 wrappers already follow this pattern. Example:

```typescript
export const modelConcurrentRequestsCall = async (
  accessToken: string,
  params: MetricsQueryParams = {},
): Promise<ConcurrentRequestsResponse> => {
  return apiClient.get<ConcurrentRequestsResponse>("/model/metrics/concurrent_requests", {
    accessToken,
    query: buildMetricsQuery(params),
  });
};
```

### 5.2 No new `@tremor/react` imports

New components should use antd for layout and charts. The Tier 1 `ModelAnalyticsView.tsx` uses Tremor (grandfathered via suppression) to match the existing Usage page, but new Tier 2 components should use antd. If Tremor is unavoidable for visual consistency within the same page, add a suppression via `npx eslint --suppress-all <file>`.

### 5.3 TypeScript strict

No `any` types. The lint budget tracks `@typescript-eslint/no-explicit-any` usage; adding new `any` types pushes toward the ceiling. If you need to type an untyped API response, validate it with a schema or `TypeAdapter` in the caller and pass the typed variable in. This matches the Python-side convention in `CLAUDE.md`.

### 5.4 Lint budgets ratchet down

If you fix violations (e.g., remove `console.log` calls or reduce `any` usage), run `npm run lint:metrics` and commit the lowered `eslint-metrics.json`. The budget ceilings ratchet down; leaving stale headroom is not allowed.

### 5.5 API types must be in sync

If Tier 2 / Tier 0 adds new backend endpoints, `schema.d.ts` must be regenerated. Run:

```bash
cd ui/litellm-dashboard
LITELLM_PYTHON="uv run --no-sync python" npm run gen:api
```

Commit the updated `schema.d.ts` alongside the backend changes.

### 5.6 Test coverage

New components must have colocated `.test.tsx` files. Tests should verify meaningful behavior (not just "it renders"). The Tier 1 tests at `ModelAnalyticsView.test.tsx` are the reference: they check the `enabled` guard, the error flag, the `% Slow` computation, and the empty state. Follow the same pattern: mock the networking calls, render with `QueryClientProvider`, assert on user-visible output.
