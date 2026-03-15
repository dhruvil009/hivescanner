"""Tests for email/Gmail scanner."""

import json
import os
import sys
import importlib.util
from unittest.mock import patch, MagicMock

import pytest

# We must load workers/sources/email.py explicitly by file path because
# ``import email`` resolves to the stdlib email package.
_EMAIL_PATH = os.path.join(
    os.path.dirname(__file__), "..", "workers", "sources", "email.py"
)

# Also add workers/ to sys.path so snapshot_store can be found
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers", "sources"))

# Load the module once under a unique name so patches can target it
_spec = importlib.util.spec_from_file_location("email_scanner", _EMAIL_PATH)
_email_mod = importlib.util.module_from_spec(_spec)
sys.modules["email_scanner"] = _email_mod
_spec.loader.exec_module(_email_mod)
EmailScanner = _email_mod.EmailScanner


SAMPLE_EMAIL = {
    "id": "msg001",
    "from": "alice@example.com",
    "subject": "Project update",
    "date": "2026-03-15T10:00:00Z",
    "snippet": "Here is the latest update on the project...",
}

SAMPLE_VIP_EMAIL = {
    "id": "msg002",
    "from": "ceo@company.com",
    "subject": "Urgent: Board meeting tomorrow",
    "date": "2026-03-15T11:00:00Z",
    "snippet": "Please prepare the deck for tomorrow's board meeting.",
}

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}


@pytest.fixture
def scanner():
    with patch("email_scanner.load_snapshot", return_value={}), \
         patch("email_scanner.save_snapshot"):
        s = EmailScanner()
        return s


@pytest.fixture
def bootstrapped_scanner():
    """Scanner that has already been bootstrapped (has prior snapshot data)."""
    with patch("email_scanner.load_snapshot", return_value={"existing": "data"}), \
         patch("email_scanner.save_snapshot"):
        s = EmailScanner()
        return s


class TestEmailScanner:
    def test_configure_returns_expected_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["vip_senders"] == []
        assert config["max_emails"] == 20

    def test_poll_returns_empty_when_gws_not_installed(self, scanner):
        with patch("shutil.which", return_value=None):
            scanner._cli_available = None
            pollen, wm = scanner.poll(scanner.configure(), "")
        assert pollen == []
        assert wm == ""

    def test_poll_returns_empty_when_no_emails(self, bootstrapped_scanner):
        scanner = bootstrapped_scanner
        scanner._cli_available = True

        with patch.object(scanner, "_gws", return_value="[]"), \
             patch("email_scanner.save_snapshot"):
            pollen, wm = scanner.poll(scanner.configure(), "")
        assert pollen == []

    def test_new_email_emits_email_new_pollen(self, bootstrapped_scanner):
        scanner = bootstrapped_scanner
        scanner._cli_available = True
        raw_json = json.dumps([SAMPLE_EMAIL])

        with patch.object(scanner, "_gws", return_value=raw_json), \
             patch("email_scanner.save_snapshot"):
            pollen, wm = scanner.poll(
                scanner.configure(),
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "email_new"
        assert pollen[0]["id"] == "email-msg001"
        assert pollen[0]["source"] == "email"
        assert pollen[0]["group"] == "Email"
        assert "alice@example.com" in pollen[0]["title"]
        assert "Project update" in pollen[0]["title"]

    def test_vip_sender_emits_email_urgent_pollen(self, bootstrapped_scanner):
        scanner = bootstrapped_scanner
        scanner._cli_available = True
        raw_json = json.dumps([SAMPLE_VIP_EMAIL])

        config = scanner.configure()
        config["vip_senders"] = ["ceo@company.com"]

        with patch.object(scanner, "_gws", return_value=raw_json), \
             patch("email_scanner.save_snapshot"):
            pollen, wm = scanner.poll(
                config,
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "email_urgent"
        assert pollen[0]["group"] == "Urgent Email"
        assert pollen[0]["id"] == "email-msg002"

    def test_bootstrap_silence_first_poll_emits_no_pollen(self, scanner):
        """First poll should snapshot emails without emitting any pollen."""
        scanner._cli_available = True
        raw_json = json.dumps([SAMPLE_EMAIL, SAMPLE_VIP_EMAIL])

        with patch.object(scanner, "_gws", return_value=raw_json), \
             patch("email_scanner.save_snapshot"):
            pollen, wm = scanner.poll(scanner.configure(), "")

        assert pollen == []
        # After first poll, bootstrapped should be True
        assert scanner._bootstrapped is True

    def test_pollen_schema_has_all_required_keys(self, bootstrapped_scanner):
        scanner = bootstrapped_scanner
        scanner._cli_available = True
        raw_json = json.dumps([SAMPLE_EMAIL])

        with patch.object(scanner, "_gws", return_value=raw_json), \
             patch("email_scanner.save_snapshot"):
            pollen, wm = scanner.poll(
                scanner.configure(),
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        missing = REQUIRED_POLLEN_KEYS - set(pollen[0].keys())
        assert not missing, f"Pollen missing keys: {missing}"
