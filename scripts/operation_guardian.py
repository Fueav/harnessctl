#!/usr/bin/env python3
"""Keep the owned process-group leader alive until all descendants are stopped."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.evidence import atomic_json


def main():
    ready_fd, result_path, command = int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
    # The supervisor durably records our identity before releasing this gate.
    permission = os.read(ready_fd, 1)
    os.close(ready_fd)
    if permission != b'1':
        return 125
    signal.signal(signal.SIGTERM, lambda *_: None)
    signal.signal(signal.SIGINT, lambda *_: None)
    try:
        process = subprocess.Popen(command)
        code = process.wait()
        code = code if code >= 0 else 128 - code
    except OSError as error:
        print('operation executable unavailable: ' + str(error.errno), file=sys.stderr)
        code = 127
    atomic_json(result_path, {'exit_code': code})
    # SIGKILL by the supervisor terminates this still-identifiable group,
    # including descendants which outlive the immediate command.
    while True:
        time.sleep(1)


if __name__ == '__main__':
    raise SystemExit(main())
