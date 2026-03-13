"""Unix socket proxy for skill CLI commands.

Runs skill CLI commands with credentials injected server-side, so the
Claude subprocess never sees secret env vars. The protocol is one JSON
request/response per connection, newline-terminated.
"""

import json
import logging
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger("istota.skill_proxy")

# Skills that have __main__.py and can be invoked via the proxy.
# Maintained manually — must match src/istota/skills/*/__main__.py.
_ALLOWED_SKILLS = frozenset({
    "accounting",
    "bookmarks",
    "browse",
    "calendar",
    "email",
    "garmin",
    "location",
    "markets",
    "memory_search",
    "nextcloud",
    "transcribe",
    "whisper",
})


class SkillProxy:
    """Unix socket server that proxies skill CLI commands with credentials.

    Usage::

        with SkillProxy(sock_path, credential_env, base_env) as proxy:
            # Claude subprocess runs here — calls istota-skill client
            ...

    The server accepts connections, reads a JSON request, runs the skill
    CLI with merged env (base_env + credential_env), and returns the result.
    """

    def __init__(
        self,
        socket_path: Path,
        credential_env: dict[str, str],
        base_env: dict[str, str],
        timeout: int = 300,
    ):
        self.socket_path = socket_path
        self.credential_env = credential_env
        self.base_env = base_env
        self.timeout = timeout
        self._server_sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        # Clean up stale socket file
        if self.socket_path.exists():
            self.socket_path.unlink()

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.bind(str(self.socket_path))
        self._server_sock.listen(8)
        self._server_sock.settimeout(1.0)  # So accept loop checks stop event

        self._thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="skill-proxy",
        )
        self._thread.start()
        logger.debug("Skill proxy started on %s", self.socket_path)

    def stop(self) -> None:
        self._stop_event.set()
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        # Clean up socket file
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass
        logger.debug("Skill proxy stopped")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                conn, _ = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # Socket closed

            # Handle each connection in a new thread so multiple skill
            # calls can run concurrently (e.g. Claude runs two Bash calls).
            handler = threading.Thread(
                target=self._handle_connection, args=(conn,),
                daemon=True, name="skill-proxy-handler",
            )
            handler.start()

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(self.timeout + 10)  # Allow for subprocess timeout + buffer
            data = self._recv_all(conn)
            if not data:
                return

            try:
                request = json.loads(data)
            except json.JSONDecodeError as e:
                self._send_response(conn, {
                    "stdout": "",
                    "stderr": f"Invalid JSON request: {e}",
                    "returncode": 1,
                })
                return

            # Route by request type: "credential" for lookups, default for skill calls
            req_type = request.get("type")

            if req_type == "credential":
                name = request.get("name", "")
                if name not in self.credential_env:
                    self._send_response(conn, {"error": f"Unknown credential: {name!r}"})
                    return
                self._send_response(conn, {"value": self.credential_env[name]})
                return

            skill = request.get("skill", "")
            args = request.get("args", [])

            # Validate skill name
            if skill not in _ALLOWED_SKILLS:
                self._send_response(conn, {
                    "stdout": "",
                    "stderr": f"Unknown skill: {skill!r}",
                    "returncode": 1,
                })
                return

            # Build command
            cmd = [sys.executable, "-m", f"istota.skills.{skill}"] + args

            # Merge envs: base gets credentials layered on top
            merged_env = dict(self.base_env)
            merged_env.update(self.credential_env)

            try:
                result = subprocess.run(
                    cmd,
                    env=merged_env,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
                self._send_response(conn, {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                })
            except subprocess.TimeoutExpired:
                self._send_response(conn, {
                    "stdout": "",
                    "stderr": f"Skill command timed out after {self.timeout}s",
                    "returncode": 124,
                })
            except Exception as e:
                self._send_response(conn, {
                    "stdout": "",
                    "stderr": f"Failed to run skill: {e}",
                    "returncode": 1,
                })

        except Exception:
            logger.debug("Error handling proxy connection", exc_info=True)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _recv_all(conn: socket.socket) -> str:
        """Read until newline (protocol delimiter)."""
        chunks = []
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        return b"".join(chunks).decode("utf-8", errors="replace").strip()

    @staticmethod
    def _send_response(conn: socket.socket, response: dict) -> None:
        """Send JSON response terminated by newline."""
        data = json.dumps(response) + "\n"
        conn.sendall(data.encode("utf-8"))
