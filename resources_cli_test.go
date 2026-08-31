package harnessctl

import (
	"bytes"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestEnginePropagatesInvokingProcessToNestedResourceSupervisors(t *testing.T) {
	repo := t.TempDir()
	if err := os.Mkdir(filepath.Join(repo, "harness"), 0o700); err != nil {
		t.Fatal(err)
	}
	for name, contents := range map[string]string{
		"harness.lock":      `{"schema_version":1,"module":"github.com/Fueav/harnessctl","version":"dev"}`,
		"dependencies.json": `{"enabled":false}`,
	} {
		if err := os.WriteFile(filepath.Join(repo, "harness", name), []byte(contents), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	var output, errOut bytes.Buffer
	code := Run([]string{"resources", "run", "--repo", repo, "--", "python3", "-c", "import os; print(os.environ.get('HARNESSCTL_OWNER_PID', 'missing'))"}, &output, &errOut)
	if code != 0 || strings.TrimSpace(output.String()) != fmt.Sprint(os.Getpid()) {
		t.Fatalf("invoking process was not propagated: code=%d output=%q error=%q", code, output.String(), errOut.String())
	}
}

func TestResourceLifecycleIsDiscoverable(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := Run([]string{"help"}, &out, &errOut); code != 0 {
		t.Fatalf("help: %d %s", code, errOut.String())
	}
	if !strings.Contains(out.String(), "resources run|status|gc") {
		t.Fatal("resource lifecycle commands are not discoverable")
	}
}

func TestResourceCommandForwardsSignalsAndWaitsForCleanup(t *testing.T) {
	if repo := os.Getenv("HARNESS_SIGNAL_TEST_REPO"); repo != "" {
		code := `import pathlib,signal,sys,time
def finish(number, frame):
 time.sleep(0.1)
 pathlib.Path('cleaned').write_text('done')
 sys.exit(128+number)
signal.signal(signal.SIGINT,finish)
signal.signal(signal.SIGTERM,finish)
pathlib.Path('ready').write_text('ready')
time.sleep(30)`
		os.Exit(Run([]string{"resources", "run", "--repo", repo, "--", "python3", "-c", code}, os.Stdout, os.Stderr))
	}
	for _, sig := range []syscall.Signal{syscall.SIGINT, syscall.SIGTERM} {
		t.Run(sig.String(), func(t *testing.T) {
			repo := t.TempDir()
			if err := os.Mkdir(filepath.Join(repo, "harness"), 0o700); err != nil {
				t.Fatal(err)
			}
			for path, content := range map[string]string{
				"harness.lock":      `{"schema_version":1,"module":"github.com/Fueav/harnessctl","version":"dev"}`,
				"dependencies.json": `{"enabled":false}`,
			} {
				if err := os.WriteFile(filepath.Join(repo, "harness", path), []byte(content), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			command := exec.Command(os.Args[0], "-test.run=^TestResourceCommandForwardsSignalsAndWaitsForCleanup$")
			command.WaitDelay = 2 * time.Second
			command.Env = append(os.Environ(), "HARNESS_SIGNAL_TEST_REPO="+repo)
			var output bytes.Buffer
			command.Stdout, command.Stderr = &output, &output
			if err := command.Start(); err != nil {
				t.Fatal(err)
			}
			defer command.Process.Kill()
			deadline := time.Now().Add(10 * time.Second)
			for {
				if _, err := os.Stat(filepath.Join(repo, "ready")); err == nil {
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("resource command did not become ready")
				}
				time.Sleep(10 * time.Millisecond)
			}
			if err := command.Process.Signal(sig); err != nil {
				t.Fatal(err)
			}
			_ = command.Wait()
			if code := command.ProcessState.ExitCode(); code != 128+int(sig) {
				t.Fatalf("exit code %d, want %d; %s", code, 128+int(sig), output.String())
			}
			if _, err := os.Stat(filepath.Join(repo, "cleaned")); err != nil {
				t.Fatal("CLI returned before child cleanup completed")
			}
		})
	}
}

func TestResourceLifecycleRejectsUnknownOperation(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := Run([]string{"resources", "prune-all"}, &out, &errOut); code != 2 {
		t.Fatalf("unknown operation: %d", code)
	}
	if !strings.Contains(errOut.String(), "resources requires run, status, or gc") {
		t.Fatalf("expected fail-closed resource routing, got %s", errOut.String())
	}
}
