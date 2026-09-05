#!/usr/bin/env python3
"""Zero-to-one deployment commands for codex-cli-model-bridge.

This module uses only the Python standard library. It keeps secrets out of
arguments and output, writes through atomic replacements, and records every
mutation as a reversible transaction.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from compaction import (
    COMPACTION_ITEM_TYPES,
    OCX_COMPACTION_PREFIX,
    extract_output_text,
)


SKILL_DIR = Path(__file__).resolve().parent.parent
BRIDGE_SCRIPT = Path(__file__).resolve().parent / "bridge.py"
DEFAULT_STATE_DIR = Path("~/.config/codex-cli-model-bridge").expanduser()
DEFAULT_INSTALL_DIR = Path("~/.local/share/codex-cli-model-bridge/bin").expanduser()
DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
def discover_default_proxy_config() -> Path:
    configured = os.environ.get("CLIPROXYAPI_CONFIG")
    if configured:
        return Path(configured).expanduser()
    runtime_state = Path("~/.config/codex-cli-model-bridge/runtime.json").expanduser()
    if runtime_state.exists():
        try:
            value = json.loads(runtime_state.read_text(encoding="utf-8")).get("proxy_config")
            if isinstance(value, str) and value:
                return Path(value).expanduser()
        except (OSError, json.JSONDecodeError):
            pass
    home = Path.home()
    candidates = [
        home / ".cli-proxy-api" / "config.yaml",
        home / ".cliproxyapi" / "config.yaml",
        Path("/opt/homebrew/etc/cliproxyapi.conf"),
        Path("/usr/local/etc/cliproxyapi.conf"),
    ]
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        roaming = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        candidates[0:0] = [
            local / "CLIProxyAPI" / "config.yaml",
            roaming / "CLIProxyAPI" / "config.yaml",
            local / "EasyCLIProxyAPI" / "cpa-core" / "config.yaml",
        ]
    return next((path for path in candidates if path.exists()), candidates[0])


DEFAULT_PROXY_CONFIG = discover_default_proxy_config()
DEFAULT_PROXY_URL = "http://127.0.0.1:8317/v1"
DEFAULT_TRANSPARENT_URL = "http://127.0.0.1:8318/v1"
RELEASE_API = "https://api.github.com/repos/router-for-me/CLIProxyAPI/releases"
PROVIDER_SCHEMA_VERSION = 1
TRANSACTION_SCHEMA_VERSION = 1
PROXY_SERVICE_LABEL = "com.jimu.codex-cli-model-bridge-cliproxyapi"


class DeploymentError(RuntimeError):
    pass


def emit(value: object, exit_code: int = 0) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    raise SystemExit(exit_code)


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except FileNotFoundError:
        return None


def mode(path: Path) -> str | None:
    try:
        return oct(stat.S_IMODE(path.stat().st_mode))
    except FileNotFoundError:
        return None


def atomic_write_bytes(path: Path, data: bytes, file_mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temp, file_mode)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def atomic_write_text(path: Path, text: str, file_mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), file_mode)


def read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise DeploymentError(f"JSON root must be an object: {path}")
    return payload


def is_loopback_url(value: str) -> bool:
    try:
        return urllib.parse.urlparse(value).hostname in {"127.0.0.1", "localhost", "::1"}
    except ValueError:
        return False


def validate_remote_url(value: str) -> None:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "http" and is_loopback_url(value):
        return
    raise DeploymentError("remote provider URLs must use HTTPS; HTTP is allowed only on loopback")


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def indent_block(text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def top_level_section(text: str, name: str) -> tuple[int, int] | None:
    lines = text.splitlines(keepends=True)
    start = None
    pattern = re.compile(rf"^{re.escape(name)}:\s*(?:#.*)?$")
    for index, line in enumerate(lines):
        raw = line.rstrip("\r\n")
        if start is None and pattern.match(raw):
            start = index
            continue
        if start is not None and raw and not raw[0].isspace() and not raw.startswith("#"):
            return start, index
    return (start, len(lines)) if start is not None else None


def append_yaml_list_entry(text: str, section: str, entry: str, provider_id: str) -> str:
    lines = text.splitlines(keepends=True)
    span = top_level_section(text, section)
    marker = re.compile(rf'^\s+(?:-\s+)?name:\s*["\']?{re.escape(provider_id)}["\']?\s*(?:#.*)?$')
    if span:
        start, end = span
        for line in lines[start + 1 : end]:
            if marker.match(line.rstrip("\r\n")):
                raise DeploymentError(f"provider id already exists in {section}: {provider_id}")
        rendered = indent_block(entry.rstrip(), 2) + "\n"
        lines.insert(end, rendered)
        return "".join(lines)
    prefix = text.rstrip() + ("\n\n" if text.strip() else "")
    return prefix + f"{section}:\n" + indent_block(entry.rstrip(), 2) + "\n"


def append_oauth_aliases(text: str, channel: str, models: list[dict]) -> str:
    root_name = "oauth-model-alias"
    lines = text.splitlines(keepends=True)
    root_span = top_level_section(text, root_name)
    aliases = {model["slug"] for model in models}
    for alias in aliases:
        if re.search(rf'^\s+alias:\s*["\']?{re.escape(alias)}["\']?\s*(?:#.*)?$', text, re.MULTILINE):
            raise DeploymentError(f"OAuth alias already exists: {alias}")
    rendered_lines: list[str] = []
    for model in models:
        rendered_lines.extend(
            [
                f"    - name: {yaml_string(model['upstream_id'])}\n",
                f"      alias: {yaml_string(model['slug'])}\n",
                "      fork: true\n",
            ]
        )
    if not root_span:
        prefix = text.rstrip() + ("\n\n" if text.strip() else "")
        return prefix + f"{root_name}:\n  {channel}:\n" + "".join(rendered_lines)
    root_start, root_end = root_span
    child_pattern = re.compile(rf"^  {re.escape(channel)}:\s*(?:#.*)?$")
    child_start = next(
        (index for index in range(root_start + 1, root_end) if child_pattern.match(lines[index].rstrip("\r\n"))),
        None,
    )
    if child_start is None:
        lines.insert(root_end, f"  {channel}:\n" + "".join(rendered_lines))
        return "".join(lines)
    child_end = root_end
    for index in range(child_start + 1, root_end):
        raw = lines[index].rstrip("\r\n")
        if raw.startswith("  ") and not raw.startswith("    ") and raw.strip() and not raw.lstrip().startswith("#"):
            child_end = index
            break
    lines.insert(child_end, "".join(rendered_lines))
    return "".join(lines)


def redact_provider_summary(spec: dict) -> dict:
    return {
        "id": spec["id"],
        "adapter": spec["adapter"],
        "auth_mode": spec["auth_mode"],
        "base_url": spec.get("base_url"),
        "routing_prefix": spec.get("routing_prefix", ""),
        "models": [item["slug"] for item in spec.get("models", [])],
        "secrets_redacted": True,
    }


def load_provider_spec(preset: str | None, spec_path: str | None) -> dict:
    if bool(preset) == bool(spec_path):
        raise DeploymentError("choose exactly one of --preset or --spec")
    path = Path(spec_path).expanduser() if spec_path else SKILL_DIR / "presets" / f"{preset}.json"
    if not path.exists():
        raise DeploymentError(f"provider spec is missing: {path}")
    spec = read_json(path)
    validate_provider_spec(spec)
    return spec


def validate_provider_spec(spec: dict) -> None:
    if spec.get("schema_version") != PROVIDER_SCHEMA_VERSION:
        raise DeploymentError(f"provider schema_version must be {PROVIDER_SCHEMA_VERSION}")
    provider_id = spec.get("id")
    if not isinstance(provider_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", provider_id):
        raise DeploymentError("provider id must use lowercase letters, digits, underscores, or hyphens")
    if spec.get("adapter") not in {"gemini-api-key", "codex-api-key", "openai-compatibility", "oauth"}:
        raise DeploymentError("unsupported provider adapter")
    if spec.get("auth_mode") not in {"api-key", "oauth"}:
        raise DeploymentError("auth_mode must be api-key or oauth")
    base_url = spec.get("base_url")
    if spec["auth_mode"] == "api-key":
        if not isinstance(base_url, str) or not base_url:
            raise DeploymentError("API-key providers require base_url")
        validate_remote_url(base_url)
    models = spec.get("models")
    if not isinstance(models, list) or not models:
        raise DeploymentError("provider models must be a non-empty array")
    slugs: set[str] = set()
    protected_native = set(read_json(SKILL_DIR / "policies" / "catalog.json").get("protected_native_model_ids", []))
    for model in models:
        if not isinstance(model, dict):
            raise DeploymentError("every model must be an object")
        for key in ("upstream_id", "slug"):
            if not isinstance(model.get(key), str) or not model[key]:
                raise DeploymentError(f"model {key} must be a non-empty string")
        if not re.fullmatch(r"[A-Za-z0-9._:/-]+", model["slug"]):
            raise DeploymentError(f"unsupported model slug: {model['slug']}")
        if model["slug"] in slugs:
            raise DeploymentError(f"duplicate model slug: {model['slug']}")
        slugs.add(model["slug"])
        bundled = SKILL_DIR / "models" / f"{model['slug']}.json"
        if not bundled.exists() and model["slug"] not in protected_native:
            required_metadata = {
                "display_name": str,
                "description": str,
                "template_slug": str,
                "context_window": int,
                "effective_context_window_percent": int,
                "default_reasoning_level": str,
                "reasoning_efforts": list,
                "input_modalities": list,
                "priority": int,
            }
            for key, expected in required_metadata.items():
                if not isinstance(model.get(key), expected):
                    raise DeploymentError(f"uncatalogued model {model['slug']} requires {key}")
            if model["default_reasoning_level"] not in model["reasoning_efforts"]:
                raise DeploymentError("default_reasoning_level must be listed in reasoning_efforts")
            if any(item not in {"text", "image"} for item in model["input_modalities"]):
                raise DeploymentError("input_modalities supports only text and image")
    if spec["auth_mode"] == "oauth":
        channel = spec.get("oauth_channel")
        if channel not in {"codex", "antigravity", "grok", "xai", "claude"}:
            raise DeploymentError("unsupported OAuth channel")


def selected_models(spec: dict, raw: str | None) -> list[dict]:
    models = list(spec["models"])
    if not raw:
        return models
    wanted = {item.strip() for item in raw.split(",") if item.strip()}
    selected = [item for item in models if item["slug"] in wanted]
    missing = sorted(wanted - {item["slug"] for item in selected})
    if missing:
        raise DeploymentError(f"models are absent from provider spec: {', '.join(missing)}")
    return selected


def render_provider_entry(spec: dict, models: list[dict], secret: str) -> tuple[str, str]:
    adapter = spec["adapter"]
    if adapter in {"gemini-api-key", "codex-api-key"}:
        lines = [f"- api-key: {yaml_string(secret)}", f"  base-url: {yaml_string(spec['base_url'])}", "  models:"]
        for model in models:
            lines.extend(
                [
                    f"    - name: {yaml_string(model['upstream_id'])}",
                    f"      alias: {yaml_string(model['slug'])}",
                ]
            )
        return adapter, "\n".join(lines)
    if adapter == "openai-compatibility":
        lines = [
            f"- name: {yaml_string(spec['id'])}",
            "  disabled: false",
        ]
        prefix = spec.get("routing_prefix")
        if prefix:
            lines.append(f"  prefix: {yaml_string(prefix)}")
        lines.extend(
            [
                f"  base-url: {yaml_string(spec['base_url'])}",
                "  api-key-entries:",
                f"    - api-key: {yaml_string(secret)}",
                "  models:",
            ]
        )
        for model in models:
            lines.extend(
                [
                    f"    - name: {yaml_string(model['upstream_id'])}",
                    f"      alias: {yaml_string(model['slug'])}",
                ]
            )
        return "openai-compatibility", "\n".join(lines)
    raise DeploymentError("OAuth providers do not write API-key configuration")


def validate_transaction_id(transaction_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", transaction_id):
        raise DeploymentError("invalid transaction id")
    return transaction_id


def transaction_dir(state_dir: Path, transaction_id: str) -> Path:
    validate_transaction_id(transaction_id)
    return state_dir / "transactions" / transaction_id


def begin_transaction(
    state_dir: Path,
    kind: str,
    paths: list[Path],
    transaction_id: str | None = None,
) -> tuple[str, dict]:
    transaction_id = validate_transaction_id(transaction_id or f"{timestamp()}-{kind}")
    root = transaction_dir(state_dir, transaction_id)
    backup_dir = root / "before"
    backup_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    files: list[dict] = []
    for index, path in enumerate(dict.fromkeys(path.resolve() for path in paths)):
        before_sha = sha256_path(path)
        backup_path = None
        if before_sha is not None:
            backup_path = backup_dir / f"{index:03d}-{path.name}"
            shutil.copy2(path, backup_path)
            if os.name != "nt":
                os.chmod(backup_path, 0o600)
        files.append(
            {
                "path": str(path),
                "before_sha256": before_sha,
                "before_mode": mode(path),
                "backup": str(backup_path) if backup_path else None,
                "after_sha256": None,
            }
        )
    payload = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "id": transaction_id,
        "kind": kind,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "files": files,
        "status": "pending",
    }
    atomic_write_text(root / "transaction.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return transaction_id, payload


def finish_transaction(state_dir: Path, payload: dict) -> None:
    for item in payload["files"]:
        item["after_sha256"] = sha256_path(Path(item["path"]))
    payload["status"] = "applied"
    atomic_write_text(
        transaction_dir(state_dir, payload["id"]) / "transaction.json",
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def fail_transaction(
    state_dir: Path,
    payload: dict,
    error: str,
    rollback_completed: bool,
    service_restore: str | None,
) -> None:
    for item in payload["files"]:
        item["after_sha256"] = sha256_path(Path(item["path"]))
    payload["status"] = "failed"
    payload["failure"] = {
        "error": error,
        "rollback_completed": rollback_completed,
        "service_restore": service_restore,
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    atomic_write_text(
        transaction_dir(state_dir, payload["id"]) / "transaction.json",
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def restore_transaction(payload: dict, require_after_match: bool = True) -> None:
    conflicts: list[str] = []
    for item in payload.get("files", []):
        path = Path(item["path"])
        if require_after_match and sha256_path(path) != item.get("after_sha256"):
            conflicts.append(str(path))
    if conflicts:
        raise DeploymentError("rollback refused because files changed: " + ", ".join(conflicts))
    for item in reversed(payload.get("files", [])):
        path = Path(item["path"])
        backup_path = Path(item["backup"]) if item.get("backup") else None
        if backup_path:
            atomic_write_bytes(path, backup_path.read_bytes(), int(item.get("before_mode") or "0o600", 8))
        elif path.exists() or path.is_symlink():
            if path.is_dir() and not path.is_symlink():
                raise DeploymentError(f"rollback will not remove a directory: {path}")
            path.unlink()


def stop_recorded_services(payload: dict) -> None:
    for service in payload.get("services", []):
        kind = service.get("kind")
        identifier = service.get("identifier")
        if not isinstance(identifier, str) or not identifier:
            continue
        if kind == "launchagent" and platform.system() == "Darwin":
            subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}/{identifier}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        elif kind == "systemd-user" and platform.system() == "Linux":
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", identifier],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        elif kind == "task-scheduler" and platform.system() == "Windows":
            subprocess.run(
                ["schtasks", "/Delete", "/F", "/TN", identifier],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def restore_transparent_service(
    service_path: Path,
    windows_task_name: str,
) -> str | None:
    """Reload a restored service definition after an apply transaction fails."""

    if platform.system() == "Darwin":
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/com.zhijian.codex-cli-model-bridge-transparent-proxy"
        loaded = subprocess.run(
            ["launchctl", "print", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        if loaded:
            stopped = subprocess.run(
                ["launchctl", "bootout", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if stopped.returncode != 0:
                return "launchctl could not unload the failed candidate during rollback"
            for _ in range(20):
                if subprocess.run(
                    ["launchctl", "print", target],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                ).returncode != 0:
                    break
                time.sleep(0.5)
            else:
                return "launchctl did not finish rollback unload within 10 seconds"
        for attempt in range(3):
            started = subprocess.run(
                ["launchctl", "bootstrap", domain, str(service_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if started.returncode == 0:
                return None
            if attempt < 2:
                time.sleep(1.0)
        return "launchctl could not restore the previous transparent proxy"
    if platform.system() == "Linux":
        reload_result = subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        restart_result = subprocess.run(
            ["systemctl", "--user", "restart", service_path.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return None if reload_result.returncode == 0 and restart_result.returncode == 0 else "systemd could not restore the previous transparent proxy"
    if platform.system() == "Windows":
        create = subprocess.run(
            [
                "schtasks",
                "/Create",
                "/F",
                "/SC",
                "ONLOGON",
                "/RL",
                "LIMITED",
                "/TN",
                windows_task_name,
                "/TR",
                subprocess.list2cmdline([str(service_path)]),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        result = subprocess.run(
            ["schtasks", "/Run", "/TN", windows_task_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return None if create.returncode == 0 and result.returncode == 0 else "Task Scheduler could not restore the previous transparent proxy"
    return "unsupported platform for automatic service rollback"


def restore_recorded_services(payload: dict) -> list[str]:
    errors: list[str] = []
    for service in payload.get("services", []):
        if not service.get("restart_after_restore"):
            continue
        definition = service.get("definition_path")
        if not isinstance(definition, str) or not definition:
            errors.append("transaction is missing the previous service definition path")
            continue
        error = restore_transparent_service(
            Path(definition),
            str(service.get("windows_task_name") or "CodexCliModelBridgeTransparentProxy"),
        )
        if error:
            errors.append(error)
    return errors


def credential_path(state_dir: Path, provider_id: str) -> Path:
    return state_dir / "credentials" / f"{provider_id}.secret"


def cmd_credential_set(args: argparse.Namespace) -> None:
    state_dir = Path(args.state_dir).expanduser()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", args.provider):
        emit({"status": "blocked", "error": "invalid provider id", "secrets_redacted": True}, 2)
    try:
        if args.stdin:
            value = sys.stdin.readline().rstrip("\r\n")
        else:
            value = getpass.getpass(f"API key for {args.provider}: ")
        if not value or any(char in value for char in "\r\n"):
            raise DeploymentError("credential must be one non-empty line")
        target = credential_path(state_dir, args.provider)
        atomic_write_text(target, value + "\n", 0o600)
        emit(
            {
                "status": "stored",
                "provider": args.provider,
                "credential_file": str(target),
                "secrets_redacted": True,
            }
        )
    except (DeploymentError, OSError) as exc:
        emit({"status": "blocked", "error": str(exc), "secrets_redacted": True}, 2)


def cmd_credential_login(args: argparse.Namespace) -> None:
    try:
        spec = load_provider_spec(args.preset, args.spec)
        if spec["auth_mode"] != "oauth":
            raise DeploymentError("credential login requires an OAuth provider preset")
        notice = spec.get("terms_notice", "Review the provider terms before authorizing this third-party client.")
        if not args.ack_provider_terms:
            emit(
                {
                    "status": "needs_acknowledgement",
                    "provider": spec["id"],
                    "notice": notice,
                    "next": "repeat with --ack-provider-terms --apply from a local terminal",
                    "secrets_redacted": True,
                },
                2,
            )
        if not args.apply:
            emit(
                {
                    "status": "planned",
                    "provider": spec["id"],
                    "oauth_channel": spec["oauth_channel"],
                    "notice": notice,
                    "secrets_redacted": True,
                }
            )
        flags = {
            "codex": "--codex-login",
            "antigravity": "--antigravity-login",
            "grok": "--grok-login",
            "xai": "--xai-login",
            "claude": "--claude-login",
        }
        state_dir = Path(args.state_dir).expanduser()
        binary = Path(args.proxy_binary).expanduser() if args.proxy_binary else runtime_binary(state_dir)
        if binary is None:
            raise DeploymentError("CLIProxyAPI binary is missing; run bootstrap first")
        proc = subprocess.run(
            [str(binary), "--config", str(Path(args.proxy_config).expanduser()), flags[spec["oauth_channel"]]],
            check=False,
        )
        emit(
            {
                "status": "completed" if proc.returncode == 0 else "blocked",
                "provider": spec["id"],
                "returncode": proc.returncode,
                "secrets_redacted": True,
            },
            0 if proc.returncode == 0 else 2,
        )
    except (DeploymentError, OSError) as exc:
        emit({"status": "blocked", "error": str(exc), "secrets_redacted": True}, 2)


def cmd_provider_add(args: argparse.Namespace) -> None:
    try:
        spec = load_provider_spec(args.preset, args.spec)
        if args.base_url:
            spec["base_url"] = args.base_url
            validate_remote_url(args.base_url)
        models = selected_models(spec, args.models)
        summary = redact_provider_summary({**spec, "models": models})
        config_path = Path(args.proxy_config).expanduser()
        state_dir = Path(args.state_dir).expanduser()
        secret_path = credential_path(state_dir, spec["id"])
        current = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        current_sha = sha256_bytes(current.encode())
        aliases_present = [
            model["slug"]
            for model in models
            if re.search(rf'^\s+alias:\s*["\']?{re.escape(model["slug"])}["\']?\s*(?:#.*)?$', current, re.MULTILINE)
        ]
        provider_named = bool(
            re.search(rf'^\s+(?:-\s+)?name:\s*["\']?{re.escape(spec["id"])}["\']?\s*(?:#.*)?$', current, re.MULTILINE)
        )
        base_present = bool(spec.get("base_url") and spec["base_url"] in current)
        if len(aliases_present) == len(models) and (
            spec["auth_mode"] == "oauth" or provider_named or base_present
        ):
            emit(
                {
                    "status": "unchanged",
                    "provider": summary,
                    "proxy_config": str(config_path),
                    "config_sha256": current_sha,
                    "next": "run sync and verify",
                    "secrets_redacted": True,
                }
            )
        if spec["auth_mode"] == "api-key" and not secret_path.exists():
            emit(
                {
                    "status": "needs_credential",
                    "provider": summary,
                    "next": f"run credential set --provider {spec['id']} in a local terminal",
                    "secrets_redacted": True,
                },
                2 if args.apply else 0,
            )
        if args.expected_sha256 and current_sha != args.expected_sha256:
            raise DeploymentError("proxy config changed after approval")
        protected = set(read_json(SKILL_DIR / "policies" / "catalog.json").get("protected_native_model_ids", []))
        collisions = sorted(protected & {model["slug"] for model in models})
        if collisions and spec.get("oauth_channel") != "codex":
            raise DeploymentError("provider models collide with protected native IDs: " + ", ".join(collisions))
        if aliases_present:
            raise DeploymentError("model aliases already exist: " + ", ".join(sorted(aliases_present)))
        if spec["auth_mode"] == "oauth":
            updated = append_oauth_aliases(current, spec["oauth_channel"], models)
        else:
            secret = secret_path.read_text(encoding="utf-8").strip()
            section, entry = render_provider_entry(spec, models, secret)
            updated = append_yaml_list_entry(current, section, entry, spec["id"])
        result = {
            "status": "planned" if not args.apply else "unchanged",
            "provider": summary,
            "proxy_config": str(config_path),
            "config_sha256": current_sha,
            "changed": updated != current,
            "secrets_redacted": True,
        }
        if not args.apply:
            emit(result)
        local_manifests = [
            state_dir / "models.d" / f"{model['slug']}.json"
            for model in models
            if not (SKILL_DIR / "models" / f"{model['slug']}.json").exists()
        ]
        transaction_id, tx = begin_transaction(state_dir, "provider-add", [config_path, *local_manifests])
        if updated != current:
            atomic_write_text(config_path, updated, 0o600)
            result["status"] = "applied"
        for model, target in zip(
            [item for item in models if not (SKILL_DIR / "models" / f"{item['slug']}.json").exists()],
            local_manifests,
        ):
            manifest = {
                "schema_version": 1,
                **{key: value for key, value in model.items() if key != "upstream_id"},
            }
            atomic_write_text(target, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", 0o600)
        if spec["auth_mode"] == "api-key":
            secret_path.unlink(missing_ok=True)
        finish_transaction(state_dir, tx)
        result["transaction"] = transaction_id
        result["credential_staging_removed"] = spec["auth_mode"] == "api-key"
        result["next"] = (
            "run credential login, then sync and verify"
            if spec["auth_mode"] == "oauth"
            else "reload CLIProxyAPI if hot reload is disabled, then run sync and verify"
        )
        emit(result)
    except (DeploymentError, OSError, json.JSONDecodeError) as exc:
        emit({"status": "blocked", "error": str(exc), "secrets_redacted": True}, 2)


def normalized_machine() -> tuple[str, list[str]]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_token = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(system)
    if not os_token:
        raise DeploymentError(f"unsupported operating system: {platform.system()}")
    aliases = {
        "x86_64": ["amd64", "x86_64"],
        "amd64": ["amd64", "x86_64"],
        "arm64": ["arm64", "aarch64"],
        "aarch64": ["aarch64", "arm64"],
    }.get(machine)
    if not aliases:
        raise DeploymentError(f"unsupported architecture: {platform.machine()}")
    return os_token, aliases


def select_release_asset(release: dict) -> tuple[dict, dict]:
    os_token, arch_aliases = normalized_machine()
    assets = release.get("assets", [])
    archives = [
        item
        for item in assets
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and os_token in item["name"].lower()
        and any(alias in item["name"].lower() for alias in arch_aliases)
        and item["name"].lower().endswith((".tar.gz", ".zip"))
        and "no-plugin" not in item["name"].lower()
    ]
    if not archives:
        raise DeploymentError("no release asset matches this OS and architecture")
    archive = sorted(archives, key=lambda item: item["name"])[0]
    checksum = next(
        (item for item in assets if isinstance(item, dict) and item.get("name") == "checksums.txt"),
        None,
    )
    if not checksum:
        raise DeploymentError("release does not publish checksums.txt")
    return archive, checksum


def fetch_bytes(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "codex-cli-model-bridge/2"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_release(version: str, api_root: str) -> dict:
    url = f"{api_root}/latest" if version == "latest" else f"{api_root}/tags/{version}"
    payload = json.loads(fetch_bytes(url).decode("utf-8"))
    if not isinstance(payload, dict):
        raise DeploymentError("GitHub release response is invalid")
    return payload


def checksum_for(checksums: bytes, asset_name: str) -> str:
    for raw in checksums.decode("utf-8").splitlines():
        parts = raw.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
            digest = parts[0].lower()
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                return digest
    raise DeploymentError(f"checksum is missing for {asset_name}")


def safe_extract_binary(archive_data: bytes, asset_name: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="codex-model-bridge-release-") as raw:
        root = Path(raw)
        archive_path = root / asset_name
        archive_path.write_bytes(archive_data)
        extract_dir = root / "extract"
        extract_dir.mkdir()
        if asset_name.lower().endswith(".zip"):
            with zipfile.ZipFile(archive_path) as bundle:
                for member in bundle.infolist():
                    target = (extract_dir / member.filename).resolve()
                    if extract_dir.resolve() not in target.parents and target != extract_dir.resolve():
                        raise DeploymentError("release archive contains an unsafe path")
                bundle.extractall(extract_dir)
        else:
            with tarfile.open(archive_path, "r:gz") as bundle:
                for member in bundle.getmembers():
                    target = (extract_dir / member.name).resolve()
                    if extract_dir.resolve() not in target.parents and target != extract_dir.resolve():
                        raise DeploymentError("release archive contains an unsafe path")
                    if member.issym() or member.islnk():
                        raise DeploymentError("release archive contains a link")
                    if not member.isfile() and not member.isdir():
                        raise DeploymentError("release archive contains a special file")
                bundle.extractall(extract_dir)
        candidates = [
            path
            for path in extract_dir.rglob("*")
            if path.is_file() and path.name.lower() in {"cli-proxy-api", "cli-proxy-api.exe", "cliproxyapi", "cliproxyapi.exe"}
        ]
        if len(candidates) != 1:
            raise DeploymentError("release archive does not contain exactly one CLIProxyAPI binary")
        return candidates[0].read_bytes()


def proxy_config_source(auth_dir: Path) -> str:
    local_key = secrets.token_urlsafe(32)
    return "\n".join(
        [
            'host: "127.0.0.1"',
            "port: 8317",
            f"auth-dir: {yaml_string(str(auth_dir))}",
            "remote-management:",
            "  allow-remote: false",
            '  secret-key: ""',
            "api-keys:",
            f"  - {yaml_string(local_key)}",
            "logging-to-file: true",
            "",
        ]
    )


def proxy_launch_agent(binary: Path, config: Path) -> bytes:
    payload = {
        "Label": PROXY_SERVICE_LABEL,
        "ProgramArguments": [str(binary), "--config", str(config)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 5,
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def proxy_systemd_unit(binary: Path, config: Path) -> str:
    command = " ".join(subprocess.list2cmdline([part]) for part in [str(binary), "--config", str(config)])
    return "\n".join(
        [
            "[Unit]",
            "Description=CLIProxyAPI for Codex CLI Model Bridge",
            "After=network.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={command}",
            "Restart=always",
            "RestartSec=3",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def start_proxy_service(binary: Path, config: Path, service_path: Path | None, task_name: str) -> str:
    system = platform.system()
    if system == "Darwin":
        if service_path is None:
            raise DeploymentError("launch agent path is missing")
        atomic_write_bytes(service_path, proxy_launch_agent(binary, config), 0o600)
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/{PROXY_SERVICE_LABEL}"
        loaded = subprocess.run(["launchctl", "print", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        command = ["launchctl", "kickstart", "-k", target] if loaded else ["launchctl", "bootstrap", domain, str(service_path)]
        if subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            raise DeploymentError("launchctl failed to start CLIProxyAPI")
        return "launchagent"
    if system == "Linux":
        if service_path is None or not shutil.which("systemctl"):
            raise DeploymentError("systemd --user is required for managed Linux service mode")
        atomic_write_text(service_path, proxy_systemd_unit(binary, config), 0o600)
        if subprocess.run(["systemctl", "--user", "daemon-reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            raise DeploymentError("systemd daemon-reload failed")
        if subprocess.run(["systemctl", "--user", "enable", "--now", service_path.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            raise DeploymentError("systemd failed to enable CLIProxyAPI")
        return "systemd-user"
    if system == "Windows":
        task_command = subprocess.list2cmdline([str(binary), "--config", str(config)])
        create = subprocess.run(
            ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED", "/TN", task_name, "/TR", task_command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if create.returncode != 0:
            raise DeploymentError("Task Scheduler failed to register CLIProxyAPI")
        subprocess.run(["schtasks", "/Run", "/TN", task_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "task-scheduler"
    raise DeploymentError(f"unsupported service platform: {system}")


def loopback_port_open(port: int = 8317) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def runtime_binary(state_dir: Path) -> Path | None:
    runtime_state = state_dir / "runtime.json"
    if runtime_state.exists():
        try:
            value = read_json(runtime_state).get("binary")
            if isinstance(value, str) and Path(value).expanduser().exists():
                return Path(value).expanduser()
        except (OSError, json.JSONDecodeError, DeploymentError):
            pass
    discovered = next(
        (shutil.which(name) for name in ("cliproxyapi", "cli-proxy-api", "CLIProxyAPI") if shutil.which(name)),
        None,
    )
    return Path(discovered) if discovered else None


def cmd_bootstrap(args: argparse.Namespace) -> None:
    state_dir = Path(args.state_dir).expanduser()
    install_dir = Path(args.install_dir).expanduser()
    discovered = runtime_binary(state_dir)
    binary = (
        Path(args.proxy_binary).expanduser()
        if args.proxy_binary
        else discovered
        if discovered
        else install_dir / ("cli-proxy-api.exe" if os.name == "nt" else "cli-proxy-api")
    )
    config = Path(args.proxy_config).expanduser()
    existing_binary = binary.exists()
    config_exists = config.exists()
    proxy_running = loopback_port_open()
    runtime_state_path = state_dir / "runtime.json"
    result = {
        "status": "planned" if not args.apply else "unchanged",
        "binary": str(binary),
        "binary_action": "adopt" if existing_binary else "install",
        "proxy_config": str(config),
        "config_action": "adopt" if config_exists else "create",
        "service": "adopt-running" if proxy_running else ("skip" if args.no_service else "create"),
        "release": args.version,
        "port_conflict": proxy_running and not (existing_binary and config_exists),
        "secrets_redacted": True,
    }
    if not args.apply:
        emit(result)
    if result["port_conflict"]:
        emit(
            {
                "status": "blocked",
                "error": "127.0.0.1:8317 is already occupied but the selected binary/config cannot be adopted",
                "secrets_redacted": True,
            },
            2,
        )
    service_path: Path | None = None
    if platform.system() == "Darwin":
        service_path = Path("~/Library/LaunchAgents/com.jimu.codex-cli-model-bridge-cliproxyapi.plist").expanduser()
    elif platform.system() == "Linux":
        service_path = Path("~/.config/systemd/user/codex-cli-model-bridge-cliproxyapi.service").expanduser()
    paths = [binary, config, runtime_state_path] + ([service_path] if service_path else [])
    transaction_id, tx = begin_transaction(state_dir, "bootstrap", paths)
    try:
        if not existing_binary:
            release = fetch_release(args.version, args.release_api)
            archive, checksum_asset = select_release_asset(release)
            archive_data = fetch_bytes(archive["browser_download_url"], timeout=180)
            checksums = fetch_bytes(checksum_asset["browser_download_url"])
            expected = checksum_for(checksums, archive["name"])
            actual = sha256_bytes(archive_data)
            if actual != expected:
                raise DeploymentError("release checksum mismatch")
            binary_data = safe_extract_binary(archive_data, archive["name"])
            atomic_write_bytes(binary, binary_data, 0o700)
            result["installed_release"] = release.get("tag_name")
            result["archive_sha256"] = actual
        if not config_exists:
            auth_dir = state_dir / "auth"
            auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write_text(config, proxy_config_source(auth_dir), 0o600)
        if not args.no_service and not proxy_running:
            result["service"] = start_proxy_service(binary, config, service_path, args.windows_task_name)
            tx["services"] = [
                {
                    "kind": result["service"],
                    "identifier": (
                        PROXY_SERVICE_LABEL
                        if result["service"] == "launchagent"
                        else service_path.name
                        if result["service"] == "systemd-user" and service_path
                        else args.windows_task_name
                    ),
                }
            ]
        atomic_write_text(
            runtime_state_path,
            json.dumps(
                {
                    "schema_version": 1,
                    "binary": str(binary),
                    "proxy_config": str(config),
                    "service": result["service"],
                    "release": result.get("installed_release") or args.version,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            0o600,
        )
        finish_transaction(state_dir, tx)
        result["status"] = "applied"
        result["transaction"] = transaction_id
        result["config_mode"] = mode(config)
        emit(result)
    except Exception as exc:
        tx["status"] = "failed"
        for item in tx["files"]:
            item["after_sha256"] = sha256_path(Path(item["path"]))
        try:
            stop_recorded_services(tx)
            restore_transaction(tx, require_after_match=False)
        except Exception:
            pass
        emit({"status": "blocked", "error": str(exc), "transaction": transaction_id, "secrets_redacted": True}, 2)


def run_bridge(arguments: list[str], timeout: int = 300) -> dict:
    proc = subprocess.run(
        [sys.executable, str(BRIDGE_SCRIPT), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise DeploymentError(f"bridge command failed without JSON: {' '.join(arguments)}") from exc
    if proc.returncode != 0:
        raise DeploymentError(payload.get("error") or f"bridge command failed: {' '.join(arguments)}")
    return payload


def cmd_activate(args: argparse.Namespace) -> None:
    state_dir = Path(args.state_dir).expanduser()
    codex_home = Path(args.codex_home).expanduser()
    proxy_config = Path(args.proxy_config).expanduser()
    catalog = codex_home / "model-catalog-cli-proxy.json"
    profile = codex_home / "cli-proxy.config.toml"
    helper = Path("~/.config/codex-cli-proxy/read-client-key.py").expanduser()
    root_config = codex_home / "config.toml"
    profile_tracked = [profile, catalog, helper]
    runtime = state_dir / "transparent_proxy.py"
    if platform.system() == "Darwin":
        service_path = Path("~/Library/LaunchAgents/com.zhijian.codex-cli-model-bridge-transparent-proxy.plist").expanduser()
        service_record = {
            "kind": "launchagent",
            "identifier": "com.zhijian.codex-cli-model-bridge-transparent-proxy",
        }
    elif platform.system() == "Linux":
        service_path = Path("~/.config/systemd/user/codex-cli-model-bridge-transparent-proxy.service").expanduser()
        service_record = {"kind": "systemd-user", "identifier": service_path.name}
    else:
        service_path = None
        service_record = {"kind": "task-scheduler", "identifier": "CodexCliModelBridgeTransparentProxy"}
    desktop_tracked = [root_config, runtime, runtime.with_name("compaction.py")] + (
        [service_path] if service_path else []
    )
    if platform.system() == "Windows":
        desktop_tracked.append(runtime.with_name("transparent_proxy.cmd"))
    plan = {
        "status": "planned",
        "mode": args.mode,
        "always_create_profile": True,
        "desktop_fallback": "profile",
        "tracked_files": [str(path) for path in profile_tracked + desktop_tracked],
    }
    if not args.apply:
        emit(plan)
    profile_transaction_id, profile_tx = begin_transaction(state_dir, "activate-profile", profile_tracked)
    try:
        run_bridge(["configure", "--profile-config", str(profile), "--catalog", str(catalog), "--helper", str(helper), "--proxy-config", str(proxy_config), "--apply"])
        sync_args = ["sync", "--config", str(profile), "--catalog", str(catalog), "--state-dir", str(state_dir), "--apply"]
        if args.models:
            sync_args.extend(["--models", args.models])
        run_bridge(sync_args)
        finish_transaction(state_dir, profile_tx)
        if args.mode == "profile":
            emit(
                {
                    "status": "applied",
                    "mode": "profile",
                    "profile": str(profile),
                    "catalog": str(catalog),
                    "transaction": profile_transaction_id,
                    "secrets_redacted": True,
                }
            )

        desktop_transaction_id, desktop_tx = begin_transaction(state_dir, "activate-desktop", desktop_tracked)
        desktop_tx["services"] = [service_record]
        try:
            preview = run_bridge(
                [
                    "configure-desktop",
                    "--config",
                    str(root_config),
                    "--state-db",
                    str(codex_home / "state_5.sqlite"),
                    "--auth-file",
                    str(codex_home / "auth.json"),
                    "--catalog",
                    str(catalog),
                    "--helper",
                    str(helper),
                    "--proxy-config",
                    str(proxy_config),
                    "--runtime-script",
                    str(runtime),
                ]
            )
            desktop = run_bridge(
                [
                    "configure-desktop",
                    "--config",
                    str(root_config),
                    "--state-db",
                    str(codex_home / "state_5.sqlite"),
                    "--auth-file",
                    str(codex_home / "auth.json"),
                    "--catalog",
                    str(catalog),
                    "--helper",
                    str(helper),
                    "--proxy-config",
                    str(proxy_config),
                    "--runtime-script",
                    str(runtime),
                    "--expected-sha256",
                    preview["config_sha256"],
                    "--apply",
                ]
            )
            finish_transaction(state_dir, desktop_tx)
            emit(
                {
                    "status": "applied",
                    "mode": "desktop",
                    "profile": str(profile),
                    "catalog": str(catalog),
                    "desktop": desktop,
                    "transactions": [profile_transaction_id, desktop_transaction_id],
                    "secrets_redacted": True,
                }
            )
        except Exception as exc:
            for item in desktop_tx["files"]:
                item["after_sha256"] = sha256_path(Path(item["path"]))
            stop_recorded_services(desktop_tx)
            restore_transaction(desktop_tx, require_after_match=False)
            emit(
                {
                    "status": "profile_fallback",
                    "error": str(exc),
                    "profile": str(profile),
                    "profile_transaction": profile_transaction_id,
                    "desktop_transaction": desktop_transaction_id,
                    "root_config_restored": True,
                    "secrets_redacted": True,
                },
                2,
            )
    except Exception as exc:
        for item in profile_tx["files"]:
            item["after_sha256"] = sha256_path(Path(item["path"]))
        try:
            restore_transaction(profile_tx, require_after_match=False)
        except Exception:
            pass
        emit(
            {
                "status": "blocked",
                "error": str(exc),
                "transaction": profile_transaction_id,
                "root_config_restored": True,
                "secrets_redacted": True,
            },
            2,
        )


def proxy_token_from_config(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_keys = False
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("api-keys:"):
            in_keys = True
            rest = stripped.split(":", 1)[1].strip()
            if rest.startswith("[") and rest.endswith("]"):
                inner = rest[1:-1].strip().strip("'\"")
                if inner:
                    return inner
            continue
        if in_keys and stripped.startswith("-"):
            value = stripped[1:].strip().strip("'\"")
            if value:
                return value
        if in_keys and stripped and not raw[0].isspace() and not stripped.startswith("#"):
            break
    raise DeploymentError("CLIProxyAPI client key is missing")


def _health_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/__codex_bridge_health", "", "", ""))


def _parse_sse(raw: bytes) -> list[dict]:
    events: list[dict] = []
    for line in raw.splitlines():
        if not line.startswith(b"data:"):
            continue
        value = line[5:].strip()
        if not value or value == b"[DONE]":
            continue
        try:
            event = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _post_response(
    base_url: str,
    path: str,
    payload: dict,
    timeout: int,
    token: str = "codex-bridge-probe",
    attempts: int = 3,
) -> dict:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/{path.lstrip('/')}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if payload.get("stream") is True else "application/json",
            "Accept-Encoding": "identity",
            "x-codex-beta-features": "remote_compaction_v2",
        },
        method="POST",
    )
    raw = b""
    content_type = ""
    status: int | None = None
    attempt_count = 0
    for attempt in range(max(1, attempts)):
        attempt_count = attempt + 1
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                status = response.status
            break
        except urllib.error.HTTPError as exc:
            status = exc.code
            if exc.code not in {429, 500, 502, 503, 504} or attempt + 1 >= max(1, attempts):
                return {"http_status": exc.code, "error": f"HTTP_{exc.code}", "attempts": attempt_count}
        except (OSError, TimeoutError) as exc:
            if attempt + 1 >= max(1, attempts):
                return {"http_status": None, "error": type(exc).__name__, "attempts": attempt_count}
        time.sleep(min(2.0, 0.25 * (2**attempt)))
    if status is None:
        return {"http_status": None, "error": "unavailable", "attempts": attempt_count}
    if "text/event-stream" in content_type or payload.get("stream") is True:
        events = _parse_sse(raw)
        completed = next(
            (
                event.get("response")
                for event in reversed(events)
                if event.get("type") == "response.completed" and isinstance(event.get("response"), dict)
            ),
            None,
        )
        return {"http_status": status, "events": events, "response": completed, "attempts": attempt_count}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {"http_status": status, "error": "invalid_json", "attempts": attempt_count}
    return {"http_status": status, "response": decoded, "attempts": attempt_count}


def _response_output(result: dict) -> list[dict]:
    response = result.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("output"), list):
        return []
    return [item for item in response["output"] if isinstance(item, dict)]


def _compaction_item(result: dict) -> dict | None:
    items = [item for item in _response_output(result) if item.get("type") in COMPACTION_ITEM_TYPES]
    return items[0] if len(items) == 1 else None


def _legacy_output_text(result: dict) -> str:
    values: list[str] = []
    for item in _response_output(result):
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                values.append(block["text"])
    return "\n".join(values)


def compaction_probe(base_url: str, token: str, model: str, timeout: int) -> dict:
    """Backward-compatible v2 probe used by callers of the V2.0 API."""

    result = _post_response(
        base_url,
        "responses",
        {
            "model": model,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Preserve exactly: bridge-probe."}],
                },
                {"type": "compaction_trigger"},
            ],
            "stream": False,
        },
        timeout,
        token,
    )
    item = _compaction_item(result)
    output_types = [value.get("type") for value in _response_output(result)]
    if item:
        return {
            "status": "supported",
            "output_types": output_types,
            "synthetic": str(item.get("encrypted_content", "")).startswith(OCX_COMPACTION_PREFIX),
            "attempts": result.get("attempts"),
        }
    if result.get("http_status") is None:
        return {"status": "unavailable", "error": result.get("error", "unavailable")}
    return {
        "status": "incompatible",
        "http_status": result.get("http_status"),
        "output_types": output_types,
        "attempts": result.get("attempts"),
    }


def _legacy_compaction_probe(base_url: str, model: str, marker: str, timeout: int) -> dict:
    result = _post_response(
        base_url,
        "responses/compact",
        {
            "model": model,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"Preserve exactly: {marker}"}],
                }
            ],
            "stream": False,
        },
        timeout,
    )
    output = _response_output(result)
    text = _legacy_output_text(result)
    ready = result.get("http_status") == 200 and bool(output) and marker in text
    return {
        "status": "supported" if ready else "incompatible",
        "http_status": result.get("http_status"),
        "replacement_items": len(output),
        "marker_preserved": marker in text,
        "attempts": result.get("attempts"),
    }


def _v2_compaction_probe(base_url: str, model: str, marker: str, timeout: int) -> tuple[dict, dict | None]:
    result = _post_response(
        base_url,
        "responses",
        {
            "model": model,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"Preserve exactly: {marker}"}],
                },
                {"type": "compaction_trigger"},
            ],
            "stream": True,
        },
        timeout,
    )
    item = _compaction_item(result)
    event_types = [event.get("type") for event in result.get("events", []) if isinstance(event, dict)]
    relevant = [
        value
        for value in event_types
        if value in {
            "response.created",
            "response.output_item.added",
            "response.output_item.done",
            "response.completed",
        }
    ]
    event_order_ok = relevant == [
        "response.created",
        "response.output_item.added",
        "response.output_item.done",
        "response.completed",
    ]
    ready = result.get("http_status") == 200 and item is not None and event_order_ok
    return (
        {
            "status": "supported" if ready else "incompatible",
            "http_status": result.get("http_status"),
            "event_order_ok": event_order_ok,
            "compaction_items": 1 if item else 0,
            "synthetic": bool(
                item and str(item.get("encrypted_content", "")).startswith(OCX_COMPACTION_PREFIX)
            ),
            "attempts": result.get("attempts"),
        },
        item,
    )


def _completion_probe(base_url: str, model: str, marker: str, timeout: int, history: list[dict] | None = None) -> dict:
    items = list(history or [])
    items.append(
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": f"Reply with exactly: {marker}"}],
        }
    )
    result = _post_response(
        base_url,
        "responses",
        {"model": model, "input": items, "stream": False, "store": False},
        timeout,
    )
    text = extract_output_text(result.get("response"))
    return {
        "status": "supported" if result.get("http_status") == 200 and marker in text else "incompatible",
        "http_status": result.get("http_status"),
        "marker_returned": marker in text,
        "attempts": result.get("attempts"),
    }


def verify_model_compaction(base_url: str, model: str, timeout: int) -> dict:
    marker = f"bridge-compaction-marker-{hashlib.sha256(f'{model}-{time.time_ns()}'.encode()).hexdigest()[:16]}"
    legacy = _legacy_compaction_probe(base_url, model, marker, timeout)
    v2, item = _v2_compaction_probe(base_url, model, marker, timeout)
    replay = (
        _completion_probe(base_url, model, marker, timeout, [item])
        if item
        else {"status": "incompatible", "http_status": None, "marker_returned": False}
    )
    ordinary_marker = f"bridge-normal-{hashlib.sha256(marker.encode()).hexdigest()[:12]}"
    ordinary = _completion_probe(base_url, model, ordinary_marker, timeout)
    ready = all(value.get("status") == "supported" for value in (legacy, v2, replay, ordinary))
    return {
        "status": "ready" if ready else "blocked",
        "legacy_v1": legacy,
        "remote_v2": v2,
        "replay": replay,
        "ordinary_response": ordinary,
        "secrets_redacted": True,
    }


def set_toml_table_bool(text: str, table: str, key: str, value: bool) -> str:
    rendered = "true" if value else "false"
    lines = text.splitlines(keepends=True)
    table_pattern = re.compile(rf"^\s*\[{re.escape(table)}\]\s*$")
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    table_start = next((index for index, line in enumerate(lines) if table_pattern.match(line.rstrip("\r\n"))), None)
    if table_start is None:
        prefix = text.rstrip() + ("\n\n" if text.strip() else "")
        return prefix + f"[{table}]\n{key} = {rendered}\n"
    table_end = next(
        (index for index in range(table_start + 1, len(lines)) if lines[index].lstrip().startswith("[")),
        len(lines),
    )
    for index in range(table_start + 1, table_end):
        if key_pattern.match(lines[index]):
            lines[index] = f"{key} = {rendered}\n"
            return "".join(lines)
    lines.insert(table_end, f"{key} = {rendered}\n")
    return "".join(lines)


def _get_health(base_url: str, timeout: int = 5) -> dict | None:
    try:
        with urllib.request.urlopen(_health_url(base_url), timeout=timeout) as response:
            payload = json.load(response)
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _live_model_ids(base_url: str, timeout: int = 10) -> set[str]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": "Bearer codex-bridge-probe"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    return {
        item["id"]
        for item in payload.get("data", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _catalog_model_ids(path: Path) -> list[str]:
    payload = read_json(path)
    models = payload.get("models")
    if not isinstance(models, list):
        raise DeploymentError("model catalog has no models array")
    return [
        item["slug"]
        for item in models
        if isinstance(item, dict) and isinstance(item.get("slug"), str)
    ]


def _selected_compaction_models(raw: str, catalog: Path) -> list[str]:
    if raw.strip().lower() == "all":
        return _catalog_model_ids(catalog)
    models = [item.strip() for item in raw.split(",") if item.strip()]
    if not models:
        raise DeploymentError("--models is empty")
    return list(dict.fromkeys(models))


def cmd_compaction_audit(args: argparse.Namespace) -> None:
    config_path = Path(args.config).expanduser()
    catalog_path = Path(args.catalog).expanduser()
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        models = _selected_compaction_models(args.models, catalog_path)
        live = _live_model_ids(args.proxy_url, args.timeout)
        health = _get_health(args.proxy_url)
        matrix: dict[str, dict] = {}

        def inspect(model: str) -> tuple[str, dict]:
            if model not in live:
                return model, {"status": "unavailable", "legacy_v1": None, "remote_v2": None}
            marker = f"bridge-audit-{hashlib.sha256(f'{model}-{time.time_ns()}'.encode()).hexdigest()[:12]}"
            legacy = _legacy_compaction_probe(args.proxy_url, model, marker, args.timeout)
            v2, _item = _v2_compaction_probe(args.proxy_url, model, marker, args.timeout)
            status = "ready" if legacy["status"] == "supported" and v2["status"] == "supported" else "blocked"
            return model, {"status": status, "legacy_v1": legacy, "remote_v2": v2}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            for model, result in pool.map(inspect, models):
                matrix[model] = result
        flag = config.get("features", {}).get("remote_compaction_v2")
        protocol = (health or {}).get("compaction", {}).get("protocol_version")
        optional = {item.strip() for item in args.optional_model.split(",") if item.strip()}
        blocking = [
            model
            for model, result in matrix.items()
            if result["status"] == "blocked" or (result["status"] == "unavailable" and model not in optional)
        ]
        ready = flag is True and protocol == "2" and not blocking
        emit(
            {
                "status": "ready" if ready else "blocked",
                "config": str(config_path),
                "remote_compaction_v2": flag,
                "legacy_false_is_blocking": flag is False,
                "proxy_url": args.proxy_url,
                "proxy_health": health,
                "live_model_count": len(live),
                "matrix": matrix,
                "blocking_models": blocking,
                "secrets_redacted": True,
            },
            0 if ready else 2,
        )
    except (DeploymentError, OSError, ValueError, tomllib.TOMLDecodeError, urllib.error.URLError) as exc:
        emit({"status": "blocked", "error": str(exc), "secrets_redacted": True}, 2)


def _compaction_configure_bridge_args(args: argparse.Namespace, expected_sha256: str | None = None) -> list[str]:
    values = [
        "configure-desktop",
        "--config",
        str(Path(args.config).expanduser()),
        "--state-db",
        str(Path(args.state_db).expanduser()),
        "--catalog",
        str(Path(args.catalog).expanduser()),
        "--auth-file",
        str(Path(args.auth_file).expanduser()),
        "--helper",
        str(Path(args.helper).expanduser()),
        "--proxy-url",
        args.upstream_url,
        "--transparent-url",
        args.proxy_url,
        "--runtime-script",
        str(Path(args.runtime_script).expanduser()),
        "--launch-agent",
        str(Path(args.launch_agent).expanduser()),
        "--systemd-unit",
        str(Path(args.systemd_unit).expanduser()),
        "--windows-task-name",
        args.windows_task_name,
    ]
    if expected_sha256:
        values.extend(["--expected-sha256", expected_sha256])
    return values


def cmd_compaction_configure(args: argparse.Namespace) -> None:
    state_dir = Path(args.state_dir).expanduser()
    config_path = Path(args.config).expanduser()
    auth_path = Path(args.auth_file).expanduser()
    runtime = Path(args.runtime_script).expanduser()
    service_path = (
        Path(args.launch_agent).expanduser()
        if platform.system() == "Darwin"
        else Path(args.systemd_unit).expanduser()
        if platform.system() == "Linux"
        else runtime.with_name("transparent_proxy.cmd")
    )
    tracked = [config_path, runtime, runtime.with_name("compaction.py"), service_path]
    try:
        current = config_path.read_text(encoding="utf-8")
        parsed_before = tomllib.loads(current)
        current_sha = sha256_bytes(current.encode())
        if args.expected_sha256 and args.expected_sha256 != current_sha:
            raise DeploymentError("Codex config changed after approval")
        if args.apply and not args.expected_sha256:
            raise DeploymentError("--apply requires the config SHA-256 from an approved dry-run")
        if args.apply and not args.transaction_id:
            raise DeploymentError("--apply requires the transaction id from an approved dry-run")
        planned_transaction_id = validate_transaction_id(
            args.transaction_id or f"{timestamp()}-compaction-configure"
        )
        preview = run_bridge(_compaction_configure_bridge_args(args))
        resolved_tracked = list(dict.fromkeys(path.resolve() for path in tracked))
        backup_root = transaction_dir(state_dir, planned_transaction_id) / "before"
        backup_plan = [
            {
                "path": str(path),
                "backup": str(backup_root / f"{index:03d}-{path.name}") if path.exists() else None,
                "current_sha256": sha256_path(path),
            }
            for index, path in enumerate(resolved_tracked)
        ]
        result = {
            "status": "planned",
            "deprecated_alias": bool(getattr(args, "deprecated_alias", False)),
            "setting": "features.remote_compaction_v2=true",
            "config_sha256": current_sha,
            "diff": preview.get("diff"),
            "runtime_script": str(runtime),
            "compaction_runtime": str(runtime.with_name("compaction.py")),
            "service": str(service_path),
            "proxy_url": args.proxy_url,
            "upstream_url": args.upstream_url,
            "impact": {
                "config": "enable features.remote_compaction_v2 and update only its managed explanatory comment",
                "runtime": "install the Python authorization and dual-protocol compaction modules",
                "service": "reload only the 8318 per-user transparent proxy definition",
                "expected_interruption": "brief 8318 handoff while the approved service definition is reloaded",
                "preserved": [
                    "default model",
                    "model_provider",
                    "ChatGPT auth.json",
                    "task rows and task inventory",
                    "CLIProxyAPI 8317 service",
                ],
            },
            "transaction": planned_transaction_id,
            "backup_root": str(backup_root),
            "backup_plan": backup_plan,
            "apply_command": [
                sys.executable,
                str(BRIDGE_SCRIPT),
                "compaction",
                "configure",
                "--config",
                str(config_path),
                "--state-db",
                str(Path(args.state_db).expanduser()),
                "--catalog",
                str(Path(args.catalog).expanduser()),
                "--auth-file",
                str(auth_path),
                "--helper",
                str(Path(args.helper).expanduser()),
                "--state-dir",
                str(state_dir),
                "--proxy-url",
                args.proxy_url,
                "--upstream-url",
                args.upstream_url,
                "--runtime-script",
                str(runtime),
                "--launch-agent",
                str(Path(args.launch_agent).expanduser()),
                "--systemd-unit",
                str(Path(args.systemd_unit).expanduser()),
                "--windows-task-name",
                args.windows_task_name,
                "--expected-sha256",
                current_sha,
                "--transaction-id",
                planned_transaction_id,
                "--apply",
            ],
            "rollback_command": [
                sys.executable,
                str(BRIDGE_SCRIPT),
                "rollback",
                "--transaction",
                planned_transaction_id,
                "--state-dir",
                str(state_dir),
            ],
            "rollback_apply_command": [
                sys.executable,
                str(BRIDGE_SCRIPT),
                "rollback",
                "--transaction",
                planned_transaction_id,
                "--state-dir",
                str(state_dir),
                "--apply",
            ],
            "default_model_preserved": True,
            "provider_preserved": True,
            "secrets_redacted": True,
        }
        if not args.apply:
            emit(result)

        transaction_id, tx = begin_transaction(
            state_dir,
            "compaction-configure",
            tracked,
            planned_transaction_id,
        )
        if platform.system() == "Darwin":
            tx["services"] = [
                {
                    "kind": "launchagent",
                    "identifier": "com.zhijian.codex-cli-model-bridge-transparent-proxy",
                    "definition_path": str(service_path),
                    "restart_after_restore": True,
                }
            ]
        elif platform.system() == "Linux":
            tx["services"] = [
                {
                    "kind": "systemd-user",
                    "identifier": service_path.name,
                    "definition_path": str(service_path),
                    "restart_after_restore": True,
                }
            ]
        elif platform.system() == "Windows":
            tx["services"] = [
                {
                    "kind": "task-scheduler",
                    "identifier": args.windows_task_name,
                    "definition_path": str(service_path),
                    "windows_task_name": args.windows_task_name,
                    "restart_after_restore": True,
                }
            ]
        auth_before = sha256_path(auth_path)
        try:
            applied = run_bridge(
                [*_compaction_configure_bridge_args(args, current_sha), "--apply"],
                timeout=120,
            )
            verified = tomllib.loads(config_path.read_text(encoding="utf-8"))
            health = _get_health(args.proxy_url, timeout=10)
            if verified.get("features", {}).get("remote_compaction_v2") is not True:
                raise DeploymentError("remote_compaction_v2 was not enabled")
            if verified.get("model") != parsed_before.get("model"):
                raise DeploymentError("default model changed during compaction configuration")
            if verified.get("model_provider", "openai") != parsed_before.get("model_provider", "openai"):
                raise DeploymentError("model Provider changed during compaction configuration")
            if auth_before != sha256_path(auth_path):
                raise DeploymentError("ChatGPT auth file changed during compaction configuration")
            if (health or {}).get("compaction", {}).get("protocol_version") != "2":
                raise DeploymentError("compaction proxy protocol v2 health check failed")
            finish_transaction(state_dir, tx)
            result.update(
                {
                    "status": "applied",
                    "transaction": transaction_id,
                    "bridge": applied,
                    "proxy_health": health,
                }
            )
            emit(result)
        except Exception as exc:
            rollback_completed = False
            service_restore: str | None = None
            rollback_error: str | None = None
            try:
                restore_transaction(tx, require_after_match=False)
                rollback_completed = True
                service_restore = restore_transparent_service(service_path, args.windows_task_name)
                if service_restore is None and _get_health(args.proxy_url, timeout=10) is None:
                    service_restore = "restored transparent proxy failed its health check"
            except Exception as rollback_exc:
                rollback_error = f"{type(rollback_exc).__name__}: {rollback_exc}"
            fail_transaction(
                state_dir,
                tx,
                str(exc),
                rollback_completed,
                service_restore or rollback_error,
            )
            if not rollback_completed or service_restore or rollback_error:
                detail = service_restore or rollback_error or "unknown rollback failure"
                raise DeploymentError(f"{exc}; automatic rollback incomplete: {detail}") from exc
            raise
    except (DeploymentError, OSError, ValueError, tomllib.TOMLDecodeError, subprocess.TimeoutExpired) as exc:
        emit({"status": "blocked", "error": str(exc), "secrets_redacted": True}, 2)


def cmd_compaction_verify(args: argparse.Namespace) -> None:
    catalog = Path(args.catalog).expanduser()
    try:
        models = _selected_compaction_models(args.models, catalog)
        live = _live_model_ids(args.proxy_url, args.timeout)
        optional = {item.strip() for item in args.optional_model.split(",") if item.strip()}
        results: dict[str, dict] = {}

        def verify(model: str) -> tuple[str, dict]:
            if model not in live:
                return model, {"status": "unavailable", "secrets_redacted": True}
            return model, verify_model_compaction(args.proxy_url, model, args.timeout)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            for model, result in pool.map(verify, models):
                results[model] = result
        blocking = [
            model
            for model, result in results.items()
            if result.get("status") == "blocked"
            or (result.get("status") == "unavailable" and model not in optional)
        ]
        health = _get_health(args.proxy_url)
        ready = not blocking and (health or {}).get("compaction", {}).get("protocol_version") == "2"
        emit(
            {
                "status": "ready" if ready else "blocked",
                "proxy_url": args.proxy_url,
                "models": results,
                "blocking_models": blocking,
                "optional_unavailable_models": sorted(optional),
                "proxy_health": health,
                "secrets_redacted": True,
            },
            0 if ready else 2,
        )
    except (DeploymentError, OSError, ValueError, urllib.error.URLError) as exc:
        emit({"status": "blocked", "error": str(exc), "secrets_redacted": True}, 2)


def cmd_local_compaction(args: argparse.Namespace) -> None:
    """Compatibility alias. False now selects a retired remote endpoint."""

    forwarded = argparse.Namespace(
        config=args.config,
        state_db=str(DEFAULT_CODEX_HOME / "state_5.sqlite"),
        catalog=str(DEFAULT_CODEX_HOME / "model-catalog-cli-proxy.json"),
        auth_file=str(DEFAULT_CODEX_HOME / "auth.json"),
        helper=str(Path("~/.config/codex-cli-proxy/read-client-key.py").expanduser()),
        state_dir=args.state_dir,
        proxy_url=DEFAULT_TRANSPARENT_URL,
        upstream_url=DEFAULT_PROXY_URL,
        runtime_script=str(Path(args.state_dir).expanduser() / "transparent_proxy.py"),
        launch_agent=str(
            Path("~/Library/LaunchAgents/com.zhijian.codex-cli-model-bridge-transparent-proxy.plist").expanduser()
        ),
        systemd_unit=str(
            Path("~/.config/systemd/user/codex-cli-model-bridge-transparent-proxy.service").expanduser()
        ),
        windows_task_name="CodexCliModelBridgeTransparentProxy",
        expected_sha256=args.expected_sha256,
        transaction_id=args.transaction_id,
        apply=args.apply,
        deprecated_alias=True,
    )
    cmd_compaction_configure(forwarded)


def cmd_verify(args: argparse.Namespace) -> None:
    models = [item.strip() for item in args.models.split(",") if item.strip()]
    if not models:
        emit({"status": "blocked", "error": "--models is empty"}, 2)
    try:
        audit = run_bridge(["audit", "--proxy-config", str(Path(args.proxy_config).expanduser())])
        probe_args = ["probe", "--models", ",".join(models)]
        if args.desktop:
            probe_args.append("--desktop")
        text_probe = run_bridge(probe_args, timeout=args.timeout * max(1, len(models)))
        shell_probe = run_bridge([*probe_args, "--shell"], timeout=args.timeout * max(1, len(models))) if args.shell else None
        sequence_probe = run_bridge([*probe_args, "--tool-sequence"], timeout=args.timeout * max(1, len(models))) if args.tool_sequence else None
        multi_agent = run_bridge(["probe-multi-agent", "--models", ",".join(models), "--timeout", str(args.timeout)]) if args.multi_agent else None
        manifests = {}
        for path in list((SKILL_DIR / "models").glob("*.json")) + list((Path(args.state_dir).expanduser() / "models.d").glob("*.json")):
            manifest = read_json(path)
            manifests[manifest.get("slug")] = manifest
        image_models = [model for model in models if "image" in manifests.get(model, {}).get("input_modalities", [])]
        image_probe = None
        if image_models:
            image_args = ["probe", "--models", ",".join(image_models), "--image"]
            if args.desktop:
                image_args.append("--desktop")
            image_probe = run_bridge(image_args, timeout=args.timeout * len(image_models))
        compaction = {
            model: verify_model_compaction(args.compaction_url, model, args.timeout)
            for model in models
        }
        sync_one = run_bridge(["sync", "--state-dir", str(Path(args.state_dir).expanduser())])
        sync_two = run_bridge(["sync", "--state-dir", str(Path(args.state_dir).expanduser())])
        codex_config = Path(args.config).expanduser()
        config_data = tomllib.loads(codex_config.read_text(encoding="utf-8"))
        remote_compaction_v2 = config_data.get("features", {}).get("remote_compaction_v2") is True
        incompatible = sorted(model for model, item in compaction.items() if item.get("status") != "ready")
        compaction_ready = remote_compaction_v2 and not incompatible
        changes = sync_two.get("changes", {})
        sync_idempotent = not any(changes.get(key) for key in ("added", "updated", "removed"))
        ready = compaction_ready and sync_idempotent
        emit(
            {
                "status": "ready" if ready else "blocked",
                "audit": audit,
                "text_probe": text_probe,
                "shell_probe": shell_probe,
                "tool_sequence_probe": sequence_probe,
                "multi_agent_probe": multi_agent,
                "image_probe": image_probe,
                "compaction": compaction,
                "compaction_policy": {
                    "remote_compaction_v2": remote_compaction_v2,
                    "incompatible_models": incompatible,
                    "next": "run compaction configure, then compaction verify" if not compaction_ready else None,
                },
                "sync_idempotent": sync_idempotent and sync_one.get("changes") == sync_two.get("changes"),
                "secrets_redacted": True,
            },
            0 if ready else 2,
        )
    except (DeploymentError, subprocess.TimeoutExpired, OSError) as exc:
        emit({"status": "blocked", "error": str(exc), "secrets_redacted": True}, 2)


def cmd_rollback(args: argparse.Namespace) -> None:
    state_dir = Path(args.state_dir).expanduser()
    transaction_path = transaction_dir(state_dir, args.transaction) / "transaction.json"
    try:
        payload = read_json(transaction_path)
        if payload.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
            raise DeploymentError("unsupported transaction schema")
        changes = [
            {
                "path": item["path"],
                "action": "restore" if item.get("backup") else "remove-created-file",
                "current_matches_transaction": sha256_path(Path(item["path"])) == item.get("after_sha256"),
            }
            for item in payload.get("files", [])
        ]
        if not args.apply:
            emit({"status": "planned", "transaction": args.transaction, "changes": changes})
        conflicts = [item["path"] for item in changes if not item["current_matches_transaction"]]
        if conflicts and not args.force:
            raise DeploymentError("rollback refused because files changed: " + ", ".join(conflicts))
        stop_recorded_services(payload)
        restore_transaction(payload, require_after_match=False)
        service_errors = restore_recorded_services(payload)
        if service_errors:
            raise DeploymentError("rollback restored files but not services: " + "; ".join(service_errors))
        payload["status"] = "rolled_back"
        payload["rolled_back_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        atomic_write_text(transaction_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        emit({"status": "rolled_back", "transaction": args.transaction, "changes": changes})
    except (DeploymentError, OSError, json.JSONDecodeError) as exc:
        emit({"status": "blocked", "error": str(exc), "transaction": args.transaction}, 2)


CATALOG_TIMER_LABEL = "com.zhijian.codex-cli-model-bridge.catalog-refresh"


def catalog_timer_plist(bridge_script: Path, state_dir: Path, interval_seconds: int, python_executable: str) -> bytes:
    payload = {
        "Label": CATALOG_TIMER_LABEL,
        "ProgramArguments": [python_executable, str(bridge_script), "catalog-refresh", "--apply"],
        "StartInterval": interval_seconds,
        "RunAtLoad": True,
        "StandardOutPath": str(state_dir / "catalog-refresh.log"),
        "StandardErrorPath": str(state_dir / "catalog-refresh.log"),
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload)


def cmd_catalog_timer(args: argparse.Namespace) -> None:
    if platform.system() != "Darwin":
        emit(
            {
                "status": "blocked",
                "error": "the catalog timer currently supports macOS launchd; schedule bridge.py catalog-refresh --apply with schtasks or a systemd timer elsewhere",
            },
            2,
        )
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{CATALOG_TIMER_LABEL}"
    plist_path = Path("~/Library/LaunchAgents").expanduser() / f"{CATALOG_TIMER_LABEL}.plist"
    if args.remove:
        if not args.apply:
            emit({"status": "planned", "action": "remove", "plist": str(plist_path), "target": target})
            return
        loaded = subprocess.run(
            ["launchctl", "print", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        if loaded:
            subprocess.run(
                ["launchctl", "bootout", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if plist_path.exists():
            plist_path.unlink()
        emit({"status": "applied", "action": "remove", "plist": str(plist_path), "secrets_redacted": True})
        return
    interval = max(600, int(args.interval_seconds))
    state_dir = Path(args.state_dir).expanduser()
    payload = catalog_timer_plist(Path(args.bridge_script).expanduser(), state_dir, interval, sys.executable)
    preview = plistlib.loads(payload)
    if not args.apply:
        emit(
            {
                "status": "planned",
                "action": "install",
                "plist": str(plist_path),
                "target": target,
                "interval_seconds": interval,
                "program_arguments": preview.get("ProgramArguments"),
                "secrets_redacted": True,
            }
        )
        return
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    if plist_path.exists():
        backup_path = plist_path.with_name(plist_path.name + ".bak-timer")
        backup_path.write_bytes(plist_path.read_bytes())
    atomic_write_bytes(plist_path, payload, 0o600)
    loaded = subprocess.run(
        ["launchctl", "print", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    if loaded:
        subprocess.run(
            ["launchctl", "bootout", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        for _ in range(20):
            if (
                subprocess.run(
                    ["launchctl", "print", target],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                ).returncode
                != 0
            ):
                break
            time.sleep(0.5)
    last_error = "launchctl failed to load the catalog timer"
    for _ in range(3):
        started = subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if started.returncode == 0:
            emit(
                {
                    "status": "applied",
                    "action": "install",
                    "plist": str(plist_path),
                    "target": target,
                    "interval_seconds": interval,
                    "secrets_redacted": True,
                }
            )
            return
        time.sleep(1.0)
    emit({"status": "blocked", "error": last_error, "plist": str(plist_path), "secrets_redacted": True}, 2)


def register_deployment_parsers(sub: argparse._SubParsersAction) -> None:
    bootstrap = sub.add_parser("bootstrap", help="Adopt or install a verified CLIProxyAPI runtime")
    bootstrap.add_argument("--proxy-binary")
    bootstrap.add_argument("--proxy-config", default=str(DEFAULT_PROXY_CONFIG))
    bootstrap.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    bootstrap.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR))
    bootstrap.add_argument("--version", default="latest")
    bootstrap.add_argument("--release-api", default=RELEASE_API)
    bootstrap.add_argument("--windows-task-name", default="CodexCliModelBridgeCLIProxyAPI")
    bootstrap.add_argument("--no-service", action="store_true")
    bootstrap.add_argument("--apply", action="store_true")
    bootstrap.set_defaults(func=cmd_bootstrap)

    catalog_timer = sub.add_parser(
        "catalog-timer",
        help="Install or remove the macOS LaunchAgent that auto-refreshes the model catalog",
    )
    catalog_timer.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    catalog_timer.add_argument("--bridge-script", default=str(SKILL_DIR / "scripts" / "bridge.py"))
    catalog_timer.add_argument("--interval-seconds", type=int, default=21600)
    catalog_timer.add_argument("--remove", action="store_true")
    catalog_timer.add_argument("--apply", action="store_true")
    catalog_timer.set_defaults(func=cmd_catalog_timer)

    provider = sub.add_parser("provider", help="Manage upstream provider definitions")
    provider_sub = provider.add_subparsers(dest="provider_command", required=True)
    provider_add = provider_sub.add_parser("add")
    provider_add.add_argument("--preset")
    provider_add.add_argument("--spec")
    provider_add.add_argument("--models")
    provider_add.add_argument("--base-url")
    provider_add.add_argument("--proxy-config", default=str(DEFAULT_PROXY_CONFIG))
    provider_add.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    provider_add.add_argument("--expected-sha256")
    provider_add.add_argument("--apply", action="store_true")
    provider_add.set_defaults(func=cmd_provider_add)

    credential = sub.add_parser("credential", help="Stage API keys or start provider OAuth")
    credential_sub = credential.add_subparsers(dest="credential_command", required=True)
    credential_set = credential_sub.add_parser("set")
    credential_set.add_argument("--provider", required=True)
    credential_set.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    credential_set.add_argument("--stdin", action="store_true", help="Read one secret line from stdin; prefer the hidden prompt")
    credential_set.set_defaults(func=cmd_credential_set)
    credential_login = credential_sub.add_parser("login")
    credential_login.add_argument("--preset")
    credential_login.add_argument("--spec")
    credential_login.add_argument("--proxy-binary")
    credential_login.add_argument("--proxy-config", default=str(DEFAULT_PROXY_CONFIG))
    credential_login.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    credential_login.add_argument("--ack-provider-terms", action="store_true")
    credential_login.add_argument("--apply", action="store_true")
    credential_login.set_defaults(func=cmd_credential_login)

    activate = sub.add_parser("activate", help="Create the fallback profile and optionally activate Desktop")
    activate.add_argument("--mode", choices=["desktop", "profile"], default="desktop")
    activate.add_argument("--models")
    activate.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    activate.add_argument("--codex-home", default=str(DEFAULT_CODEX_HOME))
    activate.add_argument("--proxy-config", default=str(DEFAULT_PROXY_CONFIG))
    activate.add_argument("--apply", action="store_true")
    activate.set_defaults(func=cmd_activate)

    compaction = sub.add_parser("compaction", help="Audit, configure, or verify Codex compaction")
    compaction_sub = compaction.add_subparsers(dest="compaction_command", required=True)

    compaction_audit = compaction_sub.add_parser("audit")
    compaction_audit.add_argument("--models", default="all")
    compaction_audit.add_argument("--catalog", default=str(DEFAULT_CODEX_HOME / "model-catalog-cli-proxy.json"))
    compaction_audit.add_argument("--config", default=str(DEFAULT_CODEX_HOME / "config.toml"))
    compaction_audit.add_argument("--proxy-url", default=DEFAULT_TRANSPARENT_URL)
    compaction_audit.add_argument("--optional-model", default="gpt-reserve")
    compaction_audit.add_argument("--workers", type=int, default=3)
    compaction_audit.add_argument("--timeout", type=int, default=180)
    compaction_audit.set_defaults(func=cmd_compaction_audit)

    compaction_configure = compaction_sub.add_parser("configure")
    compaction_configure.add_argument("--config", default=str(DEFAULT_CODEX_HOME / "config.toml"))
    compaction_configure.add_argument("--state-db", default=str(DEFAULT_CODEX_HOME / "state_5.sqlite"))
    compaction_configure.add_argument("--catalog", default=str(DEFAULT_CODEX_HOME / "model-catalog-cli-proxy.json"))
    compaction_configure.add_argument("--auth-file", default=str(DEFAULT_CODEX_HOME / "auth.json"))
    compaction_configure.add_argument(
        "--helper", default=str(Path("~/.config/codex-cli-proxy/read-client-key.py").expanduser())
    )
    compaction_configure.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    compaction_configure.add_argument("--proxy-url", default=DEFAULT_TRANSPARENT_URL)
    compaction_configure.add_argument("--upstream-url", default=DEFAULT_PROXY_URL)
    compaction_configure.add_argument(
        "--runtime-script", default=str(DEFAULT_STATE_DIR / "transparent_proxy.py")
    )
    compaction_configure.add_argument(
        "--launch-agent",
        default=str(
            Path("~/Library/LaunchAgents/com.zhijian.codex-cli-model-bridge-transparent-proxy.plist").expanduser()
        ),
    )
    compaction_configure.add_argument(
        "--systemd-unit",
        default=str(
            Path("~/.config/systemd/user/codex-cli-model-bridge-transparent-proxy.service").expanduser()
        ),
    )
    compaction_configure.add_argument("--windows-task-name", default="CodexCliModelBridgeTransparentProxy")
    compaction_configure.add_argument("--expected-sha256")
    compaction_configure.add_argument("--transaction-id")
    compaction_configure.add_argument("--apply", action="store_true")
    compaction_configure.set_defaults(func=cmd_compaction_configure, deprecated_alias=False)

    compaction_verify = compaction_sub.add_parser("verify")
    compaction_verify.add_argument("--models", default="all")
    compaction_verify.add_argument("--catalog", default=str(DEFAULT_CODEX_HOME / "model-catalog-cli-proxy.json"))
    compaction_verify.add_argument("--proxy-url", default=DEFAULT_TRANSPARENT_URL)
    compaction_verify.add_argument("--optional-model", default="gpt-reserve")
    compaction_verify.add_argument("--workers", type=int, default=3)
    compaction_verify.add_argument("--timeout", type=int, default=180)
    compaction_verify.set_defaults(func=cmd_compaction_verify)

    verify = sub.add_parser("verify", help="Run the end-to-end completion gate")
    verify.add_argument("--models", required=True)
    verify.add_argument("--desktop", action="store_true")
    verify.add_argument("--shell", action="store_true")
    verify.add_argument("--tool-sequence", action="store_true")
    verify.add_argument("--multi-agent", action="store_true")
    verify.add_argument("--timeout", type=int, default=180)
    verify.add_argument("--proxy-url", default=DEFAULT_PROXY_URL)
    verify.add_argument("--compaction-url", default=DEFAULT_TRANSPARENT_URL)
    verify.add_argument("--proxy-config", default=str(DEFAULT_PROXY_CONFIG))
    verify.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    verify.add_argument("--config", default=str(DEFAULT_CODEX_HOME / "config.toml"))
    verify.set_defaults(func=cmd_verify)

    local_compaction = sub.add_parser(
        "local-compaction",
        help="Deprecated alias for compaction configure; never writes remote_compaction_v2=false",
    )
    local_compaction.add_argument("--config", default=str(DEFAULT_CODEX_HOME / "config.toml"))
    local_compaction.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    local_compaction.add_argument("--expected-sha256")
    local_compaction.add_argument("--transaction-id")
    local_compaction.add_argument("--apply", action="store_true")
    local_compaction.set_defaults(func=cmd_local_compaction)

    rollback = sub.add_parser("rollback", help="Restore an exact recorded transaction")
    rollback.add_argument("--transaction", required=True)
    rollback.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    rollback.add_argument("--force", action="store_true")
    rollback.add_argument("--apply", action="store_true")
    rollback.set_defaults(func=cmd_rollback)
