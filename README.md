# harnessctl — retired execution path

The native-delivery Harness no longer installs or invokes this engine. Projects own their native startup, development checks and independent product acceptance. The canonical [Go template](https://github.com/moss-site/ai-first-go-template) and source-only [Template Sync](https://github.com/Fueav/harness-template-sync) own scaffold delivery; neither wraps project execution.

This repository preserves the former engine and workbench implementation for historical audit. The v1 release candidate is not the replacement runtime. There is no forwarding command, new compatibility layer or new engine release in the native path.

Existing source, tests, documents and experimental commits remain available. CI runs `python3 scripts/check_retirement.py` to verify byte preservation and retired distribution. An explicit historical investigation may run the original tests; those results do not validate native delivery.

The native path is validated in the actual target project, against a fixed candidate and a separately controlled acceptance package. Its result is a product result, not an engine receipt.
