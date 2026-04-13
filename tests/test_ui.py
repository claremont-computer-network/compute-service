"""
Tests for the /ui static file mount.

Covers:
- GET /ui returns 200 and serves HTML when the ui/ directory is present.
- The app starts cleanly when the ui/ directory is absent (mount skipped).
"""
import os
import sys
from unittest.mock import patch


def test_ui_serves_index(api_client):
    """GET /ui/ returns 200 and the HTML page."""
    resp = api_client.get("/ui/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert b"CaaS" in resp.content


def test_ui_mount_skipped_when_directory_absent(mock_docker_client):
    """App imports cleanly and the 'ui' mount is absent when ui/ does not exist."""
    # Force a fresh import so the conditional mount logic re-runs.
    for mod_name in list(sys.modules):
        if "app.main" in mod_name:
            del sys.modules[mod_name]

    real_isdir = os.path.isdir

    def fake_isdir(path):
        if os.path.basename(os.path.normpath(path)) == "ui":
            return False
        return real_isdir(path)

    with patch("docker.from_env", return_value=mock_docker_client), \
         patch("os.path.isdir", side_effect=fake_isdir):
        import app.main as main_module

    routes = {r.name for r in main_module.app.routes if hasattr(r, "name")}
    assert main_module.app is not None
    assert "ui" not in routes
