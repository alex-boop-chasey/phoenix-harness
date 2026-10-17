"""Temp hygiene for the test suite: every directory made with tempfile.mkdtemp() and every file made
with tempfile.mkstemp() while a test runs is removed when that test ends, pass or fail, and anything
left is swept at interpreter exit.

Imported by every test module (after the sys.path setup). It wraps tempfile.mkdtemp/mkstemp to record
what they hand out and wraps unittest.TestCase.run so the sweep happens after each test, after its
tearDown and addCleanup callbacks (so servers are stopped and files released first). Test code keeps
calling tempfile as before; nothing else changes.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import time
import unittest

START = time.time()
_created: list[str] = []
_orig_mkdtemp = tempfile.mkdtemp
_orig_mkstemp = tempfile.mkstemp


def mkdtemp(*args, **kwargs) -> str:
    p = _orig_mkdtemp(*args, **kwargs)
    _created.append(p)
    return p


def mkstemp(*args, **kwargs):
    fd, p = _orig_mkstemp(*args, **kwargs)
    _created.append(p)
    return fd, p


def sweep() -> list[str]:
    """Remove every recorded directory; returns the ones that still existed."""
    removed = []
    while _created:
        p = _created.pop()
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.unlink(p)
            removed.append(p)
        except FileNotFoundError:
            pass
        except OSError:
            shutil.rmtree(p, ignore_errors=True)
            removed.append(p)
    return removed


def pending() -> list[str]:
    return list(_created)


if tempfile.mkdtemp is not mkdtemp:          # install once, however many test modules import this
    tempfile.mkdtemp = mkdtemp
    tempfile.mkstemp = mkstemp
    _orig_run = unittest.TestCase.run

    def _run(self, result=None):
        try:
            return _orig_run(self, result)
        finally:
            sweep()

    unittest.TestCase.run = _run
    atexit.register(sweep)
