package harnessctl

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

type evidenceFixture struct {
	repo        string
	integration string
	evidenceDir string
}

func newEvidenceFixture(t *testing.T) evidenceFixture {
	t.Helper()
	root := t.TempDir()
	repo := filepath.Join(root, "candidate")
	integration := filepath.Join(root, "integration")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	git := func(workdir string, arguments ...string) string {
		t.Helper()
		command := exec.Command("git", append([]string{"-C", workdir}, arguments...)...)
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
	write := func(relative, contents string, mode os.FileMode) {
		t.Helper()
		path := filepath.Join(repo, relative)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(contents), mode); err != nil {
			t.Fatal(err)
		}
	}

	policy := map[string]any{
		"schema_version":     3,
		"custom_gates":       map[string]any{},
		"symlinks":           []any{},
		"coverage_threshold": 0.0,
		"unsealed_artifacts": []any{"gates.tsv"},
		"gate_sets": map[string]any{
			"change":    []any{"change_scope", "ai_boundaries"},
			"candidate": []any{"change_scope", "release_context_before", "ai_boundaries", "release_context_after"},
			"release":   []any{"change_scope", "release_context_before", "ai_boundaries", "release_context_after"},
		},
		"profiles": map[string]any{
			"change":       map[string]any{"evidence_modes": []any{"change"}, "gate_set": "change", "skippable_gates": []any{}},
			"pull_request": map[string]any{"evidence_modes": []any{"candidate"}, "gate_set": "candidate", "skippable_gates": []any{}},
			"release":      map[string]any{"evidence_modes": []any{"release"}, "gate_set": "release", "skippable_gates": []any{}},
		},
		"conditional_gates": map[string]any{
			"test_race": map[string]any{"always_profiles": []any{}, "path_prefixes": []any{"internal/risk/"}, "skip_reason": "race test not selected"},
			"benchmarks": map[string]any{
				"always_profiles": []any{}, "path_prefixes": []any{"internal/observability/"},
				"benchmark_file_suffix": "_test.go", "benchmark_declaration": "func Benchmark",
				"explicit_request": false, "skip_reason": "benchmark not selected",
			},
		},
		"evidence_sets": map[string]any{
			"change":  map[string]any{"artifacts": []any{"gates.tsv", "change_scope.json", "ai_boundaries.json"}},
			"release": map[string]any{"artifacts": []any{"gates.tsv", "change_scope.json", "ai_boundaries.json"}},
		},
		"evidence":                 map[string]any{"change": "change", "candidate": "release", "release": "release"},
		"gate_artifacts":           map[string]any{},
		"machine_status_artifacts": []any{"ai_boundaries.json"},
		"trusted_approval_runtime": []any{".ai-boundaries.yml", "harness/harness.lock", "harness/harness_profiles.json"},
	}
	encodedPolicy, err := json.MarshalIndent(policy, "", "  ")
	if err != nil {
		t.Fatal(err)
	}

	git(repo, "init", "-b", "main")
	write(".ai-boundaries.yml", "allowed:\n  - docs/\napproval_required:\n  - .ai-boundaries.yml\n  - harness/\nforbidden:\n  - secrets/\n", 0o644)
	write(".gitignore", ".artifacts/\n.tools/\n", 0o644)
	write("harness/harness.lock", "{\"schema_version\":1,\"module\":\"github.com/Fueav/harnessctl\",\"version\":\"dev\"}\n", 0o644)
	write("harness/harness_profiles.json", string(encodedPolicy)+"\n", 0o644)
	write("docs/readme.md", "base\n", 0o644)
	git(repo, "add", ".")
	git(repo, "commit", "-m", "base")
	write("docs/readme.md", "candidate\n", 0o644)
	git(repo, "add", "docs/readme.md")
	git(repo, "commit", "-m", "candidate")

	t.Setenv("VERIFY_COMPARE_REF", "HEAD^")
	var stdout, stderr bytes.Buffer
	if exitCode := Run([]string{"verify", "candidate", "--repo", repo}, &stdout, &stderr); exitCode != 0 {
		t.Fatalf("verify candidate exit code = %d\nstdout:\n%s\nstderr:\n%s", exitCode, stdout.String(), stderr.String())
	}
	git(repo, "worktree", "add", "-b", "integrated", integration, "HEAD")
	return evidenceFixture{
		repo: repo, integration: integration, evidenceDir: filepath.Join(repo, ".artifacts/candidate"),
	}
}

func runEvidenceVerify(t *testing.T, fixture evidenceFixture) (int, string, string) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	exitCode := Run([]string{
		"evidence", "verify", "--repo", fixture.integration,
		"--evidence-dir", fixture.evidenceDir, "--compare-ref", "HEAD^",
	}, &stdout, &stderr)
	return exitCode, stdout.String(), stderr.String()
}

func TestEvidenceVerifyReusesSealedCandidateOnSameCommit(t *testing.T) {
	fixture := newEvidenceFixture(t)
	exitCode, stdout, stderr := runEvidenceVerify(t, fixture)
	if exitCode != 0 {
		t.Fatalf("evidence verify exit code = %d\nstdout:\n%s\nstderr:\n%s", exitCode, stdout, stderr)
	}
	var report struct {
		Status string `json:"status"`
		Reused bool   `json:"reused"`
		Mode   string `json:"mode"`
	}
	if err := json.Unmarshal([]byte(stdout), &report); err != nil {
		t.Fatal(err)
	}
	if report.Status != "passed" || !report.Reused || report.Mode != "candidate" {
		t.Fatalf("reuse report = %+v", report)
	}
}

func TestEvidenceVerifyRejectsTamperedArtifact(t *testing.T) {
	fixture := newEvidenceFixture(t)
	log := filepath.Join(fixture.evidenceDir, "logs/ai_boundaries.log")
	file, err := os.OpenFile(log, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.WriteString("tampered\n"); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	exitCode, _, stderr := runEvidenceVerify(t, fixture)
	if exitCode == 0 || !bytes.Contains([]byte(stderr), []byte("modified")) {
		t.Fatalf("tampered evidence exit = %d, stderr = %q", exitCode, stderr)
	}
}

func TestEvidenceVerifyRejectsChangedVerifierPolicy(t *testing.T) {
	fixture := newEvidenceFixture(t)
	path := filepath.Join(fixture.integration, "harness/harness_profiles.json")
	file, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.WriteString("\n"); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	exitCode, _, stderr := runEvidenceVerify(t, fixture)
	if exitCode == 0 || !bytes.Contains([]byte(stderr), []byte("working tree")) {
		t.Fatalf("changed policy exit = %d, stderr = %q", exitCode, stderr)
	}
}

func TestEvidenceVerifyRejectsStaleHead(t *testing.T) {
	fixture := newEvidenceFixture(t)
	path := filepath.Join(fixture.integration, "docs/readme.md")
	if err := os.WriteFile(path, []byte("new head\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	command := exec.Command("git", "-C", fixture.integration, "add", "docs/readme.md")
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("git add: %v\n%s", err, output)
	}
	command = exec.Command("git", "-C", fixture.integration, "commit", "-m", "new head")
	command.Env = append(os.Environ(),
		"GIT_AUTHOR_NAME=Harness Test", "GIT_AUTHOR_EMAIL=harness@example.test",
		"GIT_COMMITTER_NAME=Harness Test", "GIT_COMMITTER_EMAIL=harness@example.test",
	)
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("git commit: %v\n%s", err, output)
	}
	exitCode, _, stderr := runEvidenceVerify(t, fixture)
	if exitCode == 0 || !bytes.Contains([]byte(stderr), []byte("stale for the current HEAD")) {
		t.Fatalf("stale evidence exit = %d, stderr = %q", exitCode, stderr)
	}
}
