# v1 migration

This is a source migration followed by one cutover. Existing consumers can remain pinned to v0; v1 does not interpret their configuration or commands.

1. Inventory the target's actual checks, product acceptance, environment requirements and ownership policy. Record a native baseline before changing execution.
2. Express those native commands in `harness/project.json` schema 1. Embed the validated dependency configuration and exact tool versions. Keep `.ai-boundaries.yml` and project-owned acceptance unchanged unless the owner explicitly changes that contract.
3. Pin `harness/harness.lock` to the exact v1 engine. Validate the descriptor and native commands in an isolated checkout.
4. Delete legacy profiles, separate dependency/tool manifests, scaffold provenance, copied generic engines, verification/evidence/approval-finalization wrappers and their workflow gates. CI invokes `harnessctl verify`; native checks remain project-owned. There is no runtime translation or fallback.
5. Validate actual resource use, isolation, interrupted execution, cleanup and a real product outcome. Use independent acceptance on the sealed candidate and retain failed attempts.

The former `resources run/status/gc`, `scaffold`, `evidence verify`, `approval finalize` and `workspace-preflight` commands are removed. The old mutable `last-result.json`, profile gate engine and scaffold inventory are removed. Boundary inspection, scope collection and explicit tool installation remain small independent capabilities. A review or branch-protection decision remains separate from execution facts.
