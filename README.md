# harnessctl

`harnessctl` is the versioned execution engine for Loop-first Harness repositories. It centralizes change-scope collection, AI boundary enforcement, verification profiles, approval finalization, and evidence generation so consumer repositories copy configuration and contracts instead of thousands of lines of process code.

## Install

```bash
go install github.com/Fueav/harnessctl/cmd/harnessctl@v0.3.0
```

Consumer repositories pin the same version in `harness/harness.lock`. The CLI refuses to execute when the running version and lock disagree.

## Commands

```bash
harnessctl check boundaries --repo . --base origin/main
harnessctl check spec-registry --repo . --compare-ref origin/main
harnessctl verify change --repo .
harnessctl verify candidate --repo .
harnessctl verify release --repo .
harnessctl approval finalize --repo . [arguments]
harnessctl install-tools --repo .
```

The first release intentionally preserves the proven Python and shell engine behind a Go entrypoint. The engine is embedded in the binary and never written into consumer repositories. Internals can move to native Go incrementally without changing the consumer command contract.

## Consumer Boundary

A consumer keeps only:

- `.ai-boundaries.yml`;
- `harness/harness.lock`, `harness_profiles.json`, and tool versions;
- thin compatibility wrappers such as `scripts/verify_release.sh`;
- repository-specific product specs and gate configuration.

Engine fixes and tests live here once and ship through versioned releases.

## Profile schema v2

Schema v2 adds repository-owned command gates and configured symlink pairs:

```json
{
  "schema_version": 2,
  "custom_gates": {
    "openapi_hash": {"run": "scripts/gates/check_openapi_hash.sh"}
  },
  "symlinks": [
    {"link": "CLAUDE.md", "target": "AGENTS.md"}
  ]
}
```

Custom gate commands are normalized repository-relative paths under `scripts/` or `harness/`. Paths emitted through the line-based configuration protocol must not contain TAB, CR, or LF characters. The gate name is added to a `gate_sets` sequence, and any output files are declared through the existing `gate_artifacts` map. Symlink `link` and `target` values are normalized paths relative to the repository root; the link itself may use the corresponding relative target (for example, `.claude/skills` resolves to `.agents/skills`).

To migrate a v1 consumer, change `schema_version` to `2`, add `custom_gates` and `symlinks` (either may be empty), and update `harness/harness.lock` to `v0.3.0`. Schema v1 remains supported and receives the four v0.1.0 template symlink pairs during loading.

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
