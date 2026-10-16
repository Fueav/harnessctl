# Workbench v1 contract

This breaking release replaces the profile-specific orchestration engine. Native agent tools remain available; the engine owns execution facts, resource ownership and verification identity, not the model's plan. Old consumers stay pinned to v0.x. Version `v1.0.0-rc.1` has no v0 configuration or CLI fallback.

## Project contract

The only execution descriptor is `harness/project.json` (schema 1). `harness/harness.lock` retains its exact-version lock schema. `.ai-boundaries.yml` remains the authorization authority. Project specs and native commands remain project-owned. Reject unknown fields, invalid types, unsafe paths and simultaneous legacy descriptors.

```json
{
  "schema_version": 1,
  "project": "example",
  "operations": {
    "test": {"argv": ["make", "test"], "timeout_seconds": 600}
  },
  "profiles": {
    "development": {"resources": "none", "lease_seconds": 1800}
  },
  "verification": {
    "change": ["test"], "candidate": ["test"], "release": ["test"]
  },
  "dependencies": {"enabled": false},
  "tools": {}
}
```

Operations are named argv arrays, never implicit shell interpolation. An optional `description` explains user-visible purpose. Operations may declare `env` (non-secret string values only). Additional run arguments are literal argv suffixes. `timeout_seconds` is mandatory and bounded by the session lease. Profiles select `none`, `shared`, or `fresh` resource allocation and a positive bounded `lease_seconds`. `dependencies` embeds the existing validated resource configuration; its internal isolation schema stays unchanged. `tools` maps installer-supported tool names to exact versions. The descriptor contains no fixed role workflow or task plan. Verification lists reference operations; the engine unconditionally enforces change scope and authorization before these commands. Unknown operations/profiles fail before execution.

## Interface

All responses are one JSON object with `schema_version: 1`; process stdout belongs in referenced execution logs, not mixed into protocol stdout. Errors have an actionable `error` and nonzero exit. `--repo` selects a Git worktree and exact lock; session IDs never grant access to another worktree. Execution requires the exact engine binary digest. A later build of the same schema/version may observe or close an already owned session for recovery, but may not execute through it.

| Command | Result |
| --- | --- |
| `describe --repo PATH` | Project, descriptor digest, operations, profiles, requirements and factual availability; no installation or allocation. |
| `open --repo PATH --profile NAME [--goal FILE]` | Session ID, state, expiry, frozen descriptor/goal digests and resource availability; no credentials. |
| `run --repo PATH --session ID --operation NAME [-- ARG...]` | Unique execution ID, running/completed/failed state, exit status when known, log/evidence references and candidate identity. |
| `observe --repo PATH --session ID [--execution ID]` | Current factual state, operation receipts, bounded log tail and cleanup status; never inferred completion. |
| `close --repo PATH --session ID` | Idempotent termination and owned resource cleanup; reports pending failures and returns nonzero until cleanup succeeds. |
| `verify change\|candidate\|release --repo PATH [--profile NAME] [--goal FILE] [--compare-ref REF]` | Ephemeral session using the same execution path, required operation and criterion results, candidate/contract identities, and truthful verification status. |

Default verification profile is `development`. Explicit sessions survive the initiating CLI process and allow several experiments against the same isolated data. They are bounded by lease, capacity and storage limits, and owned by one session supervisor. No global daemon. Concurrent calls either serialize safely or return busy. `close`, expiry and crash recovery stop owned process groups before releasing resources; unknown ownership is never deleted. Native read/edit/Git tools are not wrapped. A verification session always closes, including failure and cancellation. Ordinary checks do not require creating a goal artifact.

## Goals and evidence

An optional goal is a regular JSON file with `schema_version`, `id`, `objective`, and nonempty `criteria`: each has `id`, `description`, and `operation`. An optional `scope` string states the result boundary (for example local API, staging or production). Goal criteria reference project operations. A session freezes goal content and digest; changed goal/descriptor inputs cannot silently alter an existing session. Outcome verification runs each required criterion as well as the engineering profile, without duplicate execution of the same operation.

Each execution receipt binds session/execution ID, exact engine version, project descriptor and goal digests, operation/argv, start/end, actual exit or interruption, candidate Git HEAD plus working-state digest, and output artifacts. Receipts are per execution; there is no mutable global last-result. A failed or interrupted attempt remains visible. Logs redact generated dependency credentials. Receipt directories are private, outside the checkout, and writes are atomic. Receipts are factual provenance, not cryptographic attestation against the same OS user. Closed sessions discard their extracted runtime and retain evidence. Storage admission uses project limits and reports the owned evidence location when capacity is exhausted; v1 does not silently delete retained evidence.

Verification binds its initial candidate and contracts and fails if they change during checks. Candidate/release require a clean checkout and a resolvable compare commit. `change` may verify dirty work but must fingerprint it. A passed result means only that the named checks passed for the bound scope; it is never automatic proof of product success, deployment or independent acceptance. Independent evaluators execute their own frozen acceptance after the actor's candidate is sealed, outside its writable scope. They own the delivery judgment.

Candidate CI may explicitly defer boundary approval. Successful checks then return `status: pending_approval`, `engineering_status: passed`, and `approval_status: pending` with exit 0; this is not a promotable verification result. Release always requires actual authorization. Repository branch protection owns review approval. The obsolete evidence-format-dependent `approval finalize` interface is removed. Native operations receive `HARNESS_PROJECT_ROOT`, `HARNESS_COMPARE_SHA`, `VERIFY_COMPARE_REF` and `HARNESS_ARTIFACT_DIR`, without engine implementation paths.

## Replacement and acceptance

Delete public `resources run/status/gc`, legacy verification runners/profile schemas, the mutable last-result path, and scaffold provenance machinery from the new engine. Preserve reusable resource allocation/ownership/recovery, Git scope, boundary policy and atomic evidence primitives behind the new interface. Keep direct boundary inspection/approval finalization and explicit tool installation only if their implementation remains independent of old profiles. Do not translate old descriptors at runtime.

The release must demonstrate: configuration-only Python and Go consumers; successive commands sharing session data; two sessions isolated; parent CLI exit without premature reclamation; bounded timeout/expiry/close and recovery; rejected unknown input/foreign session; failed operation receipts; clean/dirty candidate and contract change detection; semantic outcome acceptance outside actor writes; actual Docker PostgreSQL/Redis allocation and cleanup. Regression tests for retained safety primitives remain required. Unavailable prerequisites are failures or explicit unverified cases, never passes.
