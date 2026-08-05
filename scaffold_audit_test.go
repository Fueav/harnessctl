package harnessctl

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

type auditFixture struct {
	template string
	target   string
}

func writeAuditFile(t *testing.T, root, relative, contents string, mode os.FileMode) {
	t.Helper()
	path := filepath.Join(root, relative)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(contents), mode); err != nil {
		t.Fatal(err)
	}
}

func auditDigest(contents string) string {
	digest := sha256.Sum256([]byte(contents))
	return hex.EncodeToString(digest[:])
}

func auditGit(t *testing.T, root string, arguments ...string) string {
	t.Helper()
	command := exec.Command("git", append([]string{"-C", root}, arguments...)...)
	command.Env = append(os.Environ(),
		"GIT_AUTHOR_NAME=Harness Test", "GIT_AUTHOR_EMAIL=harness@example.test",
		"GIT_COMMITTER_NAME=Harness Test", "GIT_COMMITTER_EMAIL=harness@example.test",
	)
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", arguments, err, output)
	}
	return string(bytes.TrimSpace(output))
}

func newAuditFixture(t *testing.T) auditFixture {
	t.Helper()
	root := t.TempDir()
	template := filepath.Join(root, "template")
	target := filepath.Join(root, "target")
	for _, repository := range []string{template, target} {
		if err := os.MkdirAll(repository, 0o755); err != nil {
			t.Fatal(err)
		}
		auditGit(t, repository, "init", "-b", "main")
	}

	manifest := `{"schema_version":1,"managed_paths":[{"path":"AGENTS.md","strategy":"manual_merge"},{"path":"docs/shared.md","strategy":"copy"},{"path":"docs/optional.md","strategy":"copy","required":false},{"path":"harness/policy.json","strategy":"project_overlay","version_field":"version","filter_symlinks":true},{"path":"CLAUDE.md","strategy":"symlink","target":"AGENTS.md"},{"path":"nested/CLAUDE.md","strategy":"symlink","target":"AGENTS.md","required":false,"when_target_exists":true}],"retired_paths":["scripts/retired.sh"]}` + "\n"
	writeAuditFile(t, template, "harness/scaffold_manifest.json", manifest, 0o644)
	writeAuditFile(t, template, "AGENTS.md", "template guidance\n", 0o644)
	writeAuditFile(t, template, "docs/shared.md", "shared\n", 0o644)
	writeAuditFile(t, template, "harness/policy.json", "{\"version\":3,\"shared\":true}\n", 0o644)
	auditGit(t, template, "add", ".")
	auditGit(t, template, "commit", "-m", "template")
	templateCommit := auditGit(t, template, "rev-parse", "HEAD")

	writeAuditFile(t, target, "AGENTS.md", "target guidance\n", 0o644)
	writeAuditFile(t, target, "docs/shared.md", "shared\n", 0o644)
	writeAuditFile(t, target, "harness/policy.json", "{\"version\":3,\"shared\":true,\"custom\":true}\n", 0o644)
	if err := os.Symlink("AGENTS.md", filepath.Join(target, "CLAUDE.md")); err != nil {
		t.Fatal(err)
	}
	lock := map[string]any{
		"schema_version":  1,
		"template_commit": templateCommit,
		"manifest_sha256": auditDigest(manifest),
		"resolved_paths": []map[string]any{
			{
				"path": "AGENTS.md", "strategy": "manual_merge", "resolution": "merged",
				"template_sha256": auditDigest("template guidance\n"),
				"target_sha256":   auditDigest("target guidance\n"),
			},
			{
				"path": "harness/policy.json", "strategy": "project_overlay", "resolution": "preserved",
				"template_sha256": auditDigest("{\"version\":3,\"shared\":true}\n"),
				"target_sha256":   auditDigest("{\"version\":3,\"shared\":true,\"custom\":true}\n"),
			},
		},
	}
	encoded, err := json.Marshal(lock)
	if err != nil {
		t.Fatal(err)
	}
	writeAuditFile(t, target, "harness/scaffold.lock", string(encoded)+"\n", 0o644)
	auditGit(t, target, "add", ".")
	auditGit(t, target, "commit", "-m", "target")
	return auditFixture{template: template, target: target}
}

func runAudit(t *testing.T, fixture auditFixture) (int, scaffoldAuditReport, string) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	exitCode := Run([]string{
		"scaffold", "audit", "--template", fixture.template, "--repo", fixture.target,
	}, &stdout, &stderr)
	var report scaffoldAuditReport
	if err := json.Unmarshal(stdout.Bytes(), &report); err != nil {
		t.Fatalf("audit output is not JSON: %v\nstdout=%q\nstderr=%q", err, stdout.String(), stderr.String())
	}
	return exitCode, report, stderr.String()
}

func TestScaffoldAuditAcceptsResolvedManualAndProjectOverlayPaths(t *testing.T) {
	fixture := newAuditFixture(t)
	exitCode, report, stderr := runAudit(t, fixture)
	if exitCode != 0 || !report.Converged || report.Status != "passed" {
		t.Fatalf("audit = exit %d, report %#v, stderr %q", exitCode, report, stderr)
	}
	if len(report.Errors) != 0 {
		t.Fatalf("audit errors = %v", report.Errors)
	}
}

func TestScaffoldAuditDetectsTargetDriftAndRetiredPaths(t *testing.T) {
	fixture := newAuditFixture(t)
	writeAuditFile(t, fixture.target, "AGENTS.md", "drifted target guidance\n", 0o644)
	writeAuditFile(t, fixture.target, "scripts/retired.sh", "#!/bin/sh\n", 0o755)

	exitCode, report, _ := runAudit(t, fixture)
	if exitCode != 1 || report.Converged || report.Status != "drift" {
		t.Fatalf("audit = exit %d, report %#v", exitCode, report)
	}
	if len(report.Errors) < 2 {
		t.Fatalf("audit errors = %v, want target drift and retired path", report.Errors)
	}
}

func TestScaffoldAuditRequiresConvergenceRecord(t *testing.T) {
	fixture := newAuditFixture(t)
	if err := os.Remove(filepath.Join(fixture.target, "harness/scaffold.lock")); err != nil {
		t.Fatal(err)
	}

	exitCode, report, _ := runAudit(t, fixture)
	if exitCode != 1 || report.Converged {
		t.Fatalf("audit = exit %d, report %#v", exitCode, report)
	}
}

func TestScaffoldAuditRequiresConditionalSymlinkWhenItsTargetExists(t *testing.T) {
	fixture := newAuditFixture(t)
	writeAuditFile(t, fixture.target, "nested/AGENTS.md", "nested contract\n", 0o644)

	exitCode, report, _ := runAudit(t, fixture)
	if exitCode != 1 || report.Converged {
		t.Fatalf("audit = exit %d, report %#v", exitCode, report)
	}
	found := false
	for _, message := range report.Errors {
		if message == "required managed path is missing: nested/CLAUDE.md" {
			found = true
		}
	}
	if !found {
		t.Fatalf("audit errors = %v, want conditional symlink error", report.Errors)
	}
}

func TestScaffoldAuditRejectsUnknownResolutionRecord(t *testing.T) {
	fixture := newAuditFixture(t)
	path := filepath.Join(fixture.target, "harness/scaffold.lock")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var lock map[string]any
	if err := json.Unmarshal(raw, &lock); err != nil {
		t.Fatal(err)
	}
	resolutions := lock["resolved_paths"].([]any)
	lock["resolved_paths"] = append(resolutions, map[string]any{
		"path": "unknown.md", "strategy": "manual_merge", "resolution": "preserved",
		"template_sha256": auditDigest("unknown template\n"),
		"target_sha256":   auditDigest("unknown target\n"),
	})
	encoded, err := json.Marshal(lock)
	if err != nil {
		t.Fatal(err)
	}
	writeAuditFile(t, fixture.target, "harness/scaffold.lock", string(encoded)+"\n", 0o644)

	exitCode, report, _ := runAudit(t, fixture)
	if exitCode != 1 || report.Converged {
		t.Fatalf("audit = exit %d, report %#v", exitCode, report)
	}
	found := false
	for _, message := range report.Errors {
		if message == "scaffold lock has an unknown resolution: unknown.md" {
			found = true
		}
	}
	if !found {
		t.Fatalf("audit errors = %v, want unknown resolution error", report.Errors)
	}
}

func TestDecodeStrictJSONRejectsTrailingValue(t *testing.T) {
	var document map[string]any
	if err := decodeStrictJSON([]byte("{}\n{}\n"), &document); err == nil {
		t.Fatal("decodeStrictJSON accepted a second JSON value")
	}
}
