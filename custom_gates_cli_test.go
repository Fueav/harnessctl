package harnessctl

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

type customGateFixture struct {
	repo      string
	gateNames []string
}

func newCustomGateFixture(t *testing.T, kind string) customGateFixture {
	t.Helper()
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

	rawPolicy, err := engineFS.ReadFile("scripts/harness_profiles.json")
	if err != nil {
		t.Fatal(err)
	}
	var policy map[string]any
	if err := json.Unmarshal(rawPolicy, &policy); err != nil {
		t.Fatal(err)
	}
	gateNames := []string{"project_check"}
	if kind == "failure" {
		gateNames = []string{"fail_check", "after_check"}
	}
	custom := make(map[string]any, len(gateNames))
	gateSets := policy["gate_sets"].(map[string]any)
	changeGates := gateSets["change"].([]any)
	for _, name := range gateNames {
		custom[name] = map[string]any{"run": "scripts/gates/" + name + ".sh"}
		changeGates = append(changeGates, name)
	}
	gateSets["change"] = changeGates
	policy["schema_version"] = float64(2)
	policy["custom_gates"] = custom
	policy["symlinks"] = []any{}
	profiles := policy["profiles"].(map[string]any)
	profiles["nightly"] = profiles["release"]
	conditions := policy["conditional_gates"].(map[string]any)
	for _, raw := range conditions {
		rule := raw.(map[string]any)
		always := rule["always_profiles"].([]any)
		rule["always_profiles"] = append([]any{"nightly"}, always...)
	}
	if strings.HasPrefix(kind, "conditional-") {
		policy["schema_version"] = float64(3)
		delete(profiles, "nightly")
		for _, raw := range conditions {
			rule := raw.(map[string]any)
			always := rule["always_profiles"].([]any)
			filtered := make([]any, 0, len(always))
			for _, profile := range always {
				if profile != "nightly" {
					filtered = append(filtered, profile)
				}
			}
			rule["always_profiles"] = filtered
		}
		prefixes := []any{"docs/"}
		if kind == "conditional-skip" {
			prefixes = []any{"config/"}
		}
		conditions["project_check"] = map[string]any{
			"always_profiles": []any{},
			"path_prefixes":   prefixes,
			"skip_reason":     "project check is unrelated to this change",
		}
	}
	if kind == "passing" {
		gateArtifacts := policy["gate_artifacts"].(map[string]any)
		gateArtifacts["project_check"] = map[string]any{
			"artifacts": []any{"custom/project-check.json"},
		}
	}
	encodedPolicy, err := json.MarshalIndent(policy, "", "  ")
	if err != nil {
		t.Fatal(err)
	}

	git("init", "-b", "main")
	write(".ai-boundaries.yml", "allowed:\n  - docs/\napproval_required:\n  - .ai-boundaries.yml\n  - scripts/\nforbidden:\n  - secrets/\n", 0o644)
	write(".gitignore", ".artifacts/\n.tools/\n", 0o644)
	write("AGENTS.md", "# Config-only fixture\n", 0o644)
	write("docs/harness-workflows.json", `{
  "version": 2,
  "workflow_classes": [
    {"id":"HARNESS-FOCUSED-CHANGE","use_when":"focused","artifact_policy":"none","verification":"focused","stop_rule":"done","evidence":["test"]},
    {"id":"HARNESS-MAINTENANCE","use_when":"maintenance","artifact_policy":"checklist","verification":"checks","stop_rule":"no growth","evidence":["diff"]},
    {"id":"HARNESS-SPEC-FIRST-FEATURE","use_when":"feature","artifact_policy":"spec","verification":"release","stop_rule":"approval","evidence":["spec"]},
    {"id":"HARNESS-VERIFICATION-INCIDENT","use_when":"diagnosis","artifact_policy":"evidence","verification":"reproduce","stop_rule":"truth","evidence":["logs"]}
  ]
}`, 0o644)
	write("docs/readme.md", "base\n", 0o644)
	write("specs/index.json", "{\"version\":1,\"specs\":[]}\n", 0o644)
	write("harness/harness_profiles.json", string(encodedPolicy)+"\n", 0o644)
	write("harness/harness.lock", "{\"schema_version\":1,\"module\":\"github.com/Fueav/harnessctl\",\"version\":\"dev\"}\n", 0o644)

	switch kind {
	case "passing", "conditional-run", "conditional-skip":
		write("scripts/gates/project_check.sh", `#!/usr/bin/env bash
set -euo pipefail
[[ "$PWD" == "$HARNESS_PROJECT_ROOT" ]]
for variable in HARNESS_PROJECT_ROOT HARNESS_ARTIFACT_DIR HARNESS_SNAPSHOT_FILE HARNESS_SNAPSHOT_SHA256 HARNESS_COMPARE_SHA HARNESS_HEAD_SHA HARNESS_PROFILE HARNESS_EVIDENCE_MODE; do
  [[ -n "${!variable:-}" ]]
done
[[ -z "${HARNESS_ENGINE_DIR+x}" ]]
[[ "$HARNESS_PROFILE" == change ]]
[[ "$HARNESS_EVIDENCE_MODE" == change ]]
mkdir -p "$HARNESS_ARTIFACT_DIR/custom"
printf '{"status":"passed"}\n' >"$HARNESS_ARTIFACT_DIR/custom/project-check.json"
printf 'custom gate passed\n'
`, 0o755)
	case "failure":
		write("scripts/gates/fail_check.sh", "#!/usr/bin/env bash\nprintf 'intentional custom failure\\n' >&2\nexit 7\n", 0o755)
		write("scripts/gates/after_check.sh", "#!/usr/bin/env bash\ntouch \"$HARNESS_PROJECT_ROOT/after-ran\"\n", 0o755)
	case "non-executable":
		write("scripts/gates/project_check.sh", "#!/usr/bin/env bash\nexit 0\n", 0o644)
	case "symlink":
		write("scripts/gates/real_check.sh", "#!/usr/bin/env bash\nexit 0\n", 0o755)
		if err := os.Symlink("real_check.sh", filepath.Join(repo, "scripts/gates/project_check.sh")); err != nil {
			t.Fatal(err)
		}
	case "missing":
	default:
		t.Fatalf("unknown custom gate fixture kind %q", kind)
	}

	git("add", ".")
	git("commit", "-m", "base")
	write("docs/readme.md", "changed\n", 0o644)
	return customGateFixture{repo: repo, gateNames: gateNames}
}

func runFixtureChange(t *testing.T, fixture customGateFixture) (int, string, string) {
	t.Helper()
	t.Setenv("VERIFY_COMPARE_REF", "HEAD")
	var stdout, stderr bytes.Buffer
	exitCode := Run([]string{"verify", "change", "--repo", fixture.repo}, &stdout, &stderr)
	return exitCode, stdout.String(), stderr.String()
}

func loadJSONFile(t *testing.T, path string, destination any) {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, destination); err != nil {
		t.Fatalf("decode %s: %v", path, err)
	}
}

func TestVerifyChangeRunsPassingCustomGateAndSealsEvidence(t *testing.T) {
	fixture := newCustomGateFixture(t, "passing")
	exitCode, stdout, stderr := runFixtureChange(t, fixture)
	if exitCode != 0 {
		t.Fatalf("verify change exit code = %d\nstdout:\n%s\nstderr:\n%s", exitCode, stdout, stderr)
	}

	var summary struct {
		Overall string `json:"overall"`
		Gates   []struct {
			Name      string `json:"name"`
			Status    string `json:"status"`
			LogPath   string `json:"log_path"`
			LogSHA256 string `json:"log_sha256"`
		} `json:"gates"`
		SealedArtifacts []struct {
			Path string `json:"path"`
		} `json:"sealed_artifacts"`
	}
	artifactDir := filepath.Join(fixture.repo, ".artifacts/change")
	loadJSONFile(t, filepath.Join(artifactDir, "summary.json"), &summary)
	if summary.Overall != "passed" {
		t.Fatalf("summary overall = %q, want passed", summary.Overall)
	}
	wantNames := []string{
		"change_scope", "toolchain", "gofmt", "vet", "golangci",
		"changed_package_tests", "ai_boundaries", "spec_registry", "project_check",
	}
	if len(summary.Gates) != len(wantNames) {
		t.Fatalf("summary gates = %v, want %v", summary.Gates, wantNames)
	}
	for index, want := range wantNames {
		if summary.Gates[index].Name != want {
			t.Fatalf("gate %d = %q, want %q", index, summary.Gates[index].Name, want)
		}
	}
	custom := summary.Gates[len(summary.Gates)-1]
	if custom.Status != "passed" || custom.LogPath != "logs/project_check.log" {
		t.Fatalf("custom gate = %+v", custom)
	}
	logRaw, err := os.ReadFile(filepath.Join(artifactDir, custom.LogPath))
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(logRaw)
	if custom.LogSHA256 != hex.EncodeToString(digest[:]) {
		t.Fatalf("custom log digest = %q, want %x", custom.LogSHA256, digest)
	}

	var manifest struct {
		Artifacts []struct {
			Path   string `json:"path"`
			SHA256 string `json:"sha256"`
		} `json:"artifacts"`
	}
	loadJSONFile(t, filepath.Join(artifactDir, "artifact_manifest.json"), &manifest)
	manifestPaths := map[string]string{}
	for _, artifact := range manifest.Artifacts {
		manifestPaths[artifact.Path] = artifact.SHA256
	}
	if manifestPaths[custom.LogPath] != custom.LogSHA256 {
		t.Fatalf("custom log is absent from manifest: %v", manifestPaths)
	}
	if manifestPaths["custom/project-check.json"] == "" {
		t.Fatalf("custom artifact is absent from manifest: %v", manifestPaths)
	}
	sealed := map[string]bool{}
	for _, artifact := range summary.SealedArtifacts {
		sealed[artifact.Path] = true
	}
	if !sealed["custom/project-check.json"] {
		t.Fatalf("custom artifact was not sealed: %v", sealed)
	}
}

func TestVerifyChangeConditionallyRunsOrSkipsCustomGate(t *testing.T) {
	for _, testCase := range []struct {
		kind       string
		wantStatus string
	}{
		{kind: "conditional-run", wantStatus: "passed"},
		{kind: "conditional-skip", wantStatus: "skipped"},
	} {
		t.Run(testCase.kind, func(t *testing.T) {
			fixture := newCustomGateFixture(t, testCase.kind)
			exitCode, stdout, stderr := runFixtureChange(t, fixture)
			if exitCode != 0 {
				t.Fatalf("verify change exit code = %d\nstdout:\n%s\nstderr:\n%s", exitCode, stdout, stderr)
			}
			var summary struct {
				Overall string `json:"overall"`
				Gates   []struct {
					Name   string `json:"name"`
					Status string `json:"status"`
				} `json:"gates"`
			}
			loadJSONFile(t, filepath.Join(fixture.repo, ".artifacts/change/summary.json"), &summary)
			if summary.Overall != "passed" {
				t.Fatalf("summary overall = %q", summary.Overall)
			}
			last := summary.Gates[len(summary.Gates)-1]
			if last.Name != "project_check" || last.Status != testCase.wantStatus {
				t.Fatalf("conditional gate = %+v, want status %q", last, testCase.wantStatus)
			}
		})
	}
}

func TestVerifyChangeStopsAfterFailingCustomGate(t *testing.T) {
	fixture := newCustomGateFixture(t, "failure")
	exitCode, stdout, stderr := runFixtureChange(t, fixture)
	if exitCode == 0 {
		t.Fatalf("failing custom gate exited 0\nstdout:\n%s\nstderr:\n%s", stdout, stderr)
	}
	if _, err := os.Stat(filepath.Join(fixture.repo, "after-ran")); !os.IsNotExist(err) {
		t.Fatalf("gate after failure ran: %v", err)
	}
	var summary struct {
		Overall string `json:"overall"`
		Gates   []struct {
			Name   string `json:"name"`
			Status string `json:"status"`
		} `json:"gates"`
	}
	loadJSONFile(t, filepath.Join(fixture.repo, ".artifacts/change/summary.json"), &summary)
	if summary.Overall != "failed" || len(summary.Gates) == 0 {
		t.Fatalf("failed summary = %+v", summary)
	}
	last := summary.Gates[len(summary.Gates)-1]
	if last.Name != "fail_check" || last.Status != "failed" {
		t.Fatalf("last gate = %+v, want failed fail_check", last)
	}
}

func TestVerifyChangeRecordsInvalidCustomScriptsAsFailedGates(t *testing.T) {
	for _, kind := range []string{"missing", "non-executable", "symlink"} {
		t.Run(kind, func(t *testing.T) {
			fixture := newCustomGateFixture(t, kind)
			exitCode, stdout, stderr := runFixtureChange(t, fixture)
			if exitCode == 0 {
				t.Fatalf("invalid custom script exited 0\nstdout:\n%s\nstderr:\n%s", stdout, stderr)
			}
			if strings.Contains(stderr, "Traceback") {
				t.Fatalf("invalid custom script crashed engine:\n%s", stderr)
			}
			var summary struct {
				Overall string `json:"overall"`
				Gates   []struct {
					Name   string `json:"name"`
					Status string `json:"status"`
				} `json:"gates"`
			}
			loadJSONFile(t, filepath.Join(fixture.repo, ".artifacts/change/summary.json"), &summary)
			last := summary.Gates[len(summary.Gates)-1]
			if summary.Overall != "failed" || last.Name != "project_check" || last.Status != "failed" {
				t.Fatalf("invalid script summary = %+v", summary)
			}
		})
	}
}
