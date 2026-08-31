package harnessctl

import (
	"embed"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
)

var Version = "dev"

const Module = "github.com/Fueav/harnessctl"

//go:embed scripts
var engineFS embed.FS

func Run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprint(stderr, usageText())
		return 2
	}
	if args[0] == "help" || args[0] == "--help" || args[0] == "-h" {
		fmt.Fprint(stdout, usageText())
		return 0
	}

	switch args[0] {
	case "resources":
		if len(args) < 2 || (args[1] != "run" && args[1] != "status" && args[1] != "gc") {
			return cliError(stderr, "resources requires run, status, or gc")
		}
		return runEngine("resources.py", args[1:], stdout, stderr)
	case "version":
		if len(args) != 1 {
			return cliError(stderr, "version accepts no arguments")
		}
		fmt.Fprintf(stdout, "harnessctl %s\n", Version)
		return 0
	case "scaffold":
		if len(args) < 2 {
			return cliError(stderr, "scaffold requires audit or record")
		}
		switch args[1] {
		case "audit":
			return runScaffoldAudit(args[2:], stdout, stderr)
		case "record":
			return runScaffoldRecord(args[2:], stdout, stderr)
		default:
			return cliError(stderr, "scaffold requires audit or record")
		}
	case "evidence", "approval", "collect":
		contract := map[string][2]string{
			"evidence": {"verify", "verify_evidence.py"},
			"approval": {"finalize", "finalize_approval.py"},
			"collect":  {"changes", "collect_changes.py"},
		}[args[0]]
		if len(args) < 2 || args[1] != contract[0] {
			return cliError(stderr, "%s requires %s", args[0], contract[0])
		}
		return runEngine(contract[1], args[2:], stdout, stderr)
	case "check":
		if len(args) < 2 {
			return cliError(stderr, "check requires boundaries or spec-registry")
		}
		script := map[string]string{
			"boundaries":    "check_ai_boundaries.sh",
			"spec-registry": "check_spec_registry.sh",
		}[args[1]]
		if script == "" {
			return cliError(stderr, "unknown check %q", args[1])
		}
		return runEngine(script, args[2:], stdout, stderr)
	case "verify":
		if len(args) < 2 {
			return cliError(stderr, "verify requires change, candidate, or release")
		}
		script := map[string]string{
			"change":    "verify_change.sh",
			"candidate": "verify_candidate.sh",
			"release":   "verify_release.sh",
		}[args[1]]
		if script == "" {
			return cliError(stderr, "unknown verification profile %q", args[1])
		}
		return runEngine(script, args[2:], stdout, stderr)
	case "install-tools":
		return runEngine("install_tools.sh", args[1:], stdout, stderr)
	case "workspace-preflight":
		_, forwarded, err := projectRoot(args[1:])
		if err != nil {
			return cliError(stderr, "%v", err)
		}
		if len(forwarded) != 0 {
			return cliError(stderr, "workspace-preflight received unexpected arguments")
		}
		return runEngine("workspace_preflight.sh", args[1:], stdout, stderr)
	default:
		return cliError(stderr, "unknown command %q", args[0])
	}
}

func cliError(stderr io.Writer, format string, values ...any) int {
	fmt.Fprintf(stderr, "harnessctl: "+format+"\n", values...)
	return 2
}

func usageText() string {
	return `usage: harnessctl <command>

commands:
  version
  check boundaries|spec-registry
  verify change|candidate|release
  evidence verify
  scaffold audit|record
  approval finalize
  collect changes
  install-tools
  workspace-preflight
  resources run|status|gc
`
}

func runEngine(script string, arguments []string, stdout, stderr io.Writer) int {
	repo, forwarded, err := projectRoot(arguments)
	if err != nil {
		return cliError(stderr, "%v", err)
	}
	if err := verifyProjectLock(repo); err != nil {
		return cliError(stderr, "%v", err)
	}
	runtimeRoot, err := os.MkdirTemp("", "harnessctl-engine-")
	if err != nil {
		return cliError(stderr, "create engine runtime: %v", err)
	}
	defer os.RemoveAll(runtimeRoot)
	if err := extractEngine(runtimeRoot); err != nil {
		return cliError(stderr, "extract engine runtime: %v", err)
	}

	command := exec.Command(filepath.Join(runtimeRoot, "scripts", script), forwarded...)
	command.Dir = repo
	command.Stdout = stdout
	command.Stderr = stderr
	profileConfig := filepath.Join(repo, "harness", "harness_profiles.json")
	if _, err := os.Stat(profileConfig); err != nil {
		profileConfig = filepath.Join(repo, "scripts", "harness_profiles.json")
	}
	command.Env = append(os.Environ(),
		"HARNESS_PROJECT_ROOT="+repo,
		"HARNESS_ENGINE_DIR="+filepath.Join(runtimeRoot, "scripts"),
		"HARNESS_PROFILE_CONFIG="+profileConfig,
		"HARNESS_TOOL_VERSIONS="+filepath.Join(repo, "harness", "tool_versions.env"),
		"HARNESS_EXTERNAL_ENGINE=1",
		"HARNESSCTL_VERSION="+Version,
		"HARNESSCTL_OWNER_PID="+fmt.Sprint(os.Getpid()),
	)
	if err := runSignaledCommand(command); err != nil {
		if exitError, ok := err.(*exec.ExitError); ok {
			return exitError.ExitCode()
		}
		return cliError(stderr, "execute engine: %v", err)
	}
	return 0
}

func runSignaledCommand(command *exec.Cmd) error {
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)
	if err := command.Start(); err != nil {
		return err
	}
	completed := make(chan error, 1)
	go func() { completed <- command.Wait() }()
	for {
		select {
		case sig := <-signals:
			_ = command.Process.Signal(sig)
		case err := <-completed:
			return err
		}
	}
}

func verifyProjectLock(repo string) error {
	path := filepath.Join(repo, "harness", "harness.lock")
	info, err := os.Lstat(path)
	if err != nil {
		return fmt.Errorf("read harness/harness.lock: %w", err)
	}
	if !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("harness/harness.lock must be a regular non-symlink file")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read harness/harness.lock: %w", err)
	}
	var lock struct {
		SchemaVersion int    `json:"schema_version"`
		Module        string `json:"module"`
		Version       string `json:"version"`
	}
	if err := decodeStrictJSON(raw, &lock); err != nil {
		return fmt.Errorf("parse harness/harness.lock: %w", err)
	}
	if lock.SchemaVersion != 1 || lock.Module != Module || lock.Version == "" {
		return fmt.Errorf("harness/harness.lock has an invalid contract")
	}
	if lock.Version != Version {
		return fmt.Errorf("project requires harnessctl %s, running %s", lock.Version, Version)
	}
	return nil
}

func projectRoot(arguments []string) (string, []string, error) {
	repo := "."
	forwarded := make([]string, 0, len(arguments))
	for index := 0; index < len(arguments); index++ {
		if arguments[index] == "--" {
			forwarded = append(forwarded, arguments[index:]...)
			break
		}
		if arguments[index] != "--repo" {
			forwarded = append(forwarded, arguments[index])
			continue
		}
		if index+1 >= len(arguments) || strings.TrimSpace(arguments[index+1]) == "" {
			return "", nil, fmt.Errorf("--repo requires a path")
		}
		repo = arguments[index+1]
		index++
	}
	resolved, err := filepath.Abs(repo)
	if err != nil {
		return "", nil, fmt.Errorf("resolve project root: %w", err)
	}
	info, err := os.Lstat(resolved)
	if err != nil {
		return "", nil, fmt.Errorf("project root: %w", err)
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return "", nil, fmt.Errorf("project root must be a non-symlink directory")
	}
	return resolved, forwarded, nil
}

func extractEngine(destination string) error {
	return fs.WalkDir(engineFS, "scripts", func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		target := filepath.Join(destination, filepath.FromSlash(path))
		if entry.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		contents, err := engineFS.ReadFile(path)
		if err != nil {
			return err
		}
		mode := os.FileMode(0o644)
		if strings.HasSuffix(path, ".sh") || strings.HasSuffix(path, ".py") {
			mode = 0o755
		}
		return os.WriteFile(target, contents, mode)
	})
}
