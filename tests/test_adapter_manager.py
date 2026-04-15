"""Tests for scanner_manager.py"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
import scanner_manager


@pytest.fixture(autouse=True)
def tmp_hivescanner(tmp_path, monkeypatch):
    home = tmp_path / "hivescanner_home"
    home.mkdir()
    monkeypatch.setattr(scanner_manager, "HIVESCANNER_HOME", home)
    monkeypatch.setattr(scanner_manager, "CONFIG_FILE", home / "config.json")
    monkeypatch.setattr(scanner_manager, "POLLEN_FILE", home / "pollen.json")
    monkeypatch.setattr(scanner_manager, "SCANNERS_DIR", home / "scanners")
    monkeypatch.setattr(scanner_manager, "TEAMMATES_DIR", home / "teammates")
    return home


@pytest.fixture
def community_dir(tmp_path, monkeypatch):
    """Create a mock plugin root with a community scanner."""
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    community = plugin_root / "community" / "testrss"
    community.mkdir(parents=True)

    (community / "adapter.py").write_text('class TestRssScanner:\n    name = "testrss"\n')
    (community / "teammate.json").write_text(json.dumps({
        "name": "testrss",
        "display_name": "Test RSS",
        "version": "1.0.0",
        "description": "Test scanner",
        "author": "test",
        "adapter_file": "adapter.py",
        "config_template": {"enabled": False, "feeds": []},
        "requirements": {"cli_tools": []},
    }))

    monkeypatch.setattr(scanner_manager, "_find_plugin_root", lambda: plugin_root)
    return community


class TestHire:
    def test_hire_builtin_rejected(self):
        result = scanner_manager.hire("github")
        assert "error" in result

    def test_hire_missing_scanner(self, community_dir, monkeypatch):
        result = scanner_manager.hire("nonexistent")
        assert "error" in result

    def test_hire_success(self, tmp_hivescanner, community_dir):
        # Write initial config
        config = {"version": 1, "scanners": {}}
        (tmp_hivescanner / "config.json").write_text(json.dumps(config))

        result = scanner_manager.hire("testrss")
        assert result["status"] == "hired"
        assert (tmp_hivescanner / "scanners" / "testrss.py").exists()

        # Check config was updated
        updated_config = json.loads((tmp_hivescanner / "config.json").read_text())
        assert "testrss" in updated_config["scanners"]

    def test_hire_rejects_path_traversal_adapter_file(self, tmp_hivescanner, community_dir):
        # Rewrite the manifest with a malicious adapter_file pointing outside community_dir.
        manifest = json.loads((community_dir / "teammate.json").read_text())
        manifest["adapter_file"] = "../../../etc/passwd"
        (community_dir / "teammate.json").write_text(json.dumps(manifest))

        config = {"version": 1, "scanners": {}}
        (tmp_hivescanner / "config.json").write_text(json.dumps(config))

        result = scanner_manager.hire("testrss")
        assert "error" in result
        assert "must stay within" in result["error"]
        # Scanner file should NOT have been copied.
        assert not (tmp_hivescanner / "scanners" / "testrss.py").exists()


class TestFire:
    def test_fire_builtin_rejected(self):
        result = scanner_manager.fire("github")
        assert "error" in result

    def test_fire_not_hired(self):
        result = scanner_manager.fire("nonexistent")
        assert "error" in result

    def test_fire_success(self, tmp_hivescanner, community_dir):
        config = {"version": 1, "scanners": {}}
        (tmp_hivescanner / "config.json").write_text(json.dumps(config))

        scanner_manager.hire("testrss")
        result = scanner_manager.fire("testrss")
        assert result["status"] == "fired"
        assert not (tmp_hivescanner / "scanners" / "testrss.py").exists()


class TestListTeammates:
    def test_lists_builtins(self):
        result = scanner_manager.list_teammates()
        builtin_names = {b["name"] for b in result["builtin"]}
        assert "github" in builtin_names
        assert "calendar" in builtin_names
        assert "gchat" in builtin_names
        assert "whatsapp" in builtin_names
        assert "email" in builtin_names
        assert "weather" in builtin_names

    def test_lists_hired(self, tmp_hivescanner, community_dir):
        config = {"version": 1, "scanners": {}}
        (tmp_hivescanner / "config.json").write_text(json.dumps(config))

        scanner_manager.hire("testrss")
        result = scanner_manager.list_teammates()
        hired_names = {h["name"] for h in result["hired"]}
        assert "testrss" in hired_names


class TestDisableEnable:
    def test_disable_enable(self, tmp_hivescanner):
        config = {"version": 1, "scanners": {"github": {"enabled": True}}}
        (tmp_hivescanner / "config.json").write_text(json.dumps(config))

        result = scanner_manager.disable("github")
        assert result["status"] == "disabled"

        updated = json.loads((tmp_hivescanner / "config.json").read_text())
        assert updated["scanners"]["github"]["enabled"] is False

        result = scanner_manager.enable("github")
        assert result["status"] == "enabled"

        updated = json.loads((tmp_hivescanner / "config.json").read_text())
        assert updated["scanners"]["github"]["enabled"] is True
