import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
import sys

sys.path.insert(0, str(SCRIPTS))

import bridge  # noqa: E402


def official_payload(*slugs: str) -> dict:
    return {
        "models": [
            {"slug": slug, "display_name": slug, "comp_hash": "2911" if slug.endswith("mini") else "3000"}
            for slug in slugs
        ]
    }


class _MockHandler(BaseHTTPRequestHandler):
    responses: list = []

    def do_GET(self):  # noqa: N802
        index, status, body = self.server.responses.pop(0)
        if status == 304:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("ETag", f'"{index}"')
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

    def log_message(self, *args):
        pass


class FetchOfficialCatalogTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _MockHandler)
        self.server.responses = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()

    def test_returns_validated_payload_and_etag(self):
        self.server.responses.append(
            (0, 200, json.dumps(official_payload("gpt-5.6-sol", "gpt-5.5")).encode())
        )
        payload, etag, error = bridge.fetch_official_catalog(self.base_url, "tok", None, "0.153.3", None)
        self.assertIsNone(error)
        self.assertEqual([m["slug"] for m in payload["models"]], ["gpt-5.6-sol", "gpt-5.5"])
        self.assertEqual(etag, '"0"')

    def test_304_reports_not_modified(self):
        self.server.responses.append((0, 304, b""))
        payload, etag, error = bridge.fetch_official_catalog(
            self.base_url, "tok", None, "0.153.3", {"etag": '"7"', "models": [{"slug": "keep"}]}
        )
        self.assertIsNone(payload)
        self.assertEqual(etag, '"7"')
        self.assertIsNone(error)

    def test_invalid_json_is_rejected(self):
        self.server.responses.append((0, 200, b"{not-json"))
        payload, etag, error = bridge.fetch_official_catalog(self.base_url, "tok", None, "0.153.3", None)
        self.assertIsNone(payload)
        self.assertIn("JSON", error)

    def test_missing_slugs_are_rejected(self):
        self.server.responses.append((0, 200, json.dumps({"models": [{"display_name": "x"}]}).encode()))
        payload, etag, error = bridge.fetch_official_catalog(self.base_url, "tok", None, "0.153.3", None)
        self.assertIsNone(payload)
        self.assertIn("slug", error)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def manifest(slug: str, template: str) -> dict:
    return {
        "schema_version": 1,
        "slug": slug,
        "display_name": slug,
        "description": "managed",
        "template_slug": template,
        "context_window": 1000,
        "effective_context_window_percent": 95,
        "default_reasoning_level": "high",
        "reasoning_efforts": ["high"],
        "input_modalities": ["text"],
        "priority": 10,
    }


class CatalogRefreshEndToEndTests(unittest.TestCase):
    def test_refresh_stamps_managed_comp_hash_and_keeps_native_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state_dir = home / "bridge-state"
            catalog_path = home / "model-catalog-cli-proxy.json"
            official_path = state_dir / "official-catalog.json"
            write_json(
                official_path,
                {"fetched_at": "t0", "etag": '"7"', "client_version": "0.153.3", "models": [
                    official_payload("gpt-5.6-sol", "gpt-5.4-mini")["models"][0],
                    official_payload("gpt-5.4-mini")["models"][0],
                ]},
            )
            write_json(home / "auth.json", {"tokens": {"access_token": "tok"}, "account_id": "acc"})
            write_json(catalog_path, {"models": official_payload("gpt-5.6-sol")["models"]})
            (home / "profile.config.toml").write_text("", encoding="utf-8")
            write_json(
                state_dir / "models.d" / "glm-x.json",
                manifest("glm-x", "gpt-5.4-mini"),
            )
            write_json(
                state_dir / "state.json",
                {"schema_version": 1, "managed_model_ids": ["glm-x"]},
            )
            write_json(
                state_dir / "catalog-policy.json",
                {"schema_version": 1, "protected_native_model_ids": ["gpt-5.6-sol"], "managed_comp_hash": "3000"},
            )

            fresh = official_payload("gpt-5.6-sol", "gpt-5.4-mini", "gpt-6-astra")
            fresh["models"][2]["comp_hash"] = "3000"
            args = argparse.Namespace(
                config=home / "profile.config.toml",
                catalog=catalog_path,
                native_catalog=official_path,
                catalog_policy=state_dir / "catalog-policy.json",
                state_dir=state_dir,
                auth_file=home / "auth.json",
                base_url="http://127.0.0.1:1",
                client_version="0.153.3",
                no_notify=True,
                apply=True,
            )
            with mock.patch.object(bridge, "fetch_official_catalog", return_value=(fresh, '"9"', None)), mock.patch.object(
                bridge, "token_from_provider", return_value=("tok", None)
            ), mock.patch.object(bridge, "live_model_ids", return_value={"glm-x"}):
                bridge.cmd_catalog_refresh(args)

            entries = {m["slug"]: m for m in json.load(open(catalog_path))["models"]}
            self.assertIn("gpt-6-astra", entries)
            self.assertEqual(entries["gpt-6-astra"]["comp_hash"], "3000")
            self.assertEqual(entries["gpt-5.4-mini"]["comp_hash"], "2911")
            # The managed entry is stamped even though its template carries 2911.
            self.assertEqual(entries["glm-x"]["comp_hash"], "3000")
            status = json.load(open(state_dir / "catalog-refresh-status.json"))
            self.assertTrue(status["applied"])
            self.assertIn("gpt-6-astra", status["added"])


if __name__ == "__main__":
    unittest.main()
