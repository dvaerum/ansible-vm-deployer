"""
Unit tests for MetadataManager (libvirt VM metadata stored in XML).
"""
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch, call
from datetime import datetime

import pytest
import libvirt

from ansible_deployer.metadata_manager import MetadataManager
from tests.conftest import make_mock_domain, SAMPLE_METADATA_XML


NAMESPACE = "http://ansible-vm-manager.local/metadata"
PREFIX = "avm"
FLAGS = libvirt.VIR_DOMAIN_AFFECT_CONFIG | libvirt.VIR_DOMAIN_AFFECT_LIVE


def _build_metadata_xml(**fields):
    """Build a metadata XML string from key-value pairs."""
    parts = [f'<avm:metadata xmlns:avm="{NAMESPACE}">']
    for key, value in fields.items():
        parts.append(f"  <avm:{key}>{value}</avm:{key}>")
    parts.append("</avm:metadata>")
    return "\n".join(parts)


def _parse_set_metadata_xml(domain_mock):
    """Extract the XML string passed to the most recent setMetadata() call."""
    args, _ = domain_mock.setMetadata.call_args
    return args[1]


def _parse_set_metadata_dict(domain_mock):
    """Parse the XML written by setMetadata() into a dict of {tag: text}."""
    xml_str = _parse_set_metadata_xml(domain_mock)
    if xml_str is None:
        return None
    root = ET.fromstring(xml_str)
    result = {}
    for child in root:
        tag = child.tag.replace(f"{{{NAMESPACE}}}", "")
        result[tag] = child.text or ""
    return result


class TestGetMetadata:
    """Tests for MetadataManager.get_metadata()."""

    def test_key_exists(self):
        """get_metadata returns value when key is present."""
        domain = make_mock_domain(in_use=True, task_id="task-42")
        mgr = MetadataManager(domain)

        assert mgr.get_metadata("task_id") == "task-42"

    def test_key_missing(self):
        """get_metadata returns None when key is not in the XML."""
        domain = make_mock_domain(in_use=True, task_id="task-42")
        mgr = MetadataManager(domain)

        assert mgr.get_metadata("nonexistent_key") is None

    def test_no_metadata_at_all(self):
        """get_metadata returns None when domain has no metadata (libvirtError)."""
        domain = make_mock_domain()  # No metadata -> raises libvirtError
        mgr = MetadataManager(domain)

        assert mgr.get_metadata("in_use") is None

    def test_reads_in_use_true(self):
        """get_metadata correctly reads in_use='true'."""
        domain = make_mock_domain(in_use=True)
        mgr = MetadataManager(domain)

        assert mgr.get_metadata("in_use") == "true"

    def test_reads_in_use_false(self):
        """get_metadata correctly reads in_use='false'."""
        domain = make_mock_domain(in_use=False, task_id="old-task")
        mgr = MetadataManager(domain)

        assert mgr.get_metadata("in_use") == "false"

    def test_calls_domain_metadata_with_correct_args(self):
        """get_metadata passes the right constants to domain.metadata()."""
        domain = make_mock_domain(in_use=True, task_id="t1")
        mgr = MetadataManager(domain)

        mgr.get_metadata("in_use")

        domain.metadata.assert_called_with(
            libvirt.VIR_DOMAIN_METADATA_ELEMENT,
            NAMESPACE,
            0,
        )


class TestSetMetadata:
    """Tests for MetadataManager.set_metadata() (single key)."""

    def test_delegates_to_set_metadata_bulk(self):
        """set_metadata calls set_metadata_bulk with a single-key dict."""
        domain = make_mock_domain()
        mgr = MetadataManager(domain)

        with patch.object(mgr, "set_metadata_bulk") as mock_bulk:
            mgr.set_metadata("my_key", "my_value")
            mock_bulk.assert_called_once_with({"my_key": "my_value"})


class TestSetMetadataBulk:
    """Tests for MetadataManager.set_metadata_bulk()."""

    def test_creates_new_metadata_when_none_exists(self):
        """set_metadata_bulk creates metadata from scratch when domain has none."""
        domain = make_mock_domain()  # No metadata -> raises libvirtError
        mgr = MetadataManager(domain)

        mgr.set_metadata_bulk({"color": "blue", "size": "large"})

        written = _parse_set_metadata_dict(domain)
        assert written == {"color": "blue", "size": "large"}

    def test_updates_existing_key(self):
        """set_metadata_bulk overwrites the value of an existing key."""
        domain = make_mock_domain(in_use=True, task_id="old-task")
        mgr = MetadataManager(domain)

        mgr.set_metadata_bulk({"task_id": "new-task"})

        written = _parse_set_metadata_dict(domain)
        assert written["task_id"] == "new-task"

    def test_preserves_other_keys_on_update(self):
        """set_metadata_bulk keeps untouched keys intact."""
        domain = make_mock_domain(
            in_use=True, task_id="task-1", started_at="2026-01-01T00:00:00"
        )
        mgr = MetadataManager(domain)

        mgr.set_metadata_bulk({"task_id": "task-2"})

        written = _parse_set_metadata_dict(domain)
        assert written["task_id"] == "task-2"
        assert written["in_use"] == "true"
        assert written["started_at"] == "2026-01-01T00:00:00"

    def test_adds_new_key_to_existing_metadata(self):
        """set_metadata_bulk can add a new key alongside existing ones."""
        domain = make_mock_domain(in_use=True, task_id="task-1")
        mgr = MetadataManager(domain)

        mgr.set_metadata_bulk({"extra_info": "hello"})

        written = _parse_set_metadata_dict(domain)
        assert written["extra_info"] == "hello"
        assert written["in_use"] == "true"
        assert written["task_id"] == "task-1"

    def test_calls_setMetadata_with_correct_args(self):
        """set_metadata_bulk writes with correct prefix, namespace, and flags."""
        domain = make_mock_domain()
        mgr = MetadataManager(domain)

        mgr.set_metadata_bulk({"k": "v"})

        args, _ = domain.setMetadata.call_args
        assert args[0] == libvirt.VIR_DOMAIN_METADATA_ELEMENT
        # args[1] is the XML string
        assert args[2] == PREFIX
        assert args[3] == NAMESPACE
        assert args[4] == FLAGS

    def test_single_setMetadata_call_for_multiple_keys(self):
        """set_metadata_bulk issues exactly one setMetadata() call."""
        domain = make_mock_domain()
        mgr = MetadataManager(domain)

        mgr.set_metadata_bulk({"a": "1", "b": "2", "c": "3"})

        assert domain.setMetadata.call_count == 1


class TestGetAllMetadata:
    """Tests for MetadataManager.get_all_metadata()."""

    def test_returns_all_keys(self):
        """get_all_metadata returns every key-value pair."""
        domain = make_mock_domain(
            in_use=True, task_id="task-99", started_at="2026-06-15T12:00:00"
        )
        mgr = MetadataManager(domain)

        result = mgr.get_all_metadata()

        assert result == {
            "in_use": "true",
            "task_id": "task-99",
            "started_at": "2026-06-15T12:00:00",
        }

    def test_returns_empty_dict_when_no_metadata(self):
        """get_all_metadata returns {} when no metadata exists."""
        domain = make_mock_domain()
        mgr = MetadataManager(domain)

        assert mgr.get_all_metadata() == {}

    def test_empty_text_becomes_empty_string(self):
        """get_all_metadata converts None text to empty string."""
        xml = _build_metadata_xml(in_use="true", task_id="")
        domain = make_mock_domain(in_use=True, task_id="")
        # Override with our controlled XML
        domain.metadata.side_effect = None
        domain.metadata.return_value = xml
        mgr = MetadataManager(domain)

        result = mgr.get_all_metadata()
        assert result["task_id"] == ""


class TestMarkInUse:
    """Tests for MetadataManager.mark_in_use()."""

    @patch("ansible_deployer.metadata_manager.datetime")
    def test_sets_all_fields(self, mock_datetime):
        """mark_in_use writes in_use, task_id, and started_at atomically."""
        fake_now = datetime(2026, 1, 15, 10, 30, 0)
        mock_datetime.now.return_value = fake_now

        domain = make_mock_domain()
        mgr = MetadataManager(domain)

        mgr.mark_in_use("deploy-abc")

        written = _parse_set_metadata_dict(domain)
        assert written["in_use"] == "true"
        assert written["task_id"] == "deploy-abc"
        assert written["started_at"] == fake_now.isoformat()

    def test_single_write_call(self):
        """mark_in_use issues exactly one setMetadata() call."""
        domain = make_mock_domain()
        mgr = MetadataManager(domain)

        mgr.mark_in_use("task-1")

        assert domain.setMetadata.call_count == 1


class TestTryClaim:
    """Tests for MetadataManager.try_claim() including race conditions."""

    @patch("time.sleep")
    def test_successful_claim(self, mock_sleep):
        """try_claim succeeds when re-read returns the same task_id."""
        domain = make_mock_domain()
        mgr = MetadataManager(domain)

        # First call: metadata() raises (no metadata -> is_in_use returns False)
        # Second call: metadata() raises (set_metadata_bulk reads existing)
        # Third call: after mark_in_use writes, re-read returns our task_id
        claim_xml = _build_metadata_xml(
            in_use="true", task_id="my-task", started_at="2026-01-01T00:00:00"
        )

        domain.metadata.side_effect = [
            # is_in_use() -> get_metadata("in_use") -> domain.metadata()
            libvirt.libvirtError("No metadata"),
            # mark_in_use -> set_metadata_bulk -> domain.metadata() (read existing)
            libvirt.libvirtError("No metadata"),
            # get_task_id() -> get_metadata("task_id") -> domain.metadata()
            claim_xml,
        ]

        result = mgr.try_claim("my-task")

        assert result is True
        mock_sleep.assert_called_once_with(0.15)

    @patch("time.sleep")
    def test_failed_claim_race_condition(self, mock_sleep):
        """try_claim fails when re-read returns a different task_id (another process won)."""
        domain = make_mock_domain()
        mgr = MetadataManager(domain)

        # Another process overwrote our claim during the 0.15s sleep
        other_process_xml = _build_metadata_xml(
            in_use="true",
            task_id="other-process-task",
            started_at="2026-01-01T00:00:01",
        )

        domain.metadata.side_effect = [
            # is_in_use() -> no metadata, not in use
            libvirt.libvirtError("No metadata"),
            # mark_in_use -> set_metadata_bulk reads existing
            libvirt.libvirtError("No metadata"),
            # get_task_id() re-read -> other process won
            other_process_xml,
        ]

        result = mgr.try_claim("my-task")

        assert result is False
        mock_sleep.assert_called_once_with(0.15)
        # We still called setMetadata (our attempt), but lost the race
        assert domain.setMetadata.call_count == 1

    @patch("time.sleep")
    def test_vm_already_in_use_skips_claim(self, mock_sleep):
        """try_claim returns False immediately if VM is already in use."""
        domain = make_mock_domain(in_use=True, task_id="existing-task")
        mgr = MetadataManager(domain)

        result = mgr.try_claim("my-task")

        assert result is False
        # Should not have attempted to write at all
        domain.setMetadata.assert_not_called()
        # Should not have slept
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    def test_claim_does_not_try_when_in_use_false_string(self, mock_sleep):
        """try_claim proceeds when in_use is 'false' (VM was previously released)."""
        released_xml = _build_metadata_xml(
            in_use="false", task_id="old-task", started_at="2026-01-01T00:00:00"
        )
        reclaimed_xml = _build_metadata_xml(
            in_use="true", task_id="new-task", started_at="2026-02-01T00:00:00"
        )

        domain = make_mock_domain()
        domain.metadata.side_effect = [
            # is_in_use() -> reads in_use="false"
            released_xml,
            # mark_in_use -> set_metadata_bulk reads existing
            released_xml,
            # get_task_id() re-read -> we won
            reclaimed_xml,
        ]
        mgr = MetadataManager(domain)

        result = mgr.try_claim("new-task")

        assert result is True
        mock_sleep.assert_called_once_with(0.15)

    @patch("time.sleep")
    def test_claim_re_read_returns_none(self, mock_sleep):
        """try_claim fails if re-read returns None for task_id (metadata cleared)."""
        domain = make_mock_domain()
        domain.metadata.side_effect = [
            # is_in_use() -> no metadata
            libvirt.libvirtError("No metadata"),
            # mark_in_use -> set_metadata_bulk reads existing
            libvirt.libvirtError("No metadata"),
            # get_task_id() re-read -> metadata was cleared by someone
            libvirt.libvirtError("No metadata"),
        ]
        mgr = MetadataManager(domain)

        result = mgr.try_claim("my-task")

        assert result is False


class TestMarkAvailable:
    """Tests for MetadataManager.mark_available()."""

    @patch("ansible_deployer.metadata_manager.datetime")
    def test_sets_fields_correctly(self, mock_datetime):
        """mark_available resets in_use and task_id, sets finished_at."""
        fake_now = datetime(2026, 3, 20, 15, 45, 0)
        mock_datetime.now.return_value = fake_now

        domain = make_mock_domain(in_use=True, task_id="task-done")
        mgr = MetadataManager(domain)

        mgr.mark_available()

        written = _parse_set_metadata_dict(domain)
        assert written["in_use"] == "false"
        assert written["task_id"] == ""
        assert written["finished_at"] == fake_now.isoformat()

    def test_single_write_call(self):
        """mark_available issues exactly one setMetadata() call."""
        domain = make_mock_domain(in_use=True, task_id="task-1")
        mgr = MetadataManager(domain)

        mgr.mark_available()

        assert domain.setMetadata.call_count == 1

    def test_preserves_started_at(self):
        """mark_available keeps the started_at field from the original metadata."""
        domain = make_mock_domain(
            in_use=True, task_id="task-1", started_at="2026-01-01T00:00:00"
        )
        mgr = MetadataManager(domain)

        mgr.mark_available()

        written = _parse_set_metadata_dict(domain)
        assert written["started_at"] == "2026-01-01T00:00:00"


class TestIsInUse:
    """Tests for MetadataManager.is_in_use()."""

    def test_true_when_in_use(self):
        """is_in_use returns True when metadata says 'true'."""
        domain = make_mock_domain(in_use=True, task_id="task-1")
        mgr = MetadataManager(domain)

        assert mgr.is_in_use() is True

    def test_false_when_not_in_use(self):
        """is_in_use returns False when metadata says 'false'."""
        domain = make_mock_domain(in_use=False, task_id="old")
        mgr = MetadataManager(domain)

        assert mgr.is_in_use() is False

    def test_false_when_no_metadata(self):
        """is_in_use returns False when no metadata exists at all."""
        domain = make_mock_domain()
        mgr = MetadataManager(domain)

        assert mgr.is_in_use() is False

    def test_false_for_arbitrary_string(self):
        """is_in_use returns False for any value other than 'true'."""
        xml = _build_metadata_xml(in_use="yes")
        domain = make_mock_domain()
        domain.metadata.side_effect = None
        domain.metadata.return_value = xml
        mgr = MetadataManager(domain)

        assert mgr.is_in_use() is False


class TestGetTaskId:
    """Tests for MetadataManager.get_task_id()."""

    def test_returns_task_id(self):
        """get_task_id returns the current task_id."""
        domain = make_mock_domain(in_use=True, task_id="deploy-xyz")
        mgr = MetadataManager(domain)

        assert mgr.get_task_id() == "deploy-xyz"

    def test_returns_none_when_no_metadata(self):
        """get_task_id returns None when no metadata exists."""
        domain = make_mock_domain()
        mgr = MetadataManager(domain)

        assert mgr.get_task_id() is None

    def test_returns_empty_string_when_empty(self):
        """get_task_id returns empty string when task_id is empty."""
        xml = _build_metadata_xml(in_use="false", task_id="")
        domain = make_mock_domain()
        domain.metadata.side_effect = None
        domain.metadata.return_value = xml
        mgr = MetadataManager(domain)

        # Empty element text may be None in ET, but if the element has ""
        # text, get_metadata returns that
        result = mgr.get_task_id()
        # ET.fromstring parses <avm:task_id></avm:task_id> as text=None
        # and <avm:task_id>value</avm:task_id> as text="value"
        # An empty string between tags yields text=None in ElementTree
        assert result is None or result == ""


class TestClearMetadata:
    """Tests for MetadataManager.clear_metadata()."""

    def test_successful_clear(self):
        """clear_metadata sets metadata to None."""
        domain = make_mock_domain(in_use=True, task_id="task-1")
        mgr = MetadataManager(domain)

        mgr.clear_metadata()

        domain.setMetadata.assert_called_once_with(
            libvirt.VIR_DOMAIN_METADATA_ELEMENT,
            None,
            PREFIX,
            NAMESPACE,
            FLAGS,
        )

    def test_clear_when_no_metadata_exists(self):
        """clear_metadata silently succeeds when metadata doesn't exist."""
        domain = make_mock_domain()
        domain.setMetadata.side_effect = libvirt.libvirtError(
            "Requested metadata element is not present"
        )
        mgr = MetadataManager(domain)

        # Should not raise
        mgr.clear_metadata()

    def test_clear_calls_setMetadata_once(self):
        """clear_metadata makes exactly one setMetadata() call."""
        domain = make_mock_domain(in_use=True, task_id="t")
        mgr = MetadataManager(domain)

        mgr.clear_metadata()

        assert domain.setMetadata.call_count == 1


class TestFindElement:
    """Tests for MetadataManager._find_element() namespace handling."""

    def test_finds_element_with_namespace(self):
        """_find_element finds elements stored with full namespace URI."""
        mgr = MetadataManager(make_mock_domain())
        root = ET.fromstring(
            f'<metadata xmlns="{NAMESPACE}">'
            f"<in_use>true</in_use>"
            f"</metadata>"
        )

        element = mgr._find_element(root, "in_use")
        assert element is not None
        assert element.text == "true"

    def test_finds_element_without_namespace(self):
        """_find_element finds elements stored without namespace prefix."""
        mgr = MetadataManager(make_mock_domain())
        root = ET.fromstring(
            "<metadata><in_use>true</in_use></metadata>"
        )

        element = mgr._find_element(root, "in_use")
        assert element is not None
        assert element.text == "true"

    def test_returns_none_for_missing_element(self):
        """_find_element returns None when element doesn't exist."""
        mgr = MetadataManager(make_mock_domain())
        root = ET.fromstring(
            "<metadata><in_use>true</in_use></metadata>"
        )

        element = mgr._find_element(root, "nonexistent")
        assert element is None


class TestConstructor:
    """Tests for MetadataManager initialization."""

    def test_stores_domain(self):
        """MetadataManager stores the domain reference."""
        domain = make_mock_domain()
        mgr = MetadataManager(domain)

        assert mgr.domain is domain

    def test_namespace_constants(self):
        """MetadataManager has correct namespace constants."""
        assert MetadataManager.NAMESPACE == NAMESPACE
        assert MetadataManager.NAMESPACE_PREFIX == PREFIX
