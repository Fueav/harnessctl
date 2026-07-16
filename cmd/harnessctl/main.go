package main

import (
	"os"

	"github.com/Fueav/harnessctl"
)

var version = "dev"

func main() {
	harnessctl.Version = harnessctl.ResolveVersion(version)
	os.Exit(harnessctl.Run(os.Args[1:], os.Stdout, os.Stderr))
}
