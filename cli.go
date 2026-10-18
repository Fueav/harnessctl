package harnessctl

import (
	"crypto/sha256"
	"embed"
	"encoding/json"
	"errors"
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
	case "version":
		if len(args) != 1 {
			return cliError(stdout, "version accepts no arguments")
		}
		fmt.Fprintf(stdout, "harnessctl %s\n", Version)
		return 0
	case "describe", "open", "run", "observe", "close", "verify", "install-tools":
		return runEngine("workbench.py", args, stdout, stderr)
	case "check":
		if len(args) < 2 || args[1] != "boundaries" {
			return cliError(stdout, "check requires boundaries")
		}
		return runEngine("workbench.py", args, stdout, stderr)
	case "collect":
		if len(args) < 2 || args[1] != "changes" {
			return cliError(stdout, "collect requires changes")
		}
		return runEngine("workbench.py", args, stdout, stderr)
	default:
		return cliError(stdout, "unknown command %q", args[0])
	}
}

func cliError(stderr io.Writer, format string, values ...any) int {
	_ = json.NewEncoder(stderr).Encode(map[string]any{"schema_version": 1, "error": fmt.Sprintf(format, values...)})
	return 2
}

func usageText() string {
	return `usage: harnessctl <command>

commands:
  version
  describe | open | run | observe | close
  verify change|candidate|release
  check boundaries
  collect changes
  install-tools
`

}

func runEngine(script string, arguments []string, stdout, stderr io.Writer) int {
	repo, forwarded, err := projectRoot(arguments)
	if err != nil {
		return cliError(stdout, "%v", err)
	}
	if err := verifyProjectLock(repo); err != nil {
		return cliError(stdout, "%v", err)
	}
	runtimeRoot, err := os.MkdirTemp("", "harnessctl-engine-")
	if err != nil {
		return cliError(stdout, "create engine runtime: %v", err)
	}
	defer os.RemoveAll(runtimeRoot)
	if err := extractEngine(runtimeRoot); err != nil {
		return cliError(stdout, "extract engine runtime: %v", err)
	}

	command := exec.Command(filepath.Join(runtimeRoot, "scripts", script), forwarded...)
	command.Dir = repo
	command.Stdout = stdout
	command.Stderr = stderr

	executable, err := os.Executable()
	if err != nil {
		return cliError(stdout, "resolve engine executable: %v", err)
	}
	executableBytes, err := os.ReadFile(executable)
	if err != nil {
		return cliError(stdout, "read engine identity: %v", err)
	}
	engineDigest := fmt.Sprintf("%x", sha256.Sum256(executableBytes))
	command.Env = append(os.Environ(),
		"HARNESS_PROJECT_ROOT="+repo,
		"HARNESS_ENGINE_DIR="+filepath.Join(runtimeRoot, "scripts"),
		"HARNESS_EXTERNAL_ENGINE=1",
		"HARNESSCTL_VERSION="+Version,
		"HARNESS_ENGINE_SHA256="+engineDigest,
		"HARNESSCTL_OWNER_PID="+fmt.Sprint(os.Getpid()),
	)
	if err := runSignaledCommand(command); err != nil {
		if exitError, ok := err.(*exec.ExitError); ok {
			return exitError.ExitCode()
		}
		return cliError(stdout, "execute engine: %v", err)
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

func decodeStrictJSON(raw []byte, destination any) error {
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("multiple JSON values")
		}
		return err
	}
	return nil
}
