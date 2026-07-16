package harnessctl

import "runtime/debug"

func ResolveVersion(linkerValue string) string {
	if linkerValue != "" && linkerValue != "dev" {
		return linkerValue
	}
	if info, ok := debug.ReadBuildInfo(); ok && info.Main.Version != "" && info.Main.Version != "(devel)" {
		return info.Main.Version
	}
	return "dev"
}
