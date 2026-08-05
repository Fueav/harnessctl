package harnessctl

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
)

type scaffoldManagedPath struct {
	Path             string `json:"path"`
	Strategy         string `json:"strategy"`
	Required         *bool  `json:"required,omitempty"`
	Target           string `json:"target,omitempty"`
	VersionField     string `json:"version_field,omitempty"`
	FilterSymlinks   bool   `json:"filter_symlinks,omitempty"`
	WhenTargetExists bool   `json:"when_target_exists,omitempty"`
}

type scaffoldManifest struct {
	SchemaVersion int                   `json:"schema_version"`
	ManagedPaths  []scaffoldManagedPath `json:"managed_paths"`
	RetiredPaths  []string              `json:"retired_paths"`
}

type scaffoldResolution struct {
	Path           string `json:"path"`
	Strategy       string `json:"strategy"`
	Resolution     string `json:"resolution"`
	TemplateSHA256 string `json:"template_sha256"`
	TargetSHA256   string `json:"target_sha256"`
}

type scaffoldLock struct {
	SchemaVersion  int                  `json:"schema_version"`
	TemplateCommit string               `json:"template_commit"`
	ManifestSHA256 string               `json:"manifest_sha256"`
	ResolvedPaths  []scaffoldResolution `json:"resolved_paths"`
}

type scaffoldPathReport struct {
	Path     string `json:"path"`
	Strategy string `json:"strategy"`
	Status   string `json:"status"`
	Detail   string `json:"detail,omitempty"`
}

type scaffoldAuditReport struct {
	SchemaVersion  int                  `json:"schema_version"`
	Status         string               `json:"status"`
	Converged      bool                 `json:"converged"`
	TemplateCommit string               `json:"template_commit"`
	ManifestSHA256 string               `json:"manifest_sha256"`
	TargetDirty    bool                 `json:"target_dirty"`
	Paths          []scaffoldPathReport `json:"paths"`
	Errors         []string             `json:"errors"`
}

func runScaffoldAudit(arguments []string, stdout, stderr io.Writer) int {
	parser := flag.NewFlagSet("scaffold audit", flag.ContinueOnError)
	parser.SetOutput(stderr)
	templateArgument := parser.String("template", "", "clean template checkout")
	targetArgument := parser.String("repo", ".", "target repository")
	if err := parser.Parse(arguments); err != nil {
		return 2
	}
	if parser.NArg() != 0 || strings.TrimSpace(*templateArgument) == "" {
		fmt.Fprintln(stderr, "harnessctl: scaffold audit requires --template and accepts no positional arguments")
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
	report, err := auditScaffold(template, target)
	if err != nil {
		fmt.Fprintf(stderr, "harnessctl: scaffold audit: %v\n", err)
		return 2
	}
	encoder := json.NewEncoder(stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(report); err != nil {
		fmt.Fprintf(stderr, "harnessctl: write scaffold audit: %v\n", err)
		return 2
	}
	if report.Converged {
		return 0
	}
	return 1
}

func checkedScaffoldRoot(argument, label string) (string, error) {
	resolved, err := filepath.Abs(argument)
	if err != nil {
		return "", fmt.Errorf("resolve %s root: %w", label, err)
	}
	info, err := os.Lstat(resolved)
	if err != nil {
		return "", fmt.Errorf("read %s root: %w", label, err)
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return "", fmt.Errorf("%s root must be a non-symlink directory", label)
	}
	return resolved, nil
}

func auditScaffold(template, target string) (scaffoldAuditReport, error) {
	report := scaffoldAuditReport{SchemaVersion: 1, Status: "drift", Paths: []scaffoldPathReport{}, Errors: []string{}}
	clean, err := gitOutput(template, "status", "--porcelain=v1", "--untracked-files=all")
	if err != nil {
		return report, fmt.Errorf("inspect template Git state: %w", err)
	}
	if clean != "" {
		return report, errors.New("template checkout must be clean")
	}
	commit, err := gitOutput(template, "rev-parse", "--verify", "HEAD^{commit}")
	if err != nil || len(commit) != 40 {
		return report, errors.New("template HEAD must resolve to a commit")
	}
	report.TemplateCommit = commit
	targetState, err := gitOutput(target, "status", "--porcelain=v1", "--untracked-files=all")
	if err != nil {
		return report, fmt.Errorf("inspect target Git state: %w", err)
	}
	report.TargetDirty = targetState != ""

	manifestRaw, err := readScaffoldRegular(template, "harness/scaffold_manifest.json")
	if err != nil {
		return report, fmt.Errorf("read template manifest: %w", err)
	}
	report.ManifestSHA256 = sha256Hex(manifestRaw)
	var manifest scaffoldManifest
	if err := decodeStrictJSON(manifestRaw, &manifest); err != nil || manifest.SchemaVersion != 1 {
		return report, errors.New("template scaffold manifest has an invalid contract")
	}

	lock, lockErr := loadScaffoldLock(target)
	resolutions := map[string]scaffoldResolution{}
	if lockErr != nil {
		report.Errors = append(report.Errors, lockErr.Error())
	} else {
		if lock.TemplateCommit != commit {
			report.Errors = append(report.Errors, "scaffold lock template commit is stale")
		}
		if lock.ManifestSHA256 != report.ManifestSHA256 {
			report.Errors = append(report.Errors, "scaffold lock manifest digest is stale")
		}
		for _, resolution := range lock.ResolvedPaths {
			if _, exists := resolutions[resolution.Path]; exists {
				report.Errors = append(report.Errors, "scaffold lock has duplicate resolution: "+resolution.Path)
				continue
			}
			resolutions[resolution.Path] = resolution
		}
	}

	seen := map[string]bool{}
	resolutionPaths := map[string]bool{}
	for _, managed := range manifest.ManagedPaths {
		pathReport := scaffoldPathReport{Path: managed.Path, Strategy: managed.Strategy, Status: "drift"}
		if err := validateScaffoldManaged(managed, seen); err != nil {
			return report, err
		}
		required := managed.Required == nil || *managed.Required
		conditionalTargetExists := false
		if managed.WhenTargetExists {
			conditionalTarget, _ := scaffoldSymlinkTarget(managed.Path, managed.Target)
			conditionalTargetExists = scaffoldPathExists(target, conditionalTarget)
			required = conditionalTargetExists
		}
		templateDigest, templateExists, templateErr := scaffoldPathDigest(template, managed.Path)
		targetDigest, targetExists, targetErr := scaffoldPathDigest(target, managed.Path)
		if templateErr != nil && managed.Strategy != "symlink" {
			return report, fmt.Errorf("template path %s: %w", managed.Path, templateErr)
		}
		if targetErr != nil && managed.Strategy != "symlink" {
			report.Errors = append(report.Errors, fmt.Sprintf("%s is not a regular file", managed.Path))
			report.Paths = append(report.Paths, pathReport)
			continue
		}
		if !required && !templateExists && !targetExists {
			pathReport.Status = "optional_missing"
			report.Paths = append(report.Paths, pathReport)
			continue
		}
		if !targetExists {
			if required {
				report.Errors = append(report.Errors, "required managed path is missing: "+managed.Path)
				pathReport.Detail = "required target path is missing"
			} else {
				pathReport.Status = "optional_missing"
			}
			report.Paths = append(report.Paths, pathReport)
			continue
		}
		if managed.WhenTargetExists && !conditionalTargetExists {
			report.Errors = append(report.Errors, "conditional symlink target is missing: "+managed.Path)
			pathReport.Detail = "link exists but its conditional target is absent"
			report.Paths = append(report.Paths, pathReport)
			continue
		}

		switch managed.Strategy {
		case "copy":
			if !templateExists || templateDigest != targetDigest {
				report.Errors = append(report.Errors, "copied path differs from template: "+managed.Path)
				pathReport.Detail = "content digest differs"
			} else {
				pathReport.Status = "exact"
			}
		case "symlink":
			link, err := os.Readlink(filepath.Join(target, filepath.FromSlash(managed.Path)))
			if err != nil || link != managed.Target {
				report.Errors = append(report.Errors, "managed symlink is invalid: "+managed.Path)
				pathReport.Detail = "link target differs"
			} else {
				pathReport.Status = "exact"
			}
		case "manual_merge", "project_overlay":
			resolutionPaths[managed.Path] = true
			if !templateExists {
				return report, fmt.Errorf("template managed path is missing: %s", managed.Path)
			}
			if managed.VersionField != "" {
				if err := requireOverlayVersion(template, target, managed); err != nil {
					report.Errors = append(report.Errors, err.Error())
					pathReport.Detail = err.Error()
					report.Paths = append(report.Paths, pathReport)
					continue
				}
			}
			resolution, ok := resolutions[managed.Path]
			if !ok || resolution.Strategy != managed.Strategy ||
				resolution.TemplateSHA256 != templateDigest || resolution.TargetSHA256 != targetDigest ||
				!validResolution(resolution.Resolution) {
				report.Errors = append(report.Errors, "managed path lacks a current semantic resolution: "+managed.Path)
				pathReport.Detail = "scaffold lock resolution is missing or stale"
			} else {
				pathReport.Status = "resolved"
				pathReport.Detail = resolution.Resolution
			}
		default:
			return report, fmt.Errorf("unsupported scaffold strategy %q", managed.Strategy)
		}
		report.Paths = append(report.Paths, pathReport)
	}
	for path := range resolutions {
		if !resolutionPaths[path] {
			report.Errors = append(report.Errors, "scaffold lock has an unknown resolution: "+path)
		}
	}

	retiredSeen := map[string]bool{}
	for _, retired := range manifest.RetiredPaths {
		if !validScaffoldRelative(retired) || retiredSeen[retired] {
			return report, errors.New("template scaffold manifest has an invalid retired path")
		}
		retiredSeen[retired] = true
		if scaffoldPathExists(target, retired) {
			report.Paths = append(report.Paths, scaffoldPathReport{Path: retired, Strategy: "retired", Status: "retired_present"})
			report.Errors = append(report.Errors, "retired path is still present: "+retired)
		}
	}
	sort.Slice(report.Paths, func(i, j int) bool { return report.Paths[i].Path < report.Paths[j].Path })
	sort.Strings(report.Errors)
	report.Converged = len(report.Errors) == 0
	if report.Converged {
		report.Status = "passed"
	}
	return report, nil
}

func validateScaffoldManaged(managed scaffoldManagedPath, seen map[string]bool) error {
	if !validScaffoldRelative(managed.Path) || seen[managed.Path] {
		return errors.New("template scaffold manifest has an invalid or duplicate managed path")
	}
	seen[managed.Path] = true
	switch managed.Strategy {
	case "copy", "manual_merge", "project_overlay":
		if managed.Target != "" || managed.WhenTargetExists {
			return errors.New("template scaffold manifest has symlink fields on a non-symlink path")
		}
	case "symlink":
		if managed.Target == "" {
			return errors.New("template scaffold manifest has a symlink without a target")
		}
		if _, ok := scaffoldSymlinkTarget(managed.Path, managed.Target); !ok {
			return errors.New("template scaffold manifest has an unsafe symlink target")
		}
		if managed.WhenTargetExists && (managed.Required == nil || *managed.Required) {
			return errors.New("template scaffold manifest has a required conditional symlink")
		}
	default:
		return fmt.Errorf("unsupported scaffold strategy %q", managed.Strategy)
	}
	if managed.VersionField != "" && managed.Strategy != "project_overlay" {
		return errors.New("template scaffold manifest has a version field on a non-overlay path")
	}
	if managed.FilterSymlinks && managed.Strategy != "project_overlay" {
		return errors.New("template scaffold manifest has a symlink filter on a non-overlay path")
	}
	return nil
}

func scaffoldSymlinkTarget(link, target string) (string, bool) {
	if target == "" || filepath.IsAbs(target) || strings.Contains(target, "\\") || strings.ContainsRune(target, 0) {
		return "", false
	}
	resolved := filepath.ToSlash(filepath.Clean(filepath.Join(
		filepath.Dir(filepath.FromSlash(link)), filepath.FromSlash(target),
	)))
	if resolved == "." || strings.HasPrefix(resolved, "../") {
		return "", false
	}
	return resolved, true
}

func validScaffoldRelative(value string) bool {
	if value == "" || strings.Contains(value, "\\") || strings.ContainsRune(value, 0) || filepath.IsAbs(value) {
		return false
	}
	clean := filepath.ToSlash(filepath.Clean(filepath.FromSlash(value)))
	return clean == value && value != "." && !strings.HasPrefix(value, "../")
}

func scaffoldPathDigest(root, relative string) (string, bool, error) {
	path := filepath.Join(root, filepath.FromSlash(relative))
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return "", true, errors.New("path is a symlink")
	}
	if !info.Mode().IsRegular() {
		return "", true, errors.New("path is not regular")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", true, err
	}
	return sha256Hex(raw), true, nil
}

func scaffoldPathExists(root, relative string) bool {
	_, err := os.Lstat(filepath.Join(root, filepath.FromSlash(relative)))
	return err == nil
}

func readScaffoldRegular(root, relative string) ([]byte, error) {
	_, exists, err := scaffoldPathDigest(root, relative)
	if err != nil || !exists {
		if err != nil {
			return nil, err
		}
		return nil, os.ErrNotExist
	}
	return os.ReadFile(filepath.Join(root, filepath.FromSlash(relative)))
}

func sha256Hex(raw []byte) string {
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:])
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

func loadScaffoldLock(target string) (scaffoldLock, error) {
	var lock scaffoldLock
	raw, err := readScaffoldRegular(target, "harness/scaffold.lock")
	if err != nil {
		return lock, errors.New("target is missing harness/scaffold.lock")
	}
	if err := decodeStrictJSON(raw, &lock); err != nil || lock.SchemaVersion != 1 {
		return lock, errors.New("target harness/scaffold.lock has an invalid contract")
	}
	if !validHexDigest(lock.TemplateCommit, 40) || !validHexDigest(lock.ManifestSHA256, 64) {
		return lock, errors.New("target harness/scaffold.lock has an invalid contract")
	}
	previous := ""
	for _, resolution := range lock.ResolvedPaths {
		if !validScaffoldRelative(resolution.Path) || resolution.Path <= previous ||
			(resolution.Strategy != "manual_merge" && resolution.Strategy != "project_overlay") ||
			!validResolution(resolution.Resolution) ||
			!validHexDigest(resolution.TemplateSHA256, 64) ||
			!validHexDigest(resolution.TargetSHA256, 64) {
			return lock, errors.New("target harness/scaffold.lock has an invalid contract")
		}
		previous = resolution.Path
	}
	return lock, nil
}

func validHexDigest(value string, length int) bool {
	if len(value) != length {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func validResolution(value string) bool {
	return value == "merged" || value == "preserved" || value == "adapted" || value == "relocated"
}

func requireOverlayVersion(template, target string, managed scaffoldManagedPath) error {
	readVersion := func(root string) (float64, error) {
		raw, err := readScaffoldRegular(root, managed.Path)
		if err != nil {
			return 0, err
		}
		var document map[string]any
		if err := json.Unmarshal(raw, &document); err != nil {
			return 0, err
		}
		value, ok := document[managed.VersionField].(float64)
		if !ok {
			return 0, errors.New("version field is not numeric")
		}
		return value, nil
	}
	templateVersion, err := readVersion(template)
	if err != nil {
		return fmt.Errorf("template overlay version is invalid: %s", managed.Path)
	}
	targetVersion, err := readVersion(target)
	if err != nil || targetVersion < templateVersion {
		return fmt.Errorf("target overlay version is stale: %s", managed.Path)
	}
	return nil
}

func gitOutput(root string, arguments ...string) (string, error) {
	command := exec.Command("git", append([]string{"-C", root}, arguments...)...)
	output, err := command.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("git %s: %s", strings.Join(arguments, " "), strings.TrimSpace(string(output)))
	}
	return strings.TrimSpace(string(output)), nil
}
