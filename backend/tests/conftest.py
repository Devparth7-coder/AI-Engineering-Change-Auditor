"""Shared pytest configuration for the backend unit tests.

These tests never touch the network, never require GitLab/Duo credentials and
never depend on the development database: every test that needs state gets a
fresh temporary SQLite file or plain in-memory objects.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Must be set before any app.* import so the settings singleton picks them up.
_TMPDB = Path(tempfile.mkdtemp(prefix="eca-unit-")) / "unit.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ["DUO_ENABLED"] = "false"
os.environ["GITLAB_URL"] = ""
os.environ["GITLAB_TOKEN"] = ""
os.environ["DEMO_ENABLED"] = "true"
os.environ["SANDBOX_KEEP_WORKSPACES"] = "false"
