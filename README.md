# harnessctl

`harnessctl` is the versioned execution engine for Loop-first Harness repositories. It centralizes change-scope collection, AI boundary enforcement, verification profiles, approval finalization, and evidence generation so consumer repositories copy configuration and contracts instead of thousands of lines of process code.

## Install

```bash
go install github.com/Fueav/harnessctl/cmd/harnessctl@v0.5.0
```

`resources run|status|gc` provides owned test services shared across Git worktrees, per-command data and bounded cleanup. Consumers declare `harness/dependencies.json`; see [the resource lifecycle contract](docs/test-resources.md).

Consumer repositories pin the same version in `harness/harness.lock`. The CLI refuses to execute when the running version and lock disagree.

## Commands

```bash
harnessctl check boundaries --repo . --base origin/main
harnessctl check spec-registry --repo . --compare-ref origin/main
harnessctl verify change --repo .
harnessctl verify candidate --repo .
harnessctl verify release --repo .
harnessctl evidence verify --repo . --evidence-dir /path/to/candidate-evidence
harnessctl scaffold audit --template /path/to/clean-template --repo .
harnessctl scaffold record --template /path/to/clean-template --repo . \
  --resolution AGENTS.md=merged
harnessctl approval finalize --repo . [arguments]
harnessctl install-tools --repo .
```

The release intentionally preserves the proven Python and shell engine behind a Go entrypoint. The engine is embedded in the binary and never written into consumer repositories. This is accepted implementation debt, not a migration target by itself. Move an internal component to Go only when reproducible security, distribution, reliability, or maintenance evidence justifies the change; preserve the CLI, lock, and evidence contracts and prove equivalence with the existing checker and integration suites.

## Consumer Boundary

A consumer keeps only:

- `.ai-boundaries.yml`;
- `harness/harness.lock`, `harness_profiles.json`, and tool versions;
- thin compatibility wrappers such as `scripts/verify_release.sh`;
- repository-specific product specs and gate configuration.

Engine fixes and tests live here once and ship through versioned releases.

## Profile schema v3

Schema v3 keeps the v2 custom-gate and symlink contracts, allows custom gates to be selected by changed paths, and reduces verification to `change`, `pull_request`, and `release`. A nightly job invokes the `release` profile; it is a trigger, not a separate policy profile.

```json
{
  "schema_version": 3,
  "custom_gates": {
    "openapi_hash": {"run": "scripts/gates/check_openapi_hash.sh"}
  },
  "conditional_gates": {
    "openapi_hash": {
      "always_profiles": ["release"],
      "path_prefixes": ["api/", "openapi/"],
      "skip_reason": "no API contract paths changed"
    }
  },
  "symlinks": [
    {"link": "CLAUDE.md", "target": "AGENTS.md"}
  ]
}
```

Custom gate commands are normalized repository-relative paths under `scripts/` or `harness/`. Paths emitted through the line-based configuration protocol must not contain TAB, CR, or LF characters. Add the gate name to a `gate_sets` sequence and declare outputs through `gate_artifacts`. A schema v3 custom gate may also appear in `conditional_gates`; the evidence ledger then records it independently as passed or skipped. Symlink `link` and `target` values are normalized paths relative to the repository root.

Schemas v1 and v2 remain readable for existing consumers. To migrate v2 to v3, remove the `nightly` profile, remove `nightly` from every `always_profiles` list, declare any path-selected custom gates, and update `harness/harness.lock` to v0.5.0.

## Evidence reuse

`harnessctl evidence verify` revalidates an existing candidate or release evidence directory against the current clean checkout. Reuse succeeds only when the engine, profile policy and other verifier inputs, HEAD and tree, compare SHA, merge base, gate decisions, artifact manifest, and seals still match. The command never edits the evidence directory or target repository. This permits a verified commit to move between worktrees or fast-forward branches without repeating the full release suite.

## Scaffold convergence

`harnessctl scaffold record` renders a compact deterministic `harness/scaffold.lock` to stdout and never edits the target. Identical semantic paths are recorded automatically; unchanged prior resolutions are reused; every changed manual merge or project overlay requires an explicit `--resolution path=merged|preserved|adapted|relocated` decision.

`harnessctl scaffold audit` then compares the clean template checkout with the target using `harness/scaffold_manifest.json`. Exact copies and symlinks are checked mechanically; retired paths must be absent; manual merges and project overlays must match the lock. The lock also pins the template commit and manifest digest. Audit is read-only, emits deterministic JSON, returns 0 for convergence, 1 for drift, and 2 for invalid input.

## Workflow registry compatibility

The `spec-registry` gate accepts the legacy version 2 workflow manifest and the version 3 compact class list. Version 2 preserves compatibility with existing consumers; version 3 contains only the four workflow IDs so the repository's workflow document can remain the sole semantic authority.

## Member-driven built-in gates

Built-in gates are member-driven: a built-in runs only when its name appears in the profile's `gate_set`. An omitted gate is neither executed nor recorded as skipped, and it contributes no gate-owned artifact. This makes methodology gates such as `spec_registry` optional; repositories with a different Specification contract can replace it with a custom gate.

Every `gate_set` must retain the non-replaceable safety core `change_scope` and `ai_boundaries`. A gate set used by a profile whose evidence mode is `candidate` or `release` must also contain `release_context_before` and `release_context_after`. Built-ins must preserve the engine's global execution order, and `coverage_threshold` requires `test_unit_coverage`. These constraints are validated when the policy loads; schema remains version 2.

## AI boundary patterns

`.ai-boundaries.yml` accepts exact repository-relative files, directory prefixes ending in `/`, and one restricted basename glob form. A basename glob contains `*`, contains at least one non-`*` character, and contains no `/`; examples are `*.pem` and `*secret*`. It is matched case-sensitively against the final path component at any directory depth.

The parser rejects `?`, bracket expressions such as `[ab]`, path-level globs such as `secrets/*.pem`, and catch-all `*` or `**`. When more than one rule matches, the strictest classification still wins: `forbidden` over `approval_required` over `allowed`.

## Custom gate runtime contract

Custom gates run serially from the consumer repository root with the same recorded-run lifecycle as built-in gates. Combined stdout and stderr are written to `logs/<gate-name>.log`; exit status 0 passes, while any non-zero status fails the gate and immediately ends the verification run. Before execution, the runner requires the command to be a regular, non-symlink executable file.

The runner provides these variables:

- `HARNESS_PROJECT_ROOT`
- `HARNESS_ARTIFACT_DIR`
- `HARNESS_SNAPSHOT_FILE`
- `HARNESS_SNAPSHOT_SHA256`
- `HARNESS_COMPARE_SHA`
- `HARNESS_HEAD_SHA`
- `HARNESS_PROFILE`
- `HARNESS_EVIDENCE_MODE`

`HARNESS_ENGINE_DIR` is deliberately removed from the gate environment so consumer code cannot depend on engine internals. A gate writes declared outputs beneath `$HARNESS_ARTIFACT_DIR`; after the gate passes, the runner seals every path declared for it in `gate_artifacts`. Gates must not modify the working tree; release verification detects such changes in `release_context_after`.
