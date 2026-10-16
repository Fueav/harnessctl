#!/usr/bin/env python3
"""Streaming output cannot expose a credential across arbitrary read boundaries."""
from pathlib import Path
import tempfile
import unittest
from workbench import drain


class Chunks:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def read1(self, _size):
        return next(self.chunks, b'')


class RedactionTests(unittest.TestCase):
    def redact(self, chunks, secrets):
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / 'output'
            drain(Chunks(chunks), destination, sorted(secrets, key=len, reverse=True))
            return destination.read_bytes()

    def test_adjacent_secret_split_after_first_replacement(self):
        secret = b'S' * 64
        self.assertEqual(self.redact([b'S' * 100, b'S' * 28], [secret]), b'[REDACTED][REDACTED]')

    def test_every_split_of_multiple_credentials(self):
        secret = b'0123456789abcdef' * 2
        dsn = b'postgres://user:' + secret + b'@127.0.0.1/database'
        raw = b'first=' + secret + b' second=' + dsn + b'\n' + secret + secret + b'\ndone\n'
        expected = b'first=[REDACTED] second=[REDACTED]\n[REDACTED][REDACTED]\ndone\n'
        for size in range(1, len(raw) + 1):
            with self.subTest(chunk_size=size):
                chunks = [raw[offset:offset + size] for offset in range(0, len(raw), size)]
                self.assertEqual(self.redact(chunks, [secret, dsn]), expected)


if __name__ == '__main__':
    unittest.main()
