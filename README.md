# harnessctl

A project workbench for an agent that owns a product outcome. The project declares native commands; the engine owns bounded execution, resource cleanup, authorization checks and factual receipts. It does not prescribe a planning sequence or decide that passing checks means the product is complete.

`v1.0.0-rc.1` is a breaking replacement. See [the interface contract](docs/workbench-v1.md) and [migration](docs/v1-migration.md).

```sh
harnessctl describe --repo .
harnessctl open --repo . --profile development
harnessctl run --repo . --session SESSION_ID --operation test
harnessctl observe --repo . --session SESSION_ID
harnessctl close --repo . --session SESSION_ID
harnessctl verify change --repo . --compare-ref origin/main
harnessctl verify candidate --repo . --compare-ref origin/main --goal outcome.json --output .artifacts/verification
```

The only execution descriptor is `harness/project.json`; `harness/harness.lock` pins the engine. Consumers keep their own code, product contracts and native checks, without copies of engine scripts. Operations use literal argv arrays. Generated dependency credentials are injected into operation environments and redacted from logs.

Sessions survive the initiating CLI and expire at a bounded lease. PostgreSQL databases, Redis databases/ACLs and registered process groups belong to the session. Several operations share session data; other sessions receive isolated data. `close` is idempotent and reports incomplete cleanup as a failure. Crash recovery runs when a session is observed, closed, or when admitting new work. There is no global daemon.

Private execution state defaults to `$XDG_STATE_HOME/harnessctl` or `~/.local/state/harnessctl`. Each attempt retains its own logs and receipt, including failures. Receipts bind the exact executable digest, Git candidate, descriptor and optional goal. `observe` and `close` can inspect and reclaim an owned session from another build of the same version; executing new work requires the exact build which opened it. The runtime copy is removed on successful close.

State admission obeys dependency storage/free-space limits. Output is capped at 8 MiB per stream and 256 executions per session. Closed evidence is retained; admission stops with its directory and measured budget when full. This first version does not automatically archive evidence. Receipts establish local provenance, not tamper-proof attestation against the same OS user.

`verify --output DIR` exports a self-contained bundle with relative references: public verification and boundary receipts, execution receipts, and redacted stdout/stderr. The directory must be new and either outside the worktree or Git-ignored. Private session/resource state and native artifacts are never copied. Failed verification attempts export too; an existing bundle is never overwritten.

Verification always checks Git scope and authorization before project commands or resource allocation. Candidate and release require a clean worktree and comparison commit. Verification detects persistent candidate or contract changes during checks. An explicitly deferred candidate approval produces `pending_approval`, never `passed`; release requires approval. Outcome criteria execute through the same operation path. Independent acceptance remains outside the actor's writable checkout.

```sh
make build
make test
make lint
make integration # actual local Docker PostgreSQL/Redis acceptance
make sandbox-integration # macOS + Codex CLI required; fails if unavailable
```

The portable suite (`make test` and `make lint`) runs on Linux/macOS without Codex or Docker. Resource tests cover ownership, storage and safe deletion; configuration-only consumers cover execution, receipts and approval semantics. Separate Docker acceptance proves isolation and recovery. The macOS Codex sandbox suite executes cross-call sessions, cancellation, timeout and supervisor-crash cleanup under read-only defaults with only the fixture writable; it is a required local release check, not claimed by Linux CI.
