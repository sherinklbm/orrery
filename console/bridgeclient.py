"""Talks to the Hermes helper over its Unix socket (see ../bridge/hermes_bridge.py). Standard library only."""
import http.client
import json
import socket


class BridgeError(Exception):
    """The helper is missing or refused; the message is safe to show. status 400 means the helper judged the input bad."""

    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Bridge:
    def __init__(self, path, timeout=70):
        self.path = path
        self.timeout = timeout

    def call(self, method, route, body=None):
        conn = _UnixConnection(self.path, self.timeout)
        try:
            data = json.dumps(body) if body is not None else None
            conn.request(method, route, data, {"Content-Type": "application/json"} if data else {})
            resp = conn.getresponse()
            out = json.loads(resp.read(500_000) or b"{}")
        except (OSError, ValueError):
            raise BridgeError("The Hermes helper is not running on the server (run deploy/enable-bridge.sh).")
        finally:
            conn.close()
        if resp.status != 200:
            raise BridgeError(str(out.get("error", "the helper refused the request")), resp.status)
        return out
