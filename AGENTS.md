# HARNESSCTL CONTRACT

This repository owns the versioned Harness execution engine. Consumer repositories own configuration and project contracts; they must not vendor this engine.

## Commands

```bash
make build
make test
make lint
```

## Boundaries

- Keep the CLI fail-closed for unknown commands, invalid configuration, unresolved Git identities, and unclassified paths.
- Preserve deterministic change-scope, evidence, and approval semantics across releases.
- Add behavior through tests first. Consumer compatibility needs an integration fixture that contains configuration but no engine source.
- Release tags use semantic versions. Breaking configuration or evidence changes require a schema-version change and migration notes.
- Never add credentials, organization-specific hosts, or consumer application policy to the engine.
- `scripts/` contains centralized engine implementation and engine tests. Consumer repositories receive only configuration, a version lock, and thin wrappers.
