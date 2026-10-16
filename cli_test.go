package harnessctl

import (
	"bytes"
	"encoding/json"
	"testing"
)

func TestVersion(t *testing.T) {
	var out, err bytes.Buffer
	if code := Run([]string{"version"}, &out, &err); code != 0 || out.String() != "harnessctl dev\n" {
		t.Fatalf("%d %s %s", code, out.String(), err.String())
	}
	if ResolveVersion("v1.0.0-rc.1") != "v1.0.0-rc.1" {
		t.Fatal("version lost")
	}
}

func TestRemovedCommandsFailClosedJSON(t *testing.T) {
	for _, args := range [][]string{{"resources", "status"}, {"scaffold", "audit"}, {"evidence", "verify"}, {"approval", "finalize"}, {"workspace-preflight"}, {"unknown"}} {
		var out, err bytes.Buffer
		if code := Run(args, &out, &err); code != 2 {
			t.Fatalf("%v returned %d", args, code)
		}
		var result map[string]any
		if json.Unmarshal(out.Bytes(), &result) != nil || result["error"] == nil {
			t.Fatalf("missing protocol error %v: %s", args, out.String())
		}
	}
}

func TestStrictLockJSON(t *testing.T) {
	var value struct {
		Version string `json:"version"`
	}
	for _, raw := range []string{`{} {}`, `{"unknown":1}`} {
		if decodeStrictJSON([]byte(raw), &value) == nil {
			t.Fatalf("accepted %s", raw)
		}
	}
}
