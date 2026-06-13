"""Optional live integration tests for the Sprites terminal backend.

These are skipped unless SPRITES_TOKEN is set. They create a disposable
non-persistent Sprite and delete it in cleanup.
"""

from __future__ import annotations

import os
import time

import pytest


pytestmark = pytest.mark.skipif(
    not os.getenv("SPRITES_TOKEN"),
    reason="requires SPRITES_TOKEN",
)


def test_sprites_runs_command_and_persists_file():
    from tools.environments.sprites import SpritesEnvironment

    task_id = f"integration-{int(time.time())}"
    env = SpritesEnvironment(
        cwd="/home/sprite",
        timeout=30,
        persistent_filesystem=False,
        task_id=task_id,
        name_prefix="hermes-test",
    )
    try:
        result = env.execute("echo ok")
        assert result["returncode"] == 0
        assert "ok" in result["output"]

        marker = f"/tmp/hermes-sprites-{task_id}.txt"
        result = env.execute(f"printf marker > {marker}")
        assert result["returncode"] == 0

        result = env.execute(f"cat {marker}")
        assert result["returncode"] == 0
        assert "marker" in result["output"]
    finally:
        env.cleanup()
