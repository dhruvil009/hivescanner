"""Installation and configuration contracts shared by every scanner."""

import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMMUNITY_MANIFESTS = sorted((ROOT / "community").glob("*/teammate.json"))
BUILTIN_PATHS = sorted((ROOT / "workers" / "sources").glob("[!_]*.py"))
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _module(path: Path, prefix: str):
    name = f"{prefix}_{path.parent.name}_{path.stem}".replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _scanner_classes(module, expected_name=None):
    return [
        value
        for attr in dir(module)
        if (
            isinstance((value := getattr(module, attr)), type)
            and attr.endswith("Scanner")
            and hasattr(value, "name")
            and (expected_name is None or value.name == expected_name)
        )
    ]


def _documented_config(path: Path, name: str) -> dict:
    match = re.search(r"```json\n(.*?)\n```", path.read_text(), re.DOTALL)
    assert match is not None, f"{path} has no JSON configuration example"
    example = json.loads(match.group(1))
    assert set(example) == {name}
    assert isinstance(example[name], dict)
    return example[name]


@pytest.mark.parametrize("manifest_path", COMMUNITY_MANIFESTS, ids=lambda path: path.parent.name)
def test_community_manifest_matches_adapter_and_sandbox_dispatch(manifest_path):
    manifest = json.loads(manifest_path.read_text())
    name = manifest_path.parent.name
    assert manifest["name"] == name
    assert SEMVER_RE.fullmatch(manifest["version"])
    assert isinstance(manifest["display_name"], str) and manifest["display_name"].strip()
    assert isinstance(manifest["description"], str) and manifest["description"].strip()
    assert isinstance(manifest["qpm_budget"], int) and 1 <= manifest["qpm_budget"] <= 60
    assert manifest.get("supports_check_acted", False) is False

    adapter_name = manifest.get("adapter_file", "adapter.py")
    assert isinstance(adapter_name, str) and Path(adapter_name).name == adapter_name
    adapter_path = manifest_path.parent / adapter_name
    assert adapter_path.is_file()
    requirements = manifest.get("requirements")
    assert isinstance(requirements, dict)
    assert isinstance(requirements.get("cli_tools"), list)
    assert requirements["cli_tools"] == []
    assert all(
        isinstance(tool, str) and re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", tool)
        for tool in requirements["cli_tools"]
    )

    module = _module(adapter_path, "community_contract")
    classes = _scanner_classes(module, name)
    assert len(classes) == 1
    configured = classes[0]().configure()
    assert configured == manifest["config_template"]
    assert configured.get("enabled") is False
    assert all(
        ENV_RE.fullmatch(value)
        for key, value in configured.items()
        if key.endswith("_env")
    )

    result = subprocess.run(
        [sys.executable, "-I", str(adapter_path), "--sandboxed"],
        input=json.dumps({"command": "configure"}),
        text=True,
        capture_output=True,
        env={key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"config": configured}

    for invalid_payload in (
        '{"command":"configure","command":"poll"}',
        json.dumps({"command": "unsupported"}),
    ):
        rejected = subprocess.run(
            [sys.executable, "-I", str(adapter_path), "--sandboxed"],
            input=invalid_payload,
            text=True,
            capture_output=True,
            env={key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ},
            timeout=10,
        )
        assert rejected.returncode != 0
        assert rejected.stdout == ""


def test_community_names_are_unique_and_all_have_documentation():
    names = [json.loads(path.read_text())["name"] for path in COMMUNITY_MANIFESTS]
    assert len(names) == len(set(names))
    for name in names:
        assert (ROOT / "website" / "community-scanners" / f"{name}.md").is_file()


@pytest.mark.parametrize("manifest_path", COMMUNITY_MANIFESTS, ids=lambda path: path.parent.name)
def test_community_documentation_exposes_exact_config_field_set(manifest_path):
    manifest = json.loads(manifest_path.read_text())
    name = manifest["name"]
    documented = _documented_config(
        ROOT / "website" / "community-scanners" / f"{name}.md",
        name,
    )
    assert set(documented) == set(manifest["config_template"])


@pytest.mark.parametrize(
    "manifest_path",
    [path for path in COMMUNITY_MANIFESTS if path.parent.name != "rss"],
    ids=lambda path: path.parent.name,
)
def test_network_adapters_refuse_cross_origin_redirects(manifest_path):
    module = _module(manifest_path.parent / "adapter.py", "redirect_contract")
    handler = module._SameOriginRedirectHandler()
    request = urllib.request.Request(
        "https://api.example.test/start",
        headers={"Authorization": "Bearer secret"},
    )
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://attacker.example/collect",
        )
    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://api.example.test/next",
    )
    assert redirected.full_url == "https://api.example.test/next"


def test_builtin_config_template_matches_every_adapter_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = json.loads((ROOT / "workers" / "config_template.json").read_text())[
        "scanners"
    ]
    discovered = {}
    for path in BUILTIN_PATHS:
        module = _module(path, "builtin_contract")
        if hasattr(module, "load_snapshot"):
            monkeypatch.setattr(module, "load_snapshot", lambda *args, **kwargs: {})
        if hasattr(module, "snapshot_exists"):
            monkeypatch.setattr(module, "snapshot_exists", lambda *args, **kwargs: False)
        for scanner_class in _scanner_classes(module):
            scanner = scanner_class()
            assert scanner.name not in discovered
            discovered[scanner.name] = scanner.configure()
    assert discovered == expected
    assert all(isinstance(config.get("enabled"), bool) for config in discovered.values())


def test_builtin_documentation_exposes_exact_config_field_sets():
    expected = json.loads((ROOT / "workers" / "config_template.json").read_text())[
        "scanners"
    ]
    for name, config in expected.items():
        documented = _documented_config(
            ROOT / "website" / "built-in-scanners" / f"{name.replace('_', '-')}.md",
            name,
        )
        assert set(documented) == set(config)
