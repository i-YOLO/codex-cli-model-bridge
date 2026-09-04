from __future__ import annotations

import io
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
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import deployment  # noqa: E402


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

    def test_local_compaction_preview_apply_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "state"
            config = root / "config.toml"
            config.write_text('model = "gpt-test"\n\n[features]\nremote_compaction_v2 = true\n', encoding="utf-8")
            preview = subprocess.run(
                [sys.executable, str(SCRIPTS / "bridge.py"), "local-compaction", "--config", str(config), "--state-dir", str(state)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(preview.returncode, 0, preview.stderr)
            planned = json.loads(preview.stdout)
            self.assertTrue(planned["changed"])
            applied = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "bridge.py"),
                    "local-compaction",
                    "--config",
                    str(config),
                    "--state-dir",
                    str(state),
                    "--expected-sha256",
                    planned["config_sha256"],
                    "--apply",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            payload = json.loads(applied.stdout)
            self.assertIn("remote_compaction_v2 = false", config.read_text())
            deployment.restore_transaction(
                deployment.read_json(deployment.transaction_dir(state, payload["transaction"]) / "transaction.json")
            )
            self.assertIn("remote_compaction_v2 = true", config.read_text())

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
        self.server.captured = bytes(data)  # type: ignore[attr-defined]
        if b"upgrade: websocket" in bytes(data).lower():
            self.request.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            payload = self.request.recv(4)
            self.request.sendall(payload)
        else:
            body = b"data: ok\n\n"
            self.request.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )


class TransparentProxyIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), CaptureHandler)
        self.upstream.daemon_threads = True
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

    def test_http_sse_rewrites_authorization(self) -> None:
        with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=2) as client:
            client.sendall(
                b"POST /v1/responses HTTP/1.1\r\nHost: old\r\nAuthorization: Bearer desktop-token\r\nContent-Length: 0\r\n\r\n"
            )
            response = bytearray()
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
        self.assertIn(b"data: ok", response)
        self.assertIn(b"Authorization: Bearer local-client-key", self.upstream.captured)  # type: ignore[attr-defined]
        self.assertNotIn(b"desktop-token", self.upstream.captured)  # type: ignore[attr-defined]

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
