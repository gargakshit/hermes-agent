"""Sprites cloud sandbox execution environment.

Sprites are persistent Linux environments that sleep and wake automatically.
Hermes owns only short-lived command transports and local bookkeeping; it does
not stop or hibernate persistent Sprites during cleanup.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from hashlib import sha1
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx

from tools.environments.base import BaseEnvironment
from tools.environments.file_sync import (
    FileSyncManager,
    iter_sync_files,
    quoted_mkdir_command,
    quoted_rm_command,
    unique_parent_dirs,
)

logger = logging.getLogger(__name__)

_DEFAULT_CWD = "/home/sprite"
_DEFAULT_API_BASE = "https://api.sprites.dev"
_MAX_SPRITE_NAME_LENGTH = 63
_DEFAULT_NAMESPACE_HASH_LENGTH = 10
_NAME_PART_RE = re.compile(r"[^a-z0-9-]+")
_DASH_RE = re.compile(r"-+")


def _ensure_websockets() -> None:
    """Ensure the sync WebSocket client is importable."""
    try:
        import websockets.sync.client  # noqa: F401
        return
    except ImportError:
        pass

    try:
        from tools.lazy_deps import ensure as _lazy_ensure

        _lazy_ensure("terminal.sprites", prompt=False)
    except ImportError:
        pass
    except Exception as exc:
        raise ImportError(str(exc)) from exc

    import websockets.sync.client  # noqa: F401


def _slug(value: str, *, default: str, max_len: int = 24) -> str:
    if not isinstance(value, str):
        value = ""
    cleaned = _NAME_PART_RE.sub("-", value.lower())
    cleaned = _DASH_RE.sub("-", cleaned).strip("-")
    return (cleaned or default)[:max_len].strip("-") or default


def _get_active_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or "default"
    except Exception:
        return "default"


def _namespace_from_token(token: str | None) -> str:
    token = (token or "").strip()
    if not token:
        return "default"
    digest = sha1(token.encode("utf-8")).hexdigest()[:_DEFAULT_NAMESPACE_HASH_LENGTH]
    return f"u{digest}"


def _resolve_namespace(namespace: str | None = None, *, token: str | None = None) -> str:
    configured = namespace if namespace is not None else os.getenv("TERMINAL_SPRITES_NAMESPACE", "")
    configured_slug = _slug(str(configured or ""), default="", max_len=18)
    if configured_slug:
        return configured_slug
    return _namespace_from_token(token if token is not None else os.getenv("SPRITES_TOKEN", ""))


def _build_sprite_name(
    prefix: str,
    task_id: str,
    *,
    namespace: str | None = None,
    token: str | None = None,
) -> str:
    namespace_slug = _resolve_namespace(namespace, token=token)
    prefix_slug = _slug(prefix, default="hermes", max_len=18)
    profile_slug = _slug(_get_active_profile_name(), default="default", max_len=18)
    task_slug = _slug(task_id, default="default", max_len=24)
    raw = f"{namespace_slug}-{prefix_slug}-{profile_slug}-{task_slug}"
    if len(raw) <= _MAX_SPRITE_NAME_LENGTH:
        return raw

    digest = sha1(raw.encode("utf-8")).hexdigest()[:8]
    suffix = f"-{digest}"
    budget = _MAX_SPRITE_NAME_LENGTH - len(suffix)
    # Keep all semantic pieces visible, then append a stable collision guard
    # when long namespace/profile/task names need truncation.
    namespace_budget = min(len(namespace_slug), max(8, budget // 4))
    prefix_budget = min(len(prefix_slug), max(8, budget // 4))
    profile_budget = min(len(profile_slug), max(8, budget // 4))
    task_budget = max(8, budget - namespace_budget - prefix_budget - profile_budget - 3)
    shortened = (
        f"{namespace_slug[:namespace_budget]}-"
        f"{prefix_slug[:prefix_budget]}-"
        f"{profile_slug[:profile_budget]}-"
        f"{task_slug[:task_budget]}"
    ).strip("-")
    return f"{shortened[:budget].strip('-')}{suffix}"


def _api_base_to_ws(api_base: str) -> str:
    parsed = urlsplit(api_base.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, parsed.path, "", ""))


def _quote_name(name: str) -> str:
    return quote(name, safe="")


class _SpritesClient:
    """Small REST/WebSocket client for the Sprites v1 API."""

    def __init__(self, *, token: str, api_base: str = _DEFAULT_API_BASE):
        self.token = token.strip()
        self.api_base = api_base.rstrip("/") or _DEFAULT_API_BASE
        self.ws_base = _api_base_to_ws(self.api_base)
        self._headers = {"Authorization": f"Bearer {self.token}"}
        self._client = httpx.Client(
            base_url=self.api_base,
            headers=self._headers,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = self._client.request(method, path, **kwargs)
        if response.status_code == 404:
            return response
        response.raise_for_status()
        return response

    def get_sprite(self, name: str) -> dict[str, Any] | None:
        response = self._request("GET", f"/v1/sprites/{_quote_name(name)}")
        if response.status_code == 404:
            return None
        return response.json()

    def create_sprite(self, name: str) -> dict[str, Any]:
        response = self._request("POST", "/v1/sprites", json={"name": name})
        response.raise_for_status()
        return response.json()

    def delete_sprite(self, name: str) -> None:
        response = self._request("DELETE", f"/v1/sprites/{_quote_name(name)}")
        if response.status_code not in {200, 204, 404}:
            response.raise_for_status()

    def exec_ws_url(self, name: str, cmd: list[str]) -> str:
        query = urlencode(
            [
                *[("cmd", part) for part in cmd],
                ("tty", "false"),
                ("stdin", "false"),
                ("max_run_after_disconnect", "0s"),
            ]
        )
        return f"{self.ws_base}/v1/sprites/{_quote_name(name)}/exec?{query}"

    def exec_ws(self, name: str, cmd: list[str]):
        _ensure_websockets()
        from websockets.sync.client import connect

        url = self.exec_ws_url(name, cmd)
        try:
            return connect(url, additional_headers=self._headers, open_timeout=30)
        except TypeError:
            # websockets < 14 used extra_headers. Keep compatibility with
            # distro-packaged clients even though Hermes pins 15.0.1.
            return connect(url, extra_headers=self._headers, open_timeout=30)

    def exec_post(self, name: str, cmd: list[str], *, cwd: str | None = None) -> dict:
        params: list[tuple[str, str]] = [("cmd", part) for part in cmd]
        if cwd:
            params.append(("dir", cwd))
        response = self._request(
            "POST",
            f"/v1/sprites/{_quote_name(name)}/exec",
            params=params,
        )
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            return {"stdout": response.text, "exit_code": 0}

    def kill_exec_session(
        self,
        name: str,
        session_id: str,
        signal: str = "SIGTERM",
    ) -> None:
        response = self._request(
            "POST",
            f"/v1/sprites/{_quote_name(name)}/exec/{_quote_name(str(session_id))}/kill",
            params={"signal": signal, "timeout": "10s"},
        )
        if response.status_code not in {200, 204, 404}:
            response.raise_for_status()

    def fs_write(
        self,
        name: str,
        path: str,
        data: bytes,
        working_dir: str,
        mkdir: bool = True,
    ) -> dict[str, Any]:
        response = self._request(
            "PUT",
            f"/v1/sprites/{_quote_name(name)}/fs/write",
            params={
                "path": path,
                "workingDir": working_dir,
                "mkdir": str(bool(mkdir)).lower(),
            },
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        response.raise_for_status()
        return response.json()

    def fs_read(self, name: str, path: str, working_dir: str) -> bytes:
        response = self._request(
            "GET",
            f"/v1/sprites/{_quote_name(name)}/fs/read",
            params={"path": path, "workingDir": working_dir},
        )
        if response.status_code == 404:
            raise FileNotFoundError(path)
        response.raise_for_status()
        return response.content

    def fs_delete(self, name: str, path: str, working_dir: str) -> None:
        response = self._request(
            "DELETE",
            f"/v1/sprites/{_quote_name(name)}/fs/delete",
            params={"path": path, "workingDir": working_dir},
        )
        if response.status_code not in {200, 204, 404}:
            response.raise_for_status()


class _SpritesProcessHandle:
    """ProcessHandle adapter for Sprites WebSocket exec sessions."""

    def __init__(
        self,
        *,
        client: _SpritesClient,
        sprite_name: str,
        cmd: list[str],
        on_done,
    ):
        self._client = client
        self._sprite_name = sprite_name
        self._cmd = cmd
        self._on_done = on_done
        self._done = threading.Event()
        self._returncode: int | None = None
        self._session_id: str | None = None
        self._ws = None
        self._lock = threading.Lock()
        self._killed = False

        read_fd, write_fd = os.pipe()
        self._stdout = os.fdopen(read_fd, "r", encoding="utf-8", errors="replace")
        self._write_fd = write_fd

        self._thread = threading.Thread(target=self._worker, daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def stdout(self):
        return self._stdout

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode if self._done.is_set() else None

    def wait(self, timeout: float | None = None) -> int | None:
        self._done.wait(timeout=timeout)
        return self._returncode

    def _write(self, payload: bytes) -> None:
        if not payload:
            return
        try:
            os.write(self._write_fd, payload)
        except OSError:
            pass

    def _set_returncode(self, code: int) -> None:
        self._returncode = int(code)

    def _handle_text_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            self._write(raw.encode("utf-8", errors="replace"))
            return

        msg_type = msg.get("type")
        if msg_type == "session_info":
            session_id = msg.get("session_id")
            if session_id is not None:
                self._session_id = str(session_id)
        elif msg_type == "exit":
            self._set_returncode(int(msg.get("exit_code", 0)))
        elif msg_type in {"port_opened", "port_closed"}:
            return
        elif "message" in msg:
            self._write(str(msg["message"]).encode("utf-8", errors="replace"))

    def _handle_binary_message(self, raw: bytes | bytearray | memoryview) -> None:
        data = bytes(raw)
        if not data:
            return
        stream_id, payload = data[0], data[1:]
        if stream_id in {1, 2}:  # stdout, stderr
            self._write(payload)
        elif stream_id == 3 and payload:
            # Docs describe this as an exit-code payload byte.
            self._set_returncode(payload[0])

    def _worker(self) -> None:
        try:
            with self._client.exec_ws(self._sprite_name, self._cmd) as ws:
                with self._lock:
                    self._ws = ws
                for raw in ws:
                    if isinstance(raw, str):
                        self._handle_text_message(raw)
                    else:
                        self._handle_binary_message(raw)
                    if self._returncode is not None:
                        break
        except Exception as exc:
            if not self._killed:
                self._write(f"\n[Sprites exec error: {exc}]".encode("utf-8", errors="replace"))
                self._returncode = 1
        finally:
            with self._lock:
                self._ws = None
            if self._returncode is None:
                self._returncode = 130 if self._killed else 1
            try:
                os.close(self._write_fd)
            except OSError:
                pass
            self._done.set()
            try:
                self._on_done(self)
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def kill(self) -> None:
        self._killed = True
        session_id = self._session_id
        if session_id:
            try:
                self._client.kill_exec_session(self._sprite_name, session_id)
            except Exception as exc:
                logger.debug("Sprites: failed to kill exec session %s: %s", session_id, exc)
        self.close()


class SpritesEnvironment(BaseEnvironment):
    """Sprites persistent Linux sandbox backend."""

    _stdin_mode = "heredoc"
    _snapshot_timeout = 60

    def __init__(
        self,
        cwd: str = _DEFAULT_CWD,
        timeout: int = 60,
        persistent_filesystem: bool = True,
        task_id: str = "default",
        api_base: str = _DEFAULT_API_BASE,
        namespace: str | None = None,
        name_prefix: str = "hermes",
    ):
        requested_cwd = cwd
        super().__init__(cwd=cwd, timeout=timeout)

        token = os.getenv("SPRITES_TOKEN", "").strip()
        if not token:
            raise ValueError("Sprites backend requires SPRITES_TOKEN in ~/.hermes/.env")

        self._persistent = bool(persistent_filesystem)
        self._task_id = task_id or "default"
        self._remote_home = _DEFAULT_CWD
        self._sprite_name = _build_sprite_name(
            name_prefix,
            self._task_id,
            namespace=namespace,
            token=token,
        )
        self._client = _SpritesClient(token=token, api_base=api_base)
        self._active_handles: set[_SpritesProcessHandle] = set()
        self._active_lock = threading.Lock()
        self._sync_manager: FileSyncManager | None = None

        try:
            sprite = self._client.get_sprite(self._sprite_name)
            if sprite is None:
                sprite = self._client.create_sprite(self._sprite_name)
                logger.info("Sprites: created Sprite %s", self._sprite_name)
            else:
                logger.info("Sprites: reusing Sprite %s", self._sprite_name)
            self._sprite = sprite

            home = self._detect_home()
            if home:
                self._remote_home = home
                if requested_cwd in {"", "~", _DEFAULT_CWD}:
                    self.cwd = home
            logger.info(
                "Sprites: resolved home to %s, cwd to %s", self._remote_home, self.cwd
            )

            self._sync_manager = FileSyncManager(
                get_files_fn=lambda: iter_sync_files(f"{self._remote_home}/.hermes"),
                upload_fn=self._sprites_upload,
                delete_fn=self._sprites_delete,
                bulk_upload_fn=self._sprites_bulk_upload,
            )
            self._sync_manager.sync(force=True)
            self.init_session()
        except Exception:
            self._client.close()
            raise

    @property
    def sprite_name(self) -> str:
        return self._sprite_name

    def _detect_home(self) -> str:
        result = self._run_direct("printf '%s\\n' \"$HOME\"", timeout=30)
        home = result.get("output", "").strip().splitlines()
        return home[-1] if home else ""

    def _run_direct(self, command: str, *, timeout: int = 30) -> dict:
        proc = self._run_bash(command, login=False, timeout=timeout)
        return self._wait_for_process(proc, timeout=timeout)

    def _track_handle(self, handle: _SpritesProcessHandle) -> None:
        with self._active_lock:
            self._active_handles.add(handle)

    def _untrack_handle(self, handle: _SpritesProcessHandle) -> None:
        with self._active_lock:
            self._active_handles.discard(handle)

    def _sprites_upload(self, host_path: str, remote_path: str) -> None:
        data = Path(host_path).read_bytes()
        self._client.fs_write(
            self._sprite_name,
            remote_path,
            data,
            working_dir=self._remote_home,
            mkdir=True,
        )

    def _sprites_bulk_upload(self, files: list[tuple[str, str]]) -> None:
        if not files:
            return
        parents = unique_parent_dirs(files)
        if parents:
            self._run_direct(quoted_mkdir_command(parents), timeout=30)
        for host_path, remote_path in files:
            self._sprites_upload(host_path, remote_path)

    def _sprites_delete(self, remote_paths: list[str]) -> None:
        if not remote_paths:
            return
        # Prefer a single shell rm so delete semantics match the other remote
        # backends even when stale paths contain directories.
        self._run_direct(quoted_rm_command(remote_paths), timeout=30)

    def _before_execute(self) -> None:
        if self._sync_manager:
            self._sync_manager.sync()

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ):
        if stdin_data:
            cmd_string = self._embed_stdin_heredoc(cmd_string, stdin_data)
        args = ["bash"]
        if login:
            args.extend(["-l", "-c", cmd_string])
        else:
            args.extend(["-lc", cmd_string])

        handle = _SpritesProcessHandle(
            client=self._client,
            sprite_name=self._sprite_name,
            cmd=args,
            on_done=self._untrack_handle,
        )
        self._track_handle(handle)
        handle.start()
        return handle

    def cleanup(self):
        with self._active_lock:
            handles = list(self._active_handles)
            self._active_handles.clear()

        for handle in handles:
            try:
                if self._persistent:
                    handle.close()
                else:
                    handle.kill()
            except Exception:
                pass

        if not self._persistent and self._client is not None:
            try:
                self._client.delete_sprite(self._sprite_name)
                logger.info("Sprites: deleted non-persistent Sprite %s", self._sprite_name)
            except Exception as exc:
                logger.warning("Sprites: cleanup delete failed for %s: %s", self._sprite_name, exc)

        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
