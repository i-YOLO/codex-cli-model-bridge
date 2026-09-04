from __future__ import annotations

import io
import gzip
import json
import os
from pathlib import Path
import platform
import plistlib
import socket
import socketserver
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import deployment  # noqa: E402
import compaction  # noqa: E402


class DeploymentUnitTests(unittest.TestCase):
    def test_all_provider_presets_validate(self) -> None:
        paths = sorted((ROOT / "presets").glob("*.json"))
        self.assertGreaterEqual(len(paths), 7)
        for path in paths:
            with self.subTest(path=path.name):
                deployment.validate_provider_spec(json.loads(path.read_text()))

    def test_remote_provider_rejects_plain_http(self) -> None:
        with self.assertRaises(deployment.DeploymentError):
            deployment.validate_remote_url("http://provider.example/v1")
        deployment.validate_remote_url("https://provider.example/v1")
        deployment.validate_remote_url("http://127.0.0.1:8317/v1")

    def test_yaml_provider_merge_preserves_existing_text(self) -> None:
        original = 'host: "127.0.0.1"\n# keep me\nopenai-compatibility:\n  - name: "first"\n    disabled: false\n'
        entry = '- name: "second"\n  disabled: false\n  base-url: "https://example.com/v1"'
        updated = deployment.append_yaml_list_entry(original, "openai-compatibility", entry, "second")
        self.assertIn("# keep me", updated)
        self.assertIn('  - name: "first"', updated)
        self.assertIn('  - name: "second"', updated)
        with self.assertRaises(deployment.DeploymentError):
            deployment.append_yaml_list_entry(updated, "openai-compatibility", entry, "second")

    def test_oauth_alias_merge_and_collision(self) -> None:
        original = "host: \"127.0.0.1\"\n"
        models = [{"upstream_id": "gemini-high", "slug": "gemini-friendly"}]
        updated = deployment.append_oauth_aliases(original, "antigravity", models)
        self.assertIn("oauth-model-alias:", updated)
        self.assertIn("  antigravity:", updated)
        self.assertIn('alias: "gemini-friendly"', updated)
        self.assertIn("fork: true", updated)
        with self.assertRaises(deployment.DeploymentError):
            deployment.append_oauth_aliases(updated, "antigravity", models)

    def test_transaction_rolls_back_and_guards_later_edits(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "state"
            target = root / "config"
            target.write_text("before", encoding="utf-8")
            _, tx = deployment.begin_transaction(state, "unit", [target])
            target.write_text("after", encoding="utf-8")
            deployment.finish_transaction(state, tx)
            target.write_text("user-change", encoding="utf-8")
            with self.assertRaises(deployment.DeploymentError):
                deployment.restore_transaction(tx)
            deployment.restore_transaction(tx, require_after_match=False)
            self.assertEqual(target.read_text(), "before")

    def test_transaction_removes_created_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "state"
            target = root / "created"
            _, tx = deployment.begin_transaction(state, "unit", [target])
            target.write_text("created", encoding="utf-8")
            deployment.finish_transaction(state, tx)
            deployment.restore_transaction(tx)
            self.assertFalse(target.exists())

    def test_transaction_id_rejects_path_traversal(self) -> None:
        with self.assertRaises(deployment.DeploymentError):
            deployment.transaction_dir(Path("/tmp/state"), "../escape")
        with self.assertRaises(deployment.DeploymentError):
            deployment.transaction_dir(Path("/tmp/state"), "/absolute")
        self.assertEqual(
            deployment.transaction_dir(Path("/tmp/state"), "approved-transaction"),
            Path("/tmp/state/transactions/approved-transaction"),
        )

    def test_failed_transaction_records_completed_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "config.toml"
            target.write_text("before", encoding="utf-8")
            transaction_id, tx = deployment.begin_transaction(
                root / "state", "compaction-configure", [target], "approved-transaction"
            )
            target.write_text("candidate", encoding="utf-8")
            deployment.restore_transaction(tx, require_after_match=False)
            deployment.fail_transaction(
                root / "state",
                tx,
                "candidate failed",
                rollback_completed=True,
                service_restore=None,
            )
            recorded = deployment.read_json(
                deployment.transaction_dir(root / "state", transaction_id) / "transaction.json"
            )
            self.assertEqual(recorded["status"], "failed")
            self.assertTrue(recorded["failure"]["rollback_completed"])
            self.assertEqual(target.read_text(encoding="utf-8"), "before")

    def test_restore_recorded_services_only_restarts_marked_entries(self) -> None:
        payload = {
            "services": [
                {"identifier": "created-only", "restart_after_restore": False},
                {
                    "identifier": "bridge",
                    "definition_path": "/tmp/bridge.plist",
                    "restart_after_restore": True,
                },
            ]
        }
        with mock.patch.object(deployment, "restore_transparent_service", return_value=None) as restore:
            self.assertEqual(deployment.restore_recorded_services(payload), [])
        restore.assert_called_once_with(
            Path("/tmp/bridge.plist"), "CodexCliModelBridgeTransparentProxy"
        )

    def test_compaction_apply_requires_approved_sha_and_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = Path(raw) / "config.toml"
            config.write_text('[features]\nremote_compaction_v2 = false\n', encoding="utf-8")
            missing_sha = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "bridge.py"),
                    "compaction",
                    "configure",
                    "--config",
                    str(config),
                    "--transaction-id",
                    "approved-transaction",
                    "--apply",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(missing_sha.returncode, 2)
            self.assertIn("requires the config SHA-256", missing_sha.stdout)
            sha = deployment.sha256_path(config)
            missing_transaction = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "bridge.py"),
                    "compaction",
                    "configure",
                    "--config",
                    str(config),
                    "--expected-sha256",
                    str(sha),
                    "--apply",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(missing_transaction.returncode, 2)
            self.assertIn("requires the transaction id", missing_transaction.stdout)

    def test_checksum_lookup(self) -> None:
        payload = b"a" * 64 + b"  archive.tar.gz\n"
        self.assertEqual(deployment.checksum_for(payload, "archive.tar.gz"), "a" * 64)
        with self.assertRaises(deployment.DeploymentError):
            deployment.checksum_for(payload, "missing.zip")

    def test_release_asset_selection_for_all_platform_families(self) -> None:
        release = {
            "assets": [
                {"name": "CLIProxyAPI_1_darwin_arm64.tar.gz"},
                {"name": "CLIProxyAPI_1_linux_amd64.tar.gz"},
                {"name": "CLIProxyAPI_1_windows_amd64.zip"},
                {"name": "checksums.txt"},
            ]
        }
        cases = [
            (("darwin", ["arm64", "aarch64"]), "darwin_arm64"),
            (("linux", ["amd64", "x86_64"]), "linux_amd64"),
            (("windows", ["amd64", "x86_64"]), "windows_amd64"),
        ]
        for machine, expected in cases:
            with self.subTest(expected=expected), mock.patch.object(deployment, "normalized_machine", return_value=machine):
                archive, checksum = deployment.select_release_asset(release)
                self.assertIn(expected, archive["name"])
                self.assertEqual(checksum["name"], "checksums.txt")

    def test_safe_tar_extracts_binary(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
            data = b"binary"
            member = tarfile.TarInfo("package/cli-proxy-api")
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))
        self.assertEqual(deployment.safe_extract_binary(buffer.getvalue(), "asset.tar.gz"), b"binary")

    def test_safe_tar_rejects_path_traversal(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
            data = b"bad"
            member = tarfile.TarInfo("../cli-proxy-api")
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))
        with self.assertRaises(deployment.DeploymentError):
            deployment.safe_extract_binary(buffer.getvalue(), "asset.tar.gz")

    def test_generated_proxy_config_is_loopback_and_secret_bearing(self) -> None:
        text = deployment.proxy_config_source(Path("/tmp/auth"))
        self.assertIn('host: "127.0.0.1"', text)
        self.assertIn("allow-remote: false", text)
        key = deployment.proxy_token_from_config(Path(self._write_temp(text)))
        self.assertGreater(len(key), 20)
        self.assertNotIn(key, json.dumps({"status": "planned", "secrets_redacted": True}))

    def _write_temp(self, text: str) -> str:
        handle = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        handle.write(text)
        handle.close()
        return handle.name

    def test_service_definitions_contain_expected_restart_contract(self) -> None:
        binary = Path("/opt/tool/cli-proxy-api")
        config = Path("/home/user/config.yaml")
        plist = plistlib.loads(deployment.proxy_launch_agent(binary, config))
        self.assertTrue(plist["RunAtLoad"])
        self.assertTrue(plist["KeepAlive"])
        unit = deployment.proxy_systemd_unit(binary, config)
        self.assertIn("Restart=always", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_bootstrap_preview_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "bridge.py"),
                    "bootstrap",
                    "--proxy-binary",
                    str(root / "bin"),
                    "--proxy-config",
                    str(root / "config.yaml"),
                    "--state-dir",
                    str(root / "state"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["status"], "planned")
            self.assertEqual(list(root.iterdir()), [])

    def test_provider_apply_is_redacted_idempotent_and_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "state"
            config = root / "config.yaml"
            config.write_text('host: "127.0.0.1"\nport: 8317\n', encoding="utf-8")
            secret = deployment.credential_path(state, "deepseek")
            deployment.atomic_write_text(secret, "super-secret-value\n")
            command = [
                sys.executable,
                str(SCRIPTS / "bridge.py"),
                "provider",
                "add",
                "--preset",
                "deepseek",
                "--models",
                "deepseek-v4-pro",
                "--proxy-config",
                str(config),
                "--state-dir",
                str(state),
                "--apply",
            ]
            applied = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertNotIn("super-secret-value", applied.stdout)
            payload = json.loads(applied.stdout)
            self.assertEqual(payload["status"], "applied")
            self.assertFalse(secret.exists())
            self.assertIn("super-secret-value", config.read_text())
            repeated = subprocess.run(command[:-1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(json.loads(repeated.stdout)["status"], "unchanged")
            rollback = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "bridge.py"),
                    "rollback",
                    "--transaction",
                    payload["transaction"],
                    "--state-dir",
                    str(state),
                    "--apply",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(rollback.returncode, 0, rollback.stderr)
            self.assertEqual(config.read_text(), 'host: "127.0.0.1"\nport: 8317\n')

    def test_generic_provider_writes_owner_local_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "state"
            config = root / "config.yaml"
            config.write_text('host: "127.0.0.1"\n', encoding="utf-8")
            spec = json.loads((ROOT / "presets" / "generic-openai.example.json").read_text())
            spec["id"] = "custom-test"
            spec_path = root / "provider.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            deployment.atomic_write_text(deployment.credential_path(state, "custom-test"), "key\n")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "bridge.py"),
                    "provider",
                    "add",
                    "--spec",
                    str(spec_path),
                    "--proxy-config",
                    str(config),
                    "--state-dir",
                    str(state),
                    "--apply",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            manifest = state / "models.d" / "example-model-id.json"
            self.assertTrue(manifest.exists())
            self.assertEqual(json.loads(manifest.read_text())["context_window"], 131072)

    def test_image_probe_prompt_is_exact(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        import bridge

        self.assertEqual(
            bridge.probe_prompt(False, False, True),
            "Inspect the attached image, then reply with exactly: CODEX_BRIDGE_IMAGE_OK",
        )
        png = bridge.probe_png_bytes()
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IHDR", png)
        self.assertTrue(png.endswith(b"IEND\xaeB`\x82"))

    def test_local_compaction_is_deprecated_configure_alias(self) -> None:
        args = deployment.argparse.Namespace(
            config="/tmp/config.toml",
            state_dir="/tmp/bridge-state",
            expected_sha256="approved",
            transaction_id="approved-transaction",
            apply=True,
        )
        with mock.patch.object(deployment, "cmd_compaction_configure") as configure:
            deployment.cmd_local_compaction(args)
        forwarded = configure.call_args.args[0]
        self.assertTrue(forwarded.deprecated_alias)
        self.assertTrue(forwarded.apply)
        self.assertEqual(forwarded.expected_sha256, "approved")
        self.assertEqual(forwarded.transaction_id, "approved-transaction")
        self.assertEqual(forwarded.proxy_url, deployment.DEFAULT_TRANSPARENT_URL)
        self.assertEqual(forwarded.upstream_url, deployment.DEFAULT_PROXY_URL)

    def test_compaction_setting_only_moves_to_true(self) -> None:
        original = '[features]\nremote_compaction_v2 = false\n'
        updated = deployment.set_toml_table_bool(original, "features", "remote_compaction_v2", True)
        self.assertIn("remote_compaction_v2 = true", updated)
        self.assertNotIn("remote_compaction_v2 = false", updated)

    def test_sync_defaults_to_fallback_profile(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        import bridge

        args = bridge.parser().parse_args(["sync"])
        self.assertEqual(Path(args.config), bridge.DEFAULT_PROFILE_CONFIG)

    def test_multi_agent_probe_requires_marker_delivery(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        import bridge

        self.assertEqual(
            bridge.response_output_text(
                {
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "CODEX_MULTI_AGENT_OK"}],
                        }
                    ]
                }
            ),
            "CODEX_MULTI_AGENT_OK",
        )
        self.assertEqual(bridge.response_output_text({"output": []}), "")


class CaptureHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            data.extend(self.request.recv(4096))
        marker = data.index(b"\r\n\r\n") + 4
        header = bytes(data[:marker])
        body = bytearray(data[marker:])
        length = 0
        for line in header.decode("iso-8859-1").split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        while len(body) < length:
            body.extend(self.request.recv(length - len(body)))
        captured = header + bytes(body[:length])
        self.server.captured = captured  # type: ignore[attr-defined]
        self.server.captured_requests.append(captured)  # type: ignore[attr-defined]
        if b"upgrade: websocket" in header.lower():
            self.request.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            payload = self.request.recv(4)
            self.request.sendall(payload)
        else:
            text = bytes(body[:length]).decode("utf-8", errors="replace") or "mock-normal-response"
            try:
                request_payload = json.loads(text)
            except json.JSONDecodeError:
                request_payload = {}
            if request_payload.get("model") in self.server.fail_models:  # type: ignore[attr-defined]
                error_body = b'{"error":"forced"}'
                self.request.sendall(
                    b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(error_body)}\r\nConnection: close\r\n\r\n".encode()
                    + error_body
                )
                return
            response_payload = {
                "id": "resp_mock",
                "object": "response",
                "status": "completed",
                "model": "mock",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
            }
            response_body = json.dumps(response_payload).encode()
            self.request.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(response_body)}\r\nConnection: close\r\n\r\n".encode()
                + response_body
            )


class TransparentProxyIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), CaptureHandler)
        self.upstream.daemon_threads = True
        self.upstream.captured_requests = []  # type: ignore[attr-defined]
        self.upstream.fail_models = set()  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.proxy_port = probe.getsockname()[1]
        probe.close()
        self.temp = tempfile.TemporaryDirectory()
        helper = Path(self.temp.name) / "helper.py"
        helper.write_text('print("local-client-key")\n', encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "CODEX_BRIDGE_LISTEN_PORT": str(self.proxy_port),
                "CODEX_BRIDGE_UPSTREAM_PORT": str(self.upstream.server_address[1]),
                "CODEX_BRIDGE_HELPER": str(helper),
                "CODEX_BRIDGE_HELPER_CMD": sys.executable,
                "CODEX_BRIDGE_HELPER_ARGS": "[]",
            }
        )
        self.proxy = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "transparent_proxy.py")],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            self.fail("transparent proxy did not start")

    def tearDown(self) -> None:
        self.proxy.terminate()
        self.proxy.wait(timeout=5)
        self.upstream.shutdown()
        self.upstream.server_close()
        self.temp.cleanup()

    def request(self, path: str, payload: dict, extra_headers: bytes = b"") -> bytes:
        body = json.dumps(payload, separators=(",", ":")).encode()
        with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=5) as client:
            client.sendall(
                f"POST {path} HTTP/1.1\r\nHost: old\r\nAuthorization: Bearer desktop-token\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n".encode()
                + extra_headers
                + b"\r\n"
                + body
            )
            response = bytearray()
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
        return bytes(response)

    def test_http_sse_rewrites_authorization(self) -> None:
        payload = {
            "model": "gemini-test",
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ordinary"}]}],
            "stream": False,
        }
        response = self.request("/v1/responses", payload)
        self.assertIn(b"200 OK", response)
        self.assertIn(b"Authorization: Bearer local-client-key", self.upstream.captured)  # type: ignore[attr-defined]
        self.assertNotIn(b"desktop-token", self.upstream.captured)  # type: ignore[attr-defined]
        captured_body = self.upstream.captured.split(b"\r\n\r\n", 1)[1]  # type: ignore[attr-defined]
        self.assertEqual(captured_body, json.dumps(payload, separators=(",", ":")).encode())

    def test_synthetic_v2_and_replay(self) -> None:
        marker = "synthetic-replay-marker"
        response = self.request(
            "/v1/responses",
            {
                "model": "glm-test",
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": marker}]},
                    {"type": "compaction_trigger"},
                ],
                "stream": True,
            },
        )
        self.assertIn(b"response.output_item.added", response)
        self.assertIn(b"response.output_item.done", response)
        self.assertIn(b"ocx1:", response)
        body = response.split(b"\r\n\r\n", 1)[1]
        events = deployment._parse_sse(body)
        completed = [event for event in events if event.get("type") == "response.completed"][0]
        item = completed["response"]["output"][0]
        self.assertEqual(item["type"], "compaction")
        summary_request = self.upstream.captured_requests[-1].split(b"\r\n\r\n", 1)[1]  # type: ignore[attr-defined]
        summary_payload = json.loads(summary_request)
        self.assertEqual(summary_payload["model"], "glm-test")
        self.assertNotIn("tools", summary_payload)

        replay = self.request(
            "/v1/responses",
            {
                "model": "glm-test",
                "input": [
                    item,
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
                ],
                "stream": False,
            },
        )
        self.assertIn(b"200 OK", replay)
        captured = self.upstream.captured_requests[-1].split(b"\r\n\r\n", 1)[1]  # type: ignore[attr-defined]
        forwarded = json.loads(captured)
        self.assertEqual(forwarded["input"][0]["type"], "message")
        self.assertNotIn("ocx1:", json.dumps(forwarded))
        self.assertIn(marker, json.dumps(forwarded))

    def test_legacy_v1_returns_replacement_history(self) -> None:
        marker = "legacy-v1-marker"
        response = self.request(
            "/v1/responses/compact",
            {
                "model": "gemini-test",
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": marker}]}
                ],
                "stream": False,
            },
        )
        payload = json.loads(response.split(b"\r\n\r\n", 1)[1])
        self.assertGreaterEqual(len(payload["output"]), 2)
        self.assertIn(marker, json.dumps(payload, ensure_ascii=False))

    def test_cross_route_compaction_failure_never_falls_back_to_gpt(self) -> None:
        self.upstream.fail_models.add("cross-route-target")  # type: ignore[attr-defined]
        response = self.request(
            "/v1/responses",
            {
                "model": "cross-route-target",
                "input": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "prior route result"}],
                    },
                    {"type": "compaction_trigger"},
                ],
                "stream": True,
            },
        )
        self.assertIn(b"response.failed", response)
        models = []
        for captured in self.upstream.captured_requests:  # type: ignore[attr-defined]
            payload = json.loads(captured.split(b"\r\n\r\n", 1)[1])
            models.append(payload.get("model"))
        self.assertEqual(models, ["cross-route-target"] * 3)
        self.assertNotIn("gpt-5.6-sol", models)

    def test_native_reasoning_content_is_removed_before_forward(self) -> None:
        response = self.request(
            "/v1/responses",
            {
                "model": "gpt-test",
                "input": [
                    {"type": "reasoning", "content": [1, 2], "summary": [{"type": "summary_text", "text": "keep"}]},
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
                ],
                "stream": False,
            },
        )
        self.assertIn(b"200 OK", response)
        captured = self.upstream.captured_requests[-1].split(b"\r\n\r\n", 1)[1]  # type: ignore[attr-defined]
        forwarded = json.loads(captured)
        self.assertNotIn("content", forwarded["input"][0])
        self.assertEqual(forwarded["input"][0]["summary"][0]["text"], "keep")

    def test_replay_supports_gzip_and_deflate_and_updates_length(self) -> None:
        for encoding in ("gzip", "deflate"):
            with self.subTest(encoding=encoding):
                payload = {
                    "model": "glm-test",
                    "input": [
                        {
                            "type": "compaction",
                            "encrypted_content": compaction.encode_compaction_summary(f"{encoding}-marker"),
                        }
                    ],
                    "stream": False,
                }
                source = json.dumps(payload, separators=(",", ":")).encode()
                body = gzip.compress(source) if encoding == "gzip" else zlib.compress(source)
                with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=5) as client:
                    client.sendall(
                        f"POST /v1/responses HTTP/1.1\r\nHost: old\r\nContent-Type: application/json\r\nContent-Encoding: {encoding}\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                        + body
                    )
                    while client.recv(4096):
                        pass
                captured = self.upstream.captured_requests[-1]  # type: ignore[attr-defined]
                header, compressed = captured.split(b"\r\n\r\n", 1)
                expected_length = next(
                    int(line.split(b":", 1)[1].strip())
                    for line in header.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                self.assertEqual(expected_length, len(compressed))
                decoded = gzip.decompress(compressed) if encoding == "gzip" else zlib.decompress(compressed)
                forwarded = json.loads(decoded)
                self.assertEqual(forwarded["input"][0]["type"], "message")
                self.assertIn(f"{encoding}-marker", json.dumps(forwarded))

    def test_compaction_tunnels_unsupported_content_encoding(self) -> None:
        body = b"not-brotli"
        with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=5) as client:
            client.sendall(
                f"POST /v1/responses/compact HTTP/1.1\r\nHost: old\r\nContent-Type: application/json\r\nContent-Encoding: br\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            response = bytearray()
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
        self.assertNotIn(b"415 Unsupported Media Type", response)
        self.assertIn(b"200 OK", response)
        captured = self.upstream.captured_requests[-1]  # type: ignore[attr-defined]
        header, forwarded = captured.split(b"\r\n\r\n", 1)
        self.assertIn(b"content-encoding: br", header.lower())
        self.assertEqual(forwarded, body)

    def test_health_reports_compaction_protocol_without_payloads(self) -> None:
        with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=2) as client:
            client.sendall(b"GET /__codex_bridge_health HTTP/1.1\r\nHost: old\r\n\r\n")
            response = bytearray()
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
        payload = json.loads(bytes(response).split(b"\r\n\r\n", 1)[1])
        self.assertEqual(payload["compaction"]["protocol_version"], "2")
        self.assertNotIn("synthetic-replay-marker", json.dumps(payload))

    def test_websocket_upgrade_is_tunneled(self) -> None:
        with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=2) as client:
            client.sendall(
                b"GET /v1/responses HTTP/1.1\r\nHost: old\r\nAuthorization: Bearer desktop-token\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
            )
            header = bytearray()
            while b"\r\n\r\n" not in header:
                header.extend(client.recv(4096))
            self.assertIn(b"101 Switching Protocols", header)
            client.sendall(b"ping")
            self.assertEqual(client.recv(4), b"ping")
        self.assertIn(b"Authorization: Bearer local-client-key", self.upstream.captured)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
