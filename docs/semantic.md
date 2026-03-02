# Semantic Learning Roadmap

This document describes a future direction for the QA explorer.

It does not describe what the tool does today.
It describes how the tool can improve beyond purely heuristic exploration.

## Problem Statement

Today the explorer is adaptive, but mostly heuristic.

It learns from outcomes such as:

- `navigation`
- `modal_open`
- `dom_mutation`
- `no_change`
- `broken`
- `api_error`

That is useful, but limited.

The explorer currently understands:

- this route still has pending links
- this element often does something useful
- this element often does nothing
- this route looks exhausted

It does not truly understand:

- what kind of page this is
- what business object this page represents
- what user intent an element represents
- which pages are discovery hubs
- which repeated routes are semantically redundant

So the current system is good at tactical learning, but weak at strategic learning.

## What "Semantic Learning" Means

Semantic learning means the explorer reasons about meaning, not just outcomes.

Examples:

- `dashboard` is a hub page
- `/project/:id` is a project detail page
- "New Project" is a `create` action
- "Invite Member" is a collaboration-management action
- "Billing" is a different business area than "Content"
- crawling the 20th project detail page adds less value than finding the first billing settings page

This would let the frontier optimize for new business coverage, not just new URLs.

## Why This Matters

Without semantic understanding, the crawler tends to over-value:

- new concrete URLs that map to the same route shape
- repeated detail pages with little strategic value
- surface-level novelty

And it under-values:

- pages that unlock many new branches
- routes that cover entirely new business areas
- actions that represent important workflows

Semantic learning would improve:

- route prioritization
- seed-route selection
- discovery efficiency
- bug triage quality
- workflow authoring assistance

## Current Limitation

Current explorer learning is primarily implemented in:

- `qa_agent/knowledge.py`
- `qa_agent/explorer.py`

The main signals today are:

- route novelty
- visit count
- pending links
- element priority
- element skip score
- click outcome classification

These are useful, but they are not semantic categories.

## Target Semantic Model

The semantic layer should add meaning to both routes and elements.

### Route semantics

Each canonical route should eventually be able to store:

- `page_kind`
  - dashboard
  - list
  - detail
  - editor
  - settings
  - billing
  - auth
  - onboarding
- `entity_kind`
  - project
  - scene
  - asset
  - member
  - invoice
  - organization
- `business_area`
  - content
  - collaboration
  - publishing
  - billing
  - admin
- `hub_score`
  - how likely this page is to expose new links or states
- `semantic_confidence`
  - confidence of the classification

### Element semantics

Each learned element should eventually be able to store:

- `action_kind`
  - create
  - edit
  - delete
  - invite
  - upload
  - submit
  - filter
  - paginate
  - open_detail
  - open_settings
- `target_kind`
  - project
  - member
  - scene
  - asset
  - billing
- `intent_confidence`
- `is_destructive`
- `is_navigation_hub`

## Proposed Data Model Changes

### `RouteKnowledge`

Future fields:

```text
page_kind: str
entity_kind: str
business_area: str
hub_score: float
semantic_confidence: float
semantic_sources: list[str]
```

### `ElementRecord`

Future fields:

```text
action_kind: str
target_kind: str
intent_confidence: float
is_destructive: bool
is_navigation_hub: bool
semantic_sources: list[str]
```

## How Semantic Signals Could Be Derived

This should be done in layers.

### Layer 1: deterministic rules

Cheap and stable signals:

- canonical route pattern
- page title
- headings
- button text
- aria labels
- form labels
- API endpoint names
- menu labels

Examples:

- `/project/:id/settings` -> `page_kind=settings`, `entity_kind=project`
- button text `New Project` -> `action_kind=create`, `target_kind=project`
- endpoint `/invite` -> collaboration-related action

### Layer 2: model-assisted classification

Use the LLM only when deterministic rules are weak or ambiguous.

Examples:

- decide whether a page is editor vs detail
- infer whether a button means publish vs save draft
- infer business area from mixed UI signals

### Layer 3: workflow anchors

Workflows are explicit business truth.

They can anchor semantics:

- a recorded workflow named `create_project` strongly suggests action and page semantics
- a workflow step sequence can label known elements and routes

This is the best bridge between explorer learning and business meaning.

## How Frontier Scoring Would Improve

Today, frontier scoring is mostly:

- new URL good
- pending links good
- exhausted route bad

A better semantic-aware frontier would additionally score:

- unseen `page_kind`
- unseen `entity_kind`
- unseen `business_area`
- hub routes that historically reveal new branches
- actions or routes tied to important workflow categories

And it would penalize:

- repeated detail pages of the same semantic class
- repeated entity instances with low discovery value
- semantically redundant routes

### Example

Today:

- `/project/123`
- `/project/456`
- `/project/789`

may all look somewhat interesting as separate URLs.

With semantics:

- all map to `page_kind=detail`, `entity_kind=project`
- once that class is well covered, the next project detail page should be low-value
- the crawler should prefer a new class like `/billing/settings`

## Seed Routes Should Become Semantic

Current discovery seeds are static:

- `/`
- `/dashboard`
- start route

That is fine as a first step, but eventually seed routes should be selected semantically.

Examples of future semantic seed routes:

- dashboard pages
- admin overviews
- project lists
- settings indexes
- navigation shells

These are better discovery hubs than random detail pages.

## Proposed Module Design

Future addition:

- `qa_agent/semantics.py`

Possible responsibilities:

- `classify_route(page_data) -> RouteSemantic`
- `classify_element(route_context, element_text, selector, action_context) -> ElementSemantic`
- `score_semantic_novelty(route_semantics, kb) -> float`
- `is_semantic_hub(route_semantics) -> bool`

Possible supporting types:

```text
RouteSemantic
ElementSemantic
SemanticCoverage
SemanticCluster
```

## Recommended Implementation Plan

### Phase 1: semantic tags only

Add:

- `page_kind`
- `entity_kind`
- `action_kind`

Do not change frontier behavior yet.

Goal:

- collect semantic data safely
- validate classification quality

### Phase 2: semantic observability

Expose semantics in:

- dashboard Explorer
- Knowledge tab
- report output

Goal:

- make semantic classifications inspectable
- detect bad classification rules early

### Phase 3: semantic-aware frontier scoring

Update frontier scoring to reward:

- unseen page kinds
- unseen business areas
- hub pages

Goal:

- widen meaningful coverage, not just URL coverage

### Phase 4: workflow-assisted semantics

Use workflow definitions and failures to enrich:

- action classification
- business-critical route tagging
- route importance weighting

Goal:

- align discovery with real user journeys

### Phase 5: semantic coverage metrics

Track metrics like:

- page kinds covered
- business areas covered
- action kinds exercised
- semantically redundant routes skipped

Goal:

- measure quality of exploration in business terms

## Guardrails

Semantic learning should not make the explorer unstable.

Rules:

- deterministic rules first
- LLM only for uncertain cases
- semantic tags should augment heuristics, not replace them immediately
- workflows remain explicit and non-mutating
- semantic scoring must be inspectable in dashboard and docs

## Risks

Main risks:

- over-classification with low confidence
- LLM inconsistency across runs
- false semantic certainty
- hidden coupling between semantics and workflows
- making the explorer too clever to debug

Mitigations:

- persist confidence scores
- store semantic source signals
- allow disabling semantic scoring
- surface semantic labels in the dashboard
- keep fallback heuristic behavior

## Success Criteria

This direction is worthwhile only if it improves real outcomes.

Success would look like:

- fewer repeated low-value crawls
- more new business areas discovered per run
- better discovery from hub routes
- better prioritization of meaningful pages
- more actionable reports for developers

## Non-Goals

This semantic roadmap should not become:

- full autonomous testing with no human control
- automatic workflow mutation
- blind LLM click planning
- screenshot-only reasoning without structure

The goal is better prioritization and better coverage, not AI theatrics.

## Practical Recommendation

If this gets implemented, the best first version is:

1. add `page_kind` and `action_kind`
2. classify using deterministic rules first
3. show results in dashboard
4. only then feed semantics into frontier scoring

That keeps the system debuggable while still moving it beyond pure heuristics.
