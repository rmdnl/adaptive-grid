"""Shared pytest configuration for the adaptive-grid test suite.

On some Windows environments pytest's built-in numbered-temp-directory
cleanup (``cleanup_dead_symlinks`` / ``cleanup_numbered_dir``) raises
``OSError: [WinError 448]`` because the temp path traverses an untrusted
mount point.  That teardown error is purely a pytest housekeeping issue
-- it is unrelated to application behaviour and can abort the session
before the summary line is printed.

To keep the test suite deterministic on Windows we provide a custom
``tmp_path`` fixture backed by a workspace-local directory and disable
the built-in numbered-dir cleanup by clearing the basetemp reference
before pytest's own ``pytest_sessionfinish`` runs.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


_WORKSPACE_TMP = Path(__file__).resolve().parent / ".pytest-tmp"


@pytest.fixture
def tmp_path():
    """Workspace-local temp directory that avoids Windows mount-point errors.

    Each test gets a unique subdirectory under ``.pytest-tmp``.  This
    replaces pytest's default ``tmp_path`` so the built-in numbered-dir
    cleanup (which triggers WinError 448) never runs on these paths.
    """
    _WORKSPACE_TMP.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="test_", dir=str(_WORKSPACE_TMP)))
    yield path
    shutil.rmtree(str(path), ignore_errors=True)


def pytest_sessionfinish(session, exitstatus):
    """Clear pytest's basetemp so the built-in numbered-dir cleanup is a no-op.

    The default cleanup calls ``Path.resolve()`` which can raise
    ``WinError 448`` on Windows with untrusted mount points.  By nilling
    the basetemp reference we prevent that path entirely.
    """
    factory = getattr(session.config, "_tmp_path_factory", None)
    if factory is not None:
        try:
            factory._basetemp = None
        except Exception:
            pass
