package harnessctl

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"sort"
	"strings"
)

type resolutionArguments []string

func (values *resolutionArguments) String() string { return strings.Join(*values, ",") }
func (values *resolutionArguments) Set(value string) error {
	*values = append(*values, value)
	return nil
}

func runScaffoldRecord(arguments []string, stdout, stderr io.Writer) int {
	parser := flag.NewFlagSet("scaffold record", flag.ContinueOnError)
	parser.SetOutput(stderr)
	templateArgument := parser.String("template", "", "clean template checkout")
	targetArgument := parser.String("repo", ".", "target repository")
	var rawResolutions resolutionArguments
	parser.Var(&rawResolutions, "resolution", "semantic path decision path=merged|preserved|adapted|relocated")
	if err := parser.Parse(arguments); err != nil {
		return 2
	}
	if parser.NArg() != 0 || strings.TrimSpace(*templateArgument) == "" {
		fmt.Fprintln(stderr, "harnessctl: scaffold record requires --template and accepts no positional arguments")
		return 2
	}
	template, err := checkedScaffoldRoot(*templateArgument, "template")
	if err != nil {
		fmt.Fprintf(stderr, "harnessctl: %v\n", err)
		return 2
	}
	target, err := checkedScaffoldRoot(*targetArgument, "target")
	if err != nil {
		fmt.Fprintf(stderr, "harnessctl: %v\n", err)
		return 2
	}
	decisions := map[string]string{}
	for _, raw := range rawResolutions {
		path, decision, found := strings.Cut(raw, "=")
		if !found || !validScaffoldRelative(path) || !validResolution(decision) || decisions[path] != "" {
			fmt.Fprintf(stderr, "harnessctl: invalid or duplicate scaffold resolution %q\n", raw)
			return 2
		}
		decisions[path] = decision
	}
	lock, err := recordScaffold(template, target, decisions)
	if err != nil {
		fmt.Fprintf(stderr, "harnessctl: scaffold record: %v\n", err)
		return 2
	}
	encoder := json.NewEncoder(stdout)
	if err := encoder.Encode(lock); err != nil {
		fmt.Fprintf(stderr, "harnessctl: write scaffold record: %v\n", err)
		return 2
	}
	return 0
}

func recordScaffold(template, target string, decisions map[string]string) (scaffoldLock, error) {
	manifest, commit, manifestDigest, err := loadScaffoldSource(template)
	lock := scaffoldLock{SchemaVersion: 1, TemplateCommit: commit, ManifestSHA256: manifestDigest, ResolvedPaths: []scaffoldResolution{}}
	if err != nil {
		return lock, err
	}
	prior := map[string]scaffoldResolution{}
	if previous, loadErr := loadScaffoldLock(target); loadErr == nil {
		for _, resolution := range previous.ResolvedPaths {
			prior[resolution.Path] = resolution
		}
	}
	seen, consumed := map[string]bool{}, map[string]bool{}
	for _, managed := range manifest.ManagedPaths {
		if err := validateScaffoldManaged(managed, seen); err != nil {
			return lock, err
		}
		if managed.Strategy != "manual_merge" && managed.Strategy != "project_overlay" {
			continue
		}
		templateDigest, templateExists, templateErr := scaffoldPathDigest(template, managed.Path)
		targetDigest, targetExists, targetErr := scaffoldPathDigest(target, managed.Path)
		if templateErr != nil || !templateExists {
			return lock, fmt.Errorf("template semantic path is invalid: %s", managed.Path)
		}
		if targetErr != nil {
			return lock, fmt.Errorf("target semantic path is invalid: %s", managed.Path)
		}
		if !targetExists {
			if managed.Required == nil || *managed.Required {
				return lock, fmt.Errorf("required semantic path is missing: %s", managed.Path)
			}
			continue
		}
		if managed.VersionField != "" {
			if err := requireOverlayVersion(template, target, managed); err != nil {
				return lock, err
			}
		}
		decision := decisions[managed.Path]
		if decision != "" {
			consumed[managed.Path] = true
		} else if previous, ok := prior[managed.Path]; ok &&
			previous.Strategy == managed.Strategy &&
			previous.TemplateSHA256 == templateDigest && previous.TargetSHA256 == targetDigest {
			decision = previous.Resolution
		} else if templateDigest == targetDigest {
			decision = "merged"
		} else {
			return lock, fmt.Errorf("semantic path requires --resolution %s=<decision>", managed.Path)
		}
		lock.ResolvedPaths = append(lock.ResolvedPaths, scaffoldResolution{
			Path: managed.Path, Strategy: managed.Strategy, Resolution: decision,
			TemplateSHA256: templateDigest, TargetSHA256: targetDigest,
		})
	}
	for path := range decisions {
		if !consumed[path] {
			return lock, fmt.Errorf("resolution does not name a present semantic path: %s", path)
		}
	}
	sort.Slice(lock.ResolvedPaths, func(i, j int) bool {
		return lock.ResolvedPaths[i].Path < lock.ResolvedPaths[j].Path
	})
	return lock, nil
}
