# Open Web QA Agent

QA crawler for authenticated web apps with:

- cross-run self-learning exploration
- workflow regression checks
- live dashboard
- HTML report for developers
- raw JSON evidence for deeper debugging

This tool has 2 separate responsibilities:

1. Explorer mode learns how to cover more of the app over time.
2. Workflow mode checks whether known business flows still work.

Those 2 systems should stay separate. The explorer is for breadth and discovery. Workflows are for regression checking.

---

## Open Source Hygiene

This repository is structured so you can publish the source without leaking local test data:

- Copy `.env.example` to `.env` for local secrets and machine-specific overrides.
- Keep real credentials, saved sessions, crawl artifacts, and screenshots out of version control.
- Use `sites/example-site/` as the safe template when creating a new public site profile.
- Treat `site.json` as non-secret configuration and `.env` as secret local state.

---

## Quick Setup

### 1. Install Ollama + model
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5
# ollama pull qwen2.5:14b
ollama serve
```

### 2. Install Python deps
```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure `.env`
Start from the checked-in example:

```bash
cp .env.example .env
# PowerShell:
# Copy-Item .env.example .env
```

Then update the secret values in `.env`:

- `QA_SITE_ID`
- `QA_EMAIL`
- `QA_PASSWORD`
- `QA_LLM_*` or `OLLAMA_*` if you use AI analysis

The recommended pattern is:

- keep non-secret site settings in `sites/<site_id>/site.json`
- keep secrets and local-only overrides in `.env`

### 4. Create a site profile

Use the sample profile as your starting point:

```bash
cp -r sites/example-site sites/my-site
# PowerShell:
# Copy-Item sites/example-site sites/my-site -Recurse
```

Edit `sites/my-site/site.json` and update at least:

- `site_id`
- `site_name`
- `base_url`
- `crawl.start_path`
- `routes.discovery_seed_routes`
- `routes.public_routes`

Auth is site-configurable. For sites that do not use `/dashboard` or `/login`, set these in the site profile or `.env`:

- `QA_START_PATH` for the first authenticated landing page to verify and seed
- `QA_LOGIN_URL` for one exact login URL
- `QA_LOGIN_PATHS` for a comma-separated fallback list of login paths to try
- `QA_AUTH_SUCCESS_PATHS` if a successful login lands outside the default `/dashboard,/project`
- `QA_AUTH_BLOCKING_PATHS` if your auth pages use different path markers than `/auth/`, `/login`, `/signin`, `/verify`

For an OpenAI-compatible cloud endpoint later, switch only the LLM env vars:

```bash
QA_LLM_PROVIDER=openai_compatible
QA_LLM_URL=https://api.openai.com/v1/chat/completions
QA_LLM_API_KEY=your_api_key
QA_LLM_MODEL=gpt-4.1-mini
QA_LLM_REPORT_MODEL=gpt-4.1
```

### 5. Run the crawler
```bash
python agent.py
python agent.py --site example-site
python agent.py --site my-site
```

### 6. Open the dashboard
```text
http://localhost:8766/dashboard.html
```

### 7. Record or update a workflow
```bash
python agent.py --site example-site --record-workflow
```

This opens a visible browser session, waits for you to move to the correct starting page, then records your clicks and field changes into the current site's `workflows.json`.

### 8. Site profile layout

Each site now gets its own workspace under `sites/<site_id>/`.

Example:

```text
sites/
  example-site/
    site.json
    workflows.json
    session.json
    crawl_state.json
    crawl_log.json
    history.json
    report.html
    qa_knowledge.json
    screenshots/
```

`site.json` is the profile entry point for that website. It stores non-secret configuration and links to the site-specific artifacts. Secrets such as passwords should still come from `.env` or your local environment, not from committed JSON files.

Only the sample `sites/example-site/` profile is intended to be committed. Real site workspaces should stay local.

`QA_DISCOVERY_SEED_ROUTES` uses canonical route-family matching.

`QA_PUBLIC_ROUTES` is the separate pre-auth/public crawl seed list. Those routes are opened in a fresh browser context with no saved session before the authenticated crawl begins.

Examples:

- `/project-details` matches `/project-details/:id` and deeper routes such as `/project-details/:id/task`
- `/project-details/:id` matches only that canonical detail route and its deeper children
- `/` only matches the homepage

The crawler now has two phases:

- `public`
  - fresh browser context with no saved session
  - used for landing page, login, sign-up, forgot-password, and other public interactions
- `authenticated`
  - saved session or manual login flow
  - used for in-app discovery plus workflow regression checks

`--resume` skips the public phase and resumes the authenticated crawl state only.

The page explorer is now state-aware as well as route-aware.

- `QA_PAGE_STATE_DEPTH` controls how many nested same-page states can be explored
- `QA_MAX_STATE_ACTIONS` caps interactions per page state
- `QA_MAX_PAGE_STATES` caps how many distinct states are explored on one page
- `QA_MAX_FORM_FIELDS` caps safe form fields explored per state

---

## Documentation Map

Use the docs in this order:

- `README.md`
  - quick setup
  - mental model
  - output files
  - how to use explorer vs workflows
- `docs/architecture.md`
  - end-to-end system architecture
  - who calculates score, cost, novelty, retries, and report output
  - how new paths are discovered and re-injected into future runs
  - Mermaid diagrams for crawl flow, workflow flow, dashboard flow, and reporting
- `docs/semantic.md`
  - future roadmap for semantic explorer learning
  - why the current explorer is heuristic
  - how route and element semantics could improve discovery quality
- `dashboard.html`
  - live observability surface for the current or last run
- `report.html`
  - developer-facing QA output for triage and fixing issues

If your question is "who calculates this value?" or "how does the crawler decide what to do next?", start with `docs/architecture.md`.

---

## Community Files

This repository includes the standard open-source project files expected by Git hosting platforms:

- `LICENSE`
- `.github/CONTRIBUTING.md`
- `.github/CODE_OF_CONDUCT.md`
- `.github/SECURITY.md`
- `.github/ISSUE_TEMPLATE/`
- `.github/pull_request_template.md`

---

## Mental Model

### 1. Self-learning explorer

The explorer is the adaptive part of the system.

It tries to answer:

- What routes have we already covered?
- Which elements are useful to click?
- Which elements are boring and should be skipped next run?
- Which routes still have unknown territory?
- Which discovered links were never crawled yet?

This learning is persisted in `qa_knowledge.json`.

### 2. Workflow regression checks

Workflows are not self-learning.

They exist to answer:

- Can a specific business flow still complete?
- Did a known critical path start failing?
- Which exact step broke?

These are stored in `workflows.json`.

The crawler should not silently rewrite workflows by itself. Workflow definitions should stay explicit so a failure means something stable and actionable.

---

## How Self-Learning Works

The self-learning loop lives in the explorer and knowledge base, not in `workflows.json`.

### Route learning

For each canonical route, the tool stores:

- visit count
- average score
- discovered links
- unvisited links still pending
- element count history
- exhausted/not exhausted status
- novelty score

That route memory is used to score the crawl frontier for future runs.

High novelty routes are explored earlier.
Exhausted routes are deprioritized.
Routes that still expose uncrawled links are kept alive.

### Element learning

For each clickable element on a route, the tool stores:

- text label
- selector
- outcomes over time
- `priority`
- `skip_score`
- discovered URLs
- whether it caused breakage

The main behavior is:

- elements that cause navigation, open modals, or create DOM changes gain priority
- elements that repeatedly do nothing accumulate `skip_score`
- elements that break the app are still remembered and retried as important failures, not treated as boring
- brand new elements are tried before known elements

### What improves across runs

Each run can make the next run better by:

- injecting previously discovered but uncrawled links back into the crawl frontier
- skipping boring elements that repeatedly produce no change
- trying historically useful elements earlier
- identifying routes that are still changing vs routes that are already well understood

### What does not self-learn today

- `workflows.json` does not expand itself
- workflow steps are not auto-generated from crawl history
- the LLM analysis does not change crawl decisions directly
- the explorer learns from heuristics and history, not semantic understanding of business intent

So the system is adaptive, but it is not autonomous workflow authoring.

---

## Why Explorer And Workflows Stay Separate

This separation matters.

### Explorer

Explorer is broad and opportunistic:

- clicks many elements
- tries to discover new pages and UI states
- measures what changed
- improves route and element coverage over time

Explorer is good for:

- finding unknown routes
- surfacing broken interactions
- discovering hidden UI branches
- improving future crawl efficiency

### Workflows

Workflows are narrow and intentional:

- start from a chosen page
- execute a known business path
- fail if a required step fails
- report whether the path still works

Workflows are good for:

- release gating
- regression detection
- checking specific user journeys
- tracking pass/fail for critical business flows

If workflows start self-mutating, they stop being reliable regression tests. That is why they stay explicit.

---

## What The Explorer Tab Is For

The Explorer tab in the dashboard is mainly a view of the knowledge base and crawl frontier.

It shows:

- how much the system already knows
- how many elements will be skipped next run
- which routes are exhausted
- which routes still look novel
- pending links discovered from earlier runs
- per-page discovery stats for the current run

The Explorer tab itself does not improve future runs.

What improves future runs is the data behind it:

- `qa_knowledge.json`
- route novelty scoring
- remembered unvisited links
- element `priority`
- element `skip_score`

The tab is a debugging and observability surface for the self-learning system.

---

## Output Files

| File | Purpose |
|---|---|
| `report.html` | Main developer-facing QA report |
| `crawl_log.json` | Raw per-page crawl output with evidence |
| `history.json` | Run-over-run trend summary |
| `crawl_state.json` | Resume snapshot for interrupted crawls |
| `qa_knowledge.json` | Cross-run learning memory for explorer mode |
| `workflows.json` | Explicit workflow definitions |
| `qa_data.js` | Dashboard sidecar so the dashboard can load without the agent running |
| `screenshots/` | Per-page screenshot evidence |
| `session.json` | Saved auth session for browser reuse |

---

## What The Report Gives A Developer

This is a QA tool, so the report should help a developer fix issues, not just say "something is wrong".

The main report is `report.html`.

It contains:

### 1. Run-level KPIs

- average health score
- bug count
- broken interactions count
- API failure count
- slow page count
- workflow completion coverage

This answers: how bad is the run overall?

### 2. Executive summary

A short plain-English summary of what went wrong and what to fix first.

This answers: where should the team start?

### 3. AI handoff

An evidence-based handoff generated from:

- route focus areas
- API failures
- broken elements
- workflow regressions
- coverage and run deltas

It is designed to answer:

- what is the main problem?
- what evidence supports it?
- what is the likely fix direction?
- is the build shippable, cautionary, or blocked?

This is powered by the same LLM layer used for page analysis, with a fallback heuristic summary if the model is unavailable.

### 4. Evidence graphs

The report now includes:

- run score trend across recent runs
- defect trend across recent runs
- highest-risk routes in the current run
- exploration depth by route using states and actions observed

This helps QA and developers understand whether the run is getting better, getting worse, or simply covering different parts of the app.

### 5. Dev Focus Areas

Grouped by route and sorted worst-first.

For each route, the report surfaces:

- what to fix
- approximate effort
- broken interactions
- top JS/runtime bugs
- top API problems
- performance issues
- UX issues
- visual issues

This is the primary triage section for developers.

### 6. Route tree

A route-oriented view of the application showing where issues cluster.

This helps developers understand whether failures are isolated or systemic.

### 7. Pages table

For each page, the report includes:

- URL
- score
- load time
- HTTP status
- screenshot
- broken interactions count
- summary from analysis

This gives concrete page-level evidence.

### 8. Broken elements

For element-level failures captured during exploration, the report now includes:

- route
- action label
- scope or section
- state label
- failure outcome
- evidence message
- target route

This gives developers direct clues about which control failed and where it lives.

### 9. API table

Grouped by endpoint and method with:

- call volume
- failure rate
- average latency

This helps backend or full-stack developers quickly find unstable endpoints.

### 10. Interaction reliability

For actions observed during exploration, the report tracks:

- attempts
- reliability
- flakiness

This helps identify buttons and controls that are unstable, not just broken once.

### 11. Workflow results

Each workflow reports:

- workflow name
- route
- pass/fail
- failing step
- duration

This is the regression-check layer, not the self-learning layer.

---

## What A Developer Should Use To Fix Issues

Use the outputs in this order:

### First: `report.html`

Use it to identify:

- the worst routes
- broken interactions
- failing APIs
- slow pages
- workflow regressions

### Second: `crawl_log.json`

Use it when the report summary is not enough.

`crawl_log.json` contains the raw evidence per page, including:

- console errors
- request failures
- HTTP errors
- interaction outcomes
- API call telemetry
- DOM mutation data
- screenshots
- analysis output

This is the best artifact for reproducing and debugging a specific page issue.

### Third: `screenshots/`

Use screenshots for:

- visual regressions
- missing content
- rendering failures
- broken modal states

### Fourth: `history.json`

Use this to answer:

- Did the run get better or worse?
- Did bug count rise?
- Did latency regress?
- Did route coverage shrink or grow?

### Fifth: workflow failures

If a workflow fails, inspect:

- the failing step
- the workflow route
- the matching page data in `crawl_log.json`
- the relevant screenshot

Workflow failures should be treated as high-confidence regressions.

---

## What "Outcome Of Run" Means

A completed run is not the same as a healthy product.

The tool can finish successfully while still reporting severe failures.

The actual run outcome should be interpreted using:

- health score trend
- total bug count
- broken interactions
- API failures
- workflow pass/fail
- route coverage
- explorer novelty and pending links

### Good run

A good run usually means:

- no critical workflow failures
- few or no broken interactions
- stable API layer
- acceptable performance
- no major visual or UX regressions

### Bad run

A bad run usually means:

- workflow failures on critical paths
- many broken interactions
- API failure clusters
- multiple poor-scoring routes
- lots of regressions compared with `history.json`

### Incomplete knowledge

A run can also be "clean but incomplete".

That usually means:

- low route coverage
- many pending links in explorer data
- many high-novelty routes left
- too few pages visited for the app size

In that case the right action is not only to fix bugs, but also to improve coverage.

---

## Workflow Recording

Use `python agent.py --record-workflow` when you want to add or update a workflow.

The recorder will:

1. Open a visible Chromium session.
2. Reuse the saved auth session or ask you to log in.
3. Wait for you to navigate to the correct workflow start page.
4. Capture clicks, typing, selects, and file input changes.
5. Save or update a named workflow in `workflows.json`.

Workflows should represent intentional user journeys such as:

- create project
- invite member
- upload asset
- publish flow
- checkout flow

They are meant to detect breakage in known business-critical paths.

They are not meant to replace the self-learning explorer.

---

## Current Limits

The current QA system is strong at exploration and evidence gathering, but there are limits:

- workflow definitions are still explicit, not learned
- element scoring is heuristic, not semantic
- skipped elements are determined by repeated no-change outcomes
- LLM analysis helps summarize issues, but does not implement fixes
- the report helps developers fix issues, but it does not automatically produce code patches

That is acceptable for a QA tool. The goal is evidence, prioritization, and developer guidance.

---

## Recommended Use

### For exploratory QA
```bash
python agent.py
```

Use this to widen coverage, discover new routes, and improve the knowledge base.

### For workflow regression QA
```bash
python agent.py --record-workflow
python agent.py
```

Use this when you need repeatable checks for critical flows.

### For a fresh run with no old learning

Delete these files before rerunning:

- `crawl_state.json`
- `history.json`
- `qa_knowledge.json`
- `qa_data.js`

Do not delete `workflows.json` unless you intentionally want to reset workflow definitions.

---

## Notes

- The agent uses a visible browser because login and some workflows require manual confirmation.
- Dynamic workflow routes like `/project/:id` are resolved from real URLs discovered during the crawl.
- If `workflows.json` does not exist, default workflows are bootstrapped automatically.
- Explorer learning is persisted across runs in `qa_knowledge.json`.

---

## License

This project is licensed under the MIT License. See `LICENSE`.

## Contributing And Security

- Contribution guide: `.github/CONTRIBUTING.md`
- Security policy: `.github/SECURITY.md`
- Community standards: `.github/CODE_OF_CONDUCT.md`
