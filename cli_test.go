package harnessctl

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestVersionReportsPinnedCLIIdentity(t *testing.T) {
	var stdout, stderr bytes.Buffer

	exitCode := Run([]string{"version"}, &stdout, &stderr)

	if exitCode != 0 {
		t.Fatalf("Run(version) exit code = %d, stderr = %q", exitCode, stderr.String())
	}
	if got := strings.TrimSpace(stdout.String()); got != "harnessctl dev" {
		t.Fatalf("Run(version) = %q, want %q", got, "harnessctl dev")
	}
}

func TestResolveVersionPrefersReleaseLinkerValue(t *testing.T) {
	if got := ResolveVersion("v1.2.3"); got != "v1.2.3" {
		t.Fatalf("ResolveVersion(v1.2.3) = %q", got)
	}
}

func TestUnknownCommandFailsClosed(t *testing.T) {
	var stdout, stderr bytes.Buffer

	exitCode := Run([]string{"unknown"}, &stdout, &stderr)

	if exitCode != 2 {
		t.Fatalf("Run(unknown) exit code = %d, want 2", exitCode)
	}
	if !strings.Contains(stderr.String(), "unknown command") {
		t.Fatalf("Run(unknown) stderr = %q, want unknown-command error", stderr.String())
	}
}

func TestHelpIsDiscoverableAndVersionRejectsExtraArguments(t *testing.T) {
	for _, args := range [][]string{{"help"}, {"--help"}, {"-h"}} {
		var stdout, stderr bytes.Buffer
		if exitCode := Run(args, &stdout, &stderr); exitCode != 0 {
			t.Fatalf("Run(%v) exit code = %d, stderr = %q", args, exitCode, stderr.String())
		}
		if !strings.Contains(stdout.String(), "scaffold audit|record") || !strings.Contains(stdout.String(), "evidence verify") {
			t.Fatalf("Run(%v) help is incomplete: %q", args, stdout.String())
		}
	}

	var stdout, stderr bytes.Buffer
	if exitCode := Run([]string{"version", "extra"}, &stdout, &stderr); exitCode != 2 {
		t.Fatalf("Run(version extra) exit code = %d, want 2", exitCode)
	}
	if !strings.Contains(stderr.String(), "version accepts no arguments") {
		t.Fatalf("Run(version extra) stderr = %q", stderr.String())
	}
}

func TestWorkspacePreflightRejectsUnconsumedArguments(t *testing.T) {
	var stdout, stderr bytes.Buffer
	exitCode := Run([]string{"workspace-preflight", "unexpected"}, &stdout, &stderr)
	if exitCode != 2 {
		t.Fatalf("workspace-preflight extra argument exit code = %d, want 2", exitCode)
	}
	if !strings.Contains(stderr.String(), "unexpected arguments") {
		t.Fatalf("workspace-preflight stderr = %q", stderr.String())
	}
}

func TestBoundaryCheckUsesCentralEngineAgainstProject(t *testing.T) {
	repo := t.TempDir()
	git := func(args ...string) {
		t.Helper()
		command := exec.Command("git", append([]string{"-C", repo}, args...)...)
		command.Env = append(os.Environ(),
			"GIT_AUTHOR_NAME=Harness Test", "GIT_AUTHOR_EMAIL=harness@example.test",
			"GIT_COMMITTER_NAME=Harness Test", "GIT_COMMITTER_EMAIL=harness@example.test",
		)
		if output, err := command.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, output)
		}
	}
	write := func(relative, contents string) {
		t.Helper()
		path := filepath.Join(repo, relative)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(contents), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	git("init", "-b", "main")
	write(".ai-boundaries.yml", "allowed:\n  - docs/\napproval_required:\n  - .ai-boundaries.yml\nforbidden:\n  - secrets/\n")
	write("harness/harness.lock", "{\"schema_version\":1,\"module\":\"github.com/Fueav/harnessctl\",\"version\":\"dev\"}\n")
	write("docs/readme.md", "base\n")
	git("add", ".")
	git("commit", "-m", "base")
	write("docs/readme.md", "changed\n")

	var stdout, stderr bytes.Buffer
	exitCode := Run([]string{
		"check", "boundaries", "--repo", repo, "--base", "HEAD",
		"--artifact-dir", ".artifacts/change",
	}, &stdout, &stderr)
	if exitCode != 0 {
		t.Fatalf("boundary check exit code = %d\nstdout:\n%s\nstderr:\n%s", exitCode, stdout.String(), stderr.String())
	}
	if !strings.Contains(stdout.String(), "AI boundary check passed") {
		t.Fatalf("boundary stdout = %q", stdout.String())
	}
	if _, err := os.Stat(filepath.Join(repo, ".artifacts/change/ai_boundaries.json")); err != nil {
		t.Fatalf("boundary evidence missing: %v", err)
	}
}

func TestVerifyChangeRunsFromCentralEngineWithProjectPolicy(t *testing.T) {
	repo := t.TempDir()
	git := func(args ...string) {
		t.Helper()
		command := exec.Command("git", append([]string{"-C", repo}, args...)...)
		command.Env = append(os.Environ(),
			"GIT_AUTHOR_NAME=Harness Test", "GIT_AUTHOR_EMAIL=harness@example.test",
			"GIT_COMMITTER_NAME=Harness Test", "GIT_COMMITTER_EMAIL=harness@example.test",
		)
		if output, err := command.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, output)
		}
	}
	write := func(relative string, contents []byte) {
		t.Helper()
		path := filepath.Join(repo, relative)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, contents, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	profiles, err := engineFS.ReadFile("scripts/harness_profiles.json")
	if err != nil {
		t.Fatal(err)
	}
	workflowManifest := `{
  "version": 3,
  "workflow_classes": [
    "HARNESS-FOCUSED-CHANGE",
    "HARNESS-MAINTENANCE",
    "HARNESS-SPEC-FIRST-FEATURE",
    "HARNESS-VERIFICATION-INCIDENT"
  ]
}`

	git("init", "-b", "main")
	write(".ai-boundaries.yml", []byte("allowed:\n  - docs/\napproval_required:\n  - .ai-boundaries.yml\n  - scripts/\nforbidden:\n  - secrets/\n"))
	write(".gitignore", []byte(".artifacts/\n.tools/\n"))
	write("AGENTS.md", []byte("# Fixture\n"))
	write("docs/harness-workflows.json", []byte(workflowManifest))
	write("docs/readme.md", []byte("base\n"))
	write("specs/index.json", []byte("{\"version\":1,\"specs\":[]}\n"))
	write("harness/harness_profiles.json", profiles)
	write("harness/harness.lock", []byte("{\"schema_version\":1,\"module\":\"github.com/Fueav/harnessctl\",\"version\":\"dev\"}\n"))
	git("add", ".")
	git("commit", "-m", "base")
	write("docs/readme.md", []byte("changed\n"))
	t.Setenv("VERIFY_COMPARE_REF", "HEAD")

	var stdout, stderr bytes.Buffer
	exitCode := Run([]string{"verify", "change", "--repo", repo}, &stdout, &stderr)
	if exitCode != 0 {
		t.Fatalf("verify change exit code = %d\nstdout:\n%s\nstderr:\n%s", exitCode, stdout.String(), stderr.String())
	}
	if !strings.Contains(stdout.String(), "verify-change evidence written") {
		t.Fatalf("verify stdout = %q", stdout.String())
	}
	if _, err := os.Stat(filepath.Join(repo, ".artifacts/change/summary.json")); err != nil {
		t.Fatalf("summary evidence missing: %v", err)
	}
}

func TestSpecRegistryAcceptsLegacyVersionTwoThroughFacade(t *testing.T) {
	repo := t.TempDir()
	write := func(relative, contents string) {
		t.Helper()
		path := filepath.Join(repo, relative)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(contents), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("AGENTS.md", "# Fixture\n")
	write("docs/harness-workflows.json", `{
  "version": 2,
  "workflow_classes": [
    {"id":"HARNESS-FOCUSED-CHANGE","use_when":"focused","artifact_policy":"none","verification":"focused","stop_rule":"done","evidence":["test"]},
    {"id":"HARNESS-MAINTENANCE","use_when":"maintenance","artifact_policy":"checklist","verification":"checks","stop_rule":"no growth","evidence":["diff"]},
    {"id":"HARNESS-SPEC-FIRST-FEATURE","use_when":"feature","artifact_policy":"spec","verification":"release","stop_rule":"approval","evidence":["spec"]},
    {"id":"HARNESS-VERIFICATION-INCIDENT","use_when":"diagnosis","artifact_policy":"evidence","verification":"reproduce","stop_rule":"truth","evidence":["logs"]}
  ]
}`)
	write("harness/harness.lock", "{\"schema_version\":1,\"module\":\"github.com/Fueav/harnessctl\",\"version\":\"dev\"}\n")
	write("harness/harness_profiles.json", "{}\n")
	write("specs/index.json", "{\"version\":1,\"specs\":[]}\n")

	var stdout, stderr bytes.Buffer
	exitCode := Run([]string{
		"check", "spec-registry", "--repo", repo,
		"--artifact-dir", ".artifacts/change",
	}, &stdout, &stderr)
	if exitCode != 0 {
		t.Fatalf("legacy spec registry exit code = %d\nstdout:\n%s\nstderr:\n%s", exitCode, stdout.String(), stderr.String())
	}
	if _, err := os.Stat(filepath.Join(repo, ".artifacts/change/spec_registry.json")); err != nil {
		t.Fatalf("legacy spec registry evidence missing: %v", err)
	}
}

func TestEngineRefusesVersionMismatchBeforeExecution(t *testing.T) {
	repo := t.TempDir()
	if err := os.MkdirAll(filepath.Join(repo, "harness"), 0o755); err != nil {
		t.Fatal(err)
	}
	lock := []byte("{\"schema_version\":1,\"module\":\"github.com/Fueav/harnessctl\",\"version\":\"v9.9.9\"}\n")
	if err := os.WriteFile(filepath.Join(repo, "harness/harness.lock"), lock, 0o644); err != nil {
		t.Fatal(err)
	}

	var stdout, stderr bytes.Buffer
	exitCode := Run([]string{"check", "boundaries", "--repo", repo, "--base", "HEAD"}, &stdout, &stderr)

	if exitCode != 2 {
		t.Fatalf("mismatched lock exit code = %d, want 2", exitCode)
	}
	if !strings.Contains(stderr.String(), "requires harnessctl v9.9.9") {
		t.Fatalf("mismatched lock stderr = %q", stderr.String())
	}
}

func TestEngineRejectsTrailingHarnessLockData(t *testing.T) {
	repo := t.TempDir()
	if err := os.MkdirAll(filepath.Join(repo, "harness"), 0o755); err != nil {
		t.Fatal(err)
	}
	lock := []byte("{\"schema_version\":1,\"module\":\"github.com/Fueav/harnessctl\",\"version\":\"dev\"}\n{}\n")
	if err := os.WriteFile(filepath.Join(repo, "harness/harness.lock"), lock, 0o644); err != nil {
		t.Fatal(err)
	}

	var stdout, stderr bytes.Buffer
	exitCode := Run([]string{"check", "boundaries", "--repo", repo, "--base", "HEAD"}, &stdout, &stderr)
	if exitCode != 2 || !strings.Contains(stderr.String(), "parse harness/harness.lock") {
		t.Fatalf("trailing lock exit = %d, stderr = %q", exitCode, stderr.String())
	}
}
