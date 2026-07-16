# harnessctl

`harnessctl` is the versioned execution engine for Loop-first Harness repositories. It centralizes change-scope collection, AI boundary enforcement, verification profiles, approval finalization, and evidence generation so consumer repositories copy configuration and contracts instead of thousands of lines of process code.

## Install

```bash
go install github.com/Fueav/harnessctl/cmd/harnessctl@v0.1.0
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
