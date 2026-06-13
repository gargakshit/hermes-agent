"""Unit tests for the Sprites terminal environment backend."""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock

import pytest


class _DummySyncManager:
    def __init__(self, *args, **kwargs):
        self.sync_calls = []

    def sync(self, *, force=False):
        self.sync_calls.append(force)


class _FakeClient:
    def __init__(self, *, token="tok", api_base="https://api.sprites.dev"):
        self.token = token
        self.api_base = api_base
        self.created = []
        self.deleted = []
        self.killed = []
        self.closed = False
        self.sprite = None
        self.ws_factory = None
        self.writes = []

    def get_sprite(self, name):
        return self.sprite

    def create_sprite(self, name):
        self.created.append(name)
        self.sprite = {"name": name, "status": "cold"}
        return self.sprite

    def delete_sprite(self, name):
        self.deleted.append(name)

    def close(self):
        self.closed = True

    def exec_ws(self, name, cmd):
        return self.ws_factory(name, cmd)

    def kill_exec_session(self, name, session_id, signal="SIGTERM"):
        self.killed.append((name, session_id, signal))

    def fs_write(self, *args, **kwargs):
        self.writes.append((args, kwargs))
        return {"path": args[1], "size": len(args[2]), "mode": "0644"}


class _ListWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __iter__(self):
        return iter(self.messages)

    def close(self):
        self.closed = True


class _BlockingWebSocket:
    def __init__(self):
        self.closed = threading.Event()
        self.sent_session = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __iter__(self):
        return self

    def __next__(self):
        if not self.sent_session:
            self.sent_session = True
            return json.dumps({"type": "session_info", "session_id": "sess-1"})
        self.closed.wait(timeout=2)
        raise StopIteration

    def close(self):
        self.closed.set()


@pytest.fixture()
def sprites_module(monkeypatch):
    monkeypatch.setenv("SPRITES_TOKEN", "tok")
    import tools.environments.sprites as mod

    monkeypatch.setattr(mod, "FileSyncManager", _DummySyncManager)
    monkeypatch.setattr(mod.SpritesEnvironment, "init_session", lambda self: None)
    monkeypatch.setattr(mod, "_get_active_profile_name", lambda: "default")
    return mod


def _make_env(sprites_module, monkeypatch, *, client=None, home="/home/sprite", **kwargs):
    client = client or _FakeClient()
    monkeypatch.setattr(sprites_module, "_SpritesClient", MagicMock(return_value=client))
    monkeypatch.setattr(sprites_module.SpritesEnvironment, "_detect_home", lambda self: home)
    env = sprites_module.SpritesEnvironment(**kwargs)
    env._fake_client = client
    return env


def test_create_if_missing_flow(sprites_module, monkeypatch):
    client = _FakeClient()
    env = _make_env(sprites_module, monkeypatch, client=client, task_id="task-1")

    assert client.created == ["u79bead8e6d-hermes-default-task-1"]
    assert env.sprite_name == "u79bead8e6d-hermes-default-task-1"


def test_reuse_existing_sprite(sprites_module, monkeypatch):
    client = _FakeClient()
    client.sprite = {"name": "u79bead8e6d-hermes-default-default", "status": "cold"}

    env = _make_env(sprites_module, monkeypatch, client=client)

    assert client.created == []
    assert env.sprite_name == "u79bead8e6d-hermes-default-default"


def test_detected_home_updates_default_cwd(sprites_module, monkeypatch):
    env = _make_env(sprites_module, monkeypatch, home="/home/testuser")

    assert env.cwd == "/home/testuser"


def test_explicit_cwd_is_not_overridden(sprites_module, monkeypatch):
    env = _make_env(
        sprites_module,
        monkeypatch,
        home="/home/testuser",
        cwd="/workspace",
    )

    assert env.cwd == "/workspace"


def test_run_bash_streams_stdout_and_exit_code(sprites_module, monkeypatch):
    client = _FakeClient()
    client.ws_factory = lambda name, cmd: _ListWebSocket(
        [
            json.dumps({"type": "session_info", "session_id": "sess-42"}),
            b"\x01hello\n",
            json.dumps({"type": "exit", "exit_code": 0}),
        ]
    )
    env = _make_env(sprites_module, monkeypatch, client=client)

    proc = env._run_bash("echo hello")
    result = env._wait_for_process(proc, timeout=5)

    assert result["output"] == "hello\n"
    assert result["returncode"] == 0
    assert proc.session_id == "sess-42"


def test_bulk_upload_creates_remote_parents_and_writes_files(
    sprites_module,
    monkeypatch,
    tmp_path,
):
    client = _FakeClient()
    env = _make_env(sprites_module, monkeypatch, client=client)
    mkdir_commands = []
    host_file = tmp_path / "skill.md"
    host_file.write_text("sprite data", encoding="utf-8")
    remote_file = "/home/sprite/.hermes/skills/skill.md"

    monkeypatch.setattr(
        env,
        "_run_direct",
        lambda command, *, timeout=30: mkdir_commands.append((command, timeout))
        or {"output": "", "returncode": 0},
    )

    env._sprites_bulk_upload([(str(host_file), remote_file)])

    assert mkdir_commands
    assert mkdir_commands[0][0].startswith("mkdir -p ")
    assert "/home/sprite/.hermes/skills" in mkdir_commands[0][0]
    assert client.writes[0][0][1] == remote_file
    assert client.writes[0][0][2] == b"sprite data"


def test_kill_calls_exec_session_kill(sprites_module, monkeypatch):
    client = _FakeClient()
    ws = _BlockingWebSocket()
    client.ws_factory = lambda name, cmd: ws
    env = _make_env(sprites_module, monkeypatch, client=client)

    proc = env._run_bash("sleep 300")
    deadline = time.monotonic() + 2
    while proc.session_id is None and time.monotonic() < deadline:
        time.sleep(0.01)

    proc.kill()
    proc.wait(timeout=2)

    assert client.killed == [(env.sprite_name, "sess-1", "SIGTERM")]
    assert proc.returncode == 130


def test_persistent_cleanup_does_not_delete_sprite(sprites_module, monkeypatch):
    client = _FakeClient()
    env = _make_env(sprites_module, monkeypatch, client=client, persistent_filesystem=True)

    env.cleanup()

    assert client.deleted == []
    assert client.closed is True


def test_non_persistent_cleanup_deletes_sprite(sprites_module, monkeypatch):
    client = _FakeClient()
    env = _make_env(sprites_module, monkeypatch, client=client, persistent_filesystem=False)

    env.cleanup()

    assert client.deleted == [env.sprite_name]


def test_sprite_name_is_bounded_and_deterministic(sprites_module, monkeypatch):
    monkeypatch.setattr(sprites_module, "_get_active_profile_name", lambda: "Research Profile")

    name1 = sprites_module._build_sprite_name("Hermes", "Task With Spaces And A Very Long Tail")
    name2 = sprites_module._build_sprite_name("Hermes", "Task With Spaces And A Very Long Tail")

    assert name1 == name2
    assert len(name1) <= 63
    assert name1.startswith("u79bead8e6d-hermes-research-profile-task-with-spaces")


def test_explicit_namespace_wins_over_token_hash(sprites_module, monkeypatch):
    monkeypatch.setattr(sprites_module, "_get_active_profile_name", lambda: "Research Profile")

    name = sprites_module._build_sprite_name(
        "Hermes",
        "Task With Spaces",
        namespace="Acme Team",
    )

    assert name == "acme-team-hermes-research-profile-task-with-spaces"
