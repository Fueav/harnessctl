SHELL := /usr/bin/env bash
VERSION ?= v1.0.0-rc.1

.PHONY: build test lint integration sandbox-integration

build:
	go build -ldflags "-X main.version=$(VERSION)" ./cmd/harnessctl

test:
	go test ./...
	scripts/run_checker_self_tests.sh

lint:
	@unformatted="$$(gofmt -l .)"; \
	if [[ -n "$$unformatted" ]]; then printf 'gofmt needed:\n%s\n' "$$unformatted" >&2; exit 1; fi
	go vet ./...

integration:
	python3 scripts/workbench_docker_test.py

sandbox-integration:
	python3 scripts/workbench_sandbox_test.py
