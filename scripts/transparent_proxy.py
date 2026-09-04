#!/usr/bin/env python3
"""Loopback-only transparent authorization rewriter for Codex.

The proxy intentionally understands only the first HTTP request on a
connection. Normal HTTP requests are forced to close after the response;
WebSocket upgrades are tunneled unchanged after the Authorization header is
replaced. This keeps the implementation dependency-free while preserving SSE
streaming and WebSocket traffic.
"""

from __future__ import annotations

import json
import os
import selectors
import socket
import socketserver
import subprocess
import sys
from typing import Iterable


LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("CODEX_BRIDGE_LISTEN_PORT", "8318"))
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = int(os.environ.get("CODEX_BRIDGE_UPSTREAM_PORT", "8317"))
MAX_HEADER_BYTES = 64 * 1024


def helper_command() -> list[str]:
    command = os.environ.get("CODEX_BRIDGE_HELPER_CMD")
    helper = os.environ.get("CODEX_BRIDGE_HELPER")
    if not command or not helper:
        raise RuntimeError("credential helper is not configured")
    raw_args = os.environ.get("CODEX_BRIDGE_HELPER_ARGS", "[]")
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        raise RuntimeError("credential helper arguments are invalid") from exc
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise RuntimeError("credential helper arguments must be a string array")
    return [command, *args, helper]


def read_client_key() -> str:
    proc = subprocess.run(
        helper_command(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
        check=False,
    )
    key = proc.stdout.strip()
    if proc.returncode != 0 or not key:
        raise RuntimeError("credential helper returned no key")
    return key


def receive_headers(client: socket.socket) -> tuple[bytes, bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = client.recv(8192)
        if not chunk:
            raise ConnectionError("client closed before sending headers")
        data.extend(chunk)
        if len(data) > MAX_HEADER_BYTES:
            raise ValueError("request headers are too large")
    marker = data.index(b"\r\n\r\n") + 4
    return bytes(data[:marker]), bytes(data[marker:])


def parse_request(header: bytes) -> tuple[str, list[tuple[str, str]]]:
    lines = header.decode("iso-8859-1").split("\r\n")
    request_line = lines[0]
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise ValueError("malformed HTTP header")
        name, value = line.split(":", 1)
        headers.append((name.strip(), value.strip()))
    return request_line, headers


def is_health_request(request_line: str) -> bool:
    parts = request_line.split()
    return len(parts) >= 2 and parts[1].split("?", 1)[0] == "/__codex_bridge_health"


def is_upgrade(headers: Iterable[tuple[str, str]]) -> bool:
    return any(name.lower() == "upgrade" and value for name, value in headers)


def rewritten_request(request_line: str, headers: list[tuple[str, str]], key: str) -> bytes:
    upgraded = is_upgrade(headers)
    rendered: list[tuple[str, str]] = []
    saw_auth = False
    saw_connection = False
    for name, value in headers:
        lowered = name.lower()
        if lowered == "authorization":
            rendered.append((name, f"Bearer {key}"))
            saw_auth = True
        elif lowered == "host":
            rendered.append((name, f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"))
        elif lowered == "connection" and not upgraded:
            rendered.append((name, "close"))
            saw_connection = True
        else:
            rendered.append((name, value))
    if not saw_auth:
        rendered.append(("Authorization", f"Bearer {key}"))
    if not upgraded and not saw_connection:
        rendered.append(("Connection", "close"))
    lines = [request_line, *(f"{name}: {value}" for name, value in rendered), "", ""]
    return "\r\n".join(lines).encode("iso-8859-1")


def send_health(client: socket.socket) -> None:
    body = json.dumps(
        {"status": "ok", "upstream": f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"},
        separators=(",", ":"),
    ).encode()
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )
    client.sendall(response)


def send_error(client: socket.socket) -> None:
    body = b'{"error":"Codex transparent proxy is unavailable"}'
    try:
        client.sendall(
            b"HTTP/1.1 502 Bad Gateway\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
    except OSError:
        pass


def tunnel(left: socket.socket, right: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    selector.register(left, selectors.EVENT_READ, right)
    selector.register(right, selectors.EVENT_READ, left)
    try:
        while selector.get_map():
            for key, _ in selector.select(timeout=60):
                source = key.fileobj
                target = key.data
                try:
                    chunk = source.recv(64 * 1024)
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(source)
                    except Exception:
                        pass
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                target.sendall(chunk)
    finally:
        selector.close()


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client: socket.socket = self.request
        upstream: socket.socket | None = None
        try:
            header, remainder = receive_headers(client)
            request_line, headers = parse_request(header)
            if is_health_request(request_line):
                send_health(client)
                return
            key = read_client_key()
            upstream = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=10)
            upstream.settimeout(None)
            client.settimeout(None)
            upstream.sendall(rewritten_request(request_line, headers, key) + remainder)
            tunnel(client, upstream)
        except Exception:
            send_error(client)
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass


class ThreadingLoopbackServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    with ThreadingLoopbackServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler) as server:
        server.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
