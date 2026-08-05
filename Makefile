SHELL := /usr/bin/env bash
VERSION ?= v0.4.0

.PHONY: build test lint

build:
	go build -ldflags "-X main.version=$(VERSION)" ./cmd/harnessctl

test:
	go test ./...
	scripts/run_checker_self_tests.sh

lint:
	@unformatted="$$(gofmt -l .)"; \
	if [[ -n "$$unformatted" ]]; then printf 'gofmt needed:\n%s\n' "$$unformatted" >&2; exit 1; fi
	go vet ./...
