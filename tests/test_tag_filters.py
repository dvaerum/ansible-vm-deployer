"""
Unit tests for vm_tools_common.tag_filters — pure tag matching logic.

Tests the shared tag filtering used by both ansible-deployer (VM selection)
and vm-manager (event filtering).
"""
import pytest
from vm_tools_common.tag_filters import vm_matches_tags


class TestVMMatchesTags:
    """Test tag matching logic used by both deployer and vm-manager."""

    # --- Basic matching ---

    def test_matches_single_required_tag(self):
        """VM with one matching tag should match."""
        assert vm_matches_tags(
            vm_tags=["linux-test", "linux-test-v1"],
            required_tags=["linux-test"],
        ) is True

    def test_matches_any_required_tag(self):
        """VM matching any one of multiple required tags should match (OR logic)."""
        assert vm_matches_tags(
            vm_tags=["linux-test-v2"],
            required_tags=["linux-test-v1", "linux-test-v2"],
        ) is True

    def test_no_matching_required_tag(self):
        """VM with no matching required tags should not match."""
        assert vm_matches_tags(
            vm_tags=["other-tag"],
            required_tags=["linux-test"],
        ) is False

    def test_empty_vm_tags(self):
        """VM with no tags should not match."""
        assert vm_matches_tags(
            vm_tags=[],
            required_tags=["linux-test"],
        ) is False

    def test_empty_required_tags(self):
        """Empty required tags should not match anything."""
        assert vm_matches_tags(
            vm_tags=["linux-test"],
            required_tags=[],
        ) is False

    # --- Exclude tag logic ---

    def test_exclude_tag_blocks_match(self):
        """VM with an excluded tag should not match, even if required tag present."""
        assert vm_matches_tags(
            vm_tags=["linux-test", "used"],
            required_tags=["linux-test"],
            exclude_tags=["used"],
        ) is False

    def test_exclude_tag_not_present_allows_match(self):
        """VM without excluded tag should match normally."""
        assert vm_matches_tags(
            vm_tags=["linux-test", "linux-test-v1"],
            required_tags=["linux-test"],
            exclude_tags=["used"],
        ) is True

    def test_multiple_exclude_tags_any_blocks(self):
        """Any single excluded tag blocks the match (OR logic)."""
        assert vm_matches_tags(
            vm_tags=["linux-test", "broken"],
            required_tags=["linux-test"],
            exclude_tags=["used", "broken"],
        ) is False

    def test_exclude_tags_none(self):
        """None exclude_tags should be treated as empty list."""
        assert vm_matches_tags(
            vm_tags=["linux-test"],
            required_tags=["linux-test"],
            exclude_tags=None,
        ) is True

    def test_exclude_tags_empty_list(self):
        """Empty exclude_tags list should allow match."""
        assert vm_matches_tags(
            vm_tags=["linux-test"],
            required_tags=["linux-test"],
            exclude_tags=[],
        ) is True

    # --- Real-world scenarios from stress tests ---

    def test_deployer_selects_vm_without_used_tag(self):
        """Deployer should select VMs that have the type tag but not 'used'."""
        assert vm_matches_tags(
            vm_tags=["linux-test", "linux-test-v1"],
            required_tags=["linux-test-v1"],
            exclude_tags=["used"],
        ) is True

    def test_deployer_skips_vm_with_used_tag(self):
        """Deployer should skip VMs that have the 'used' tag."""
        assert vm_matches_tags(
            vm_tags=["linux-test", "linux-test-v1", "used"],
            required_tags=["linux-test-v1"],
            exclude_tags=["used"],
        ) is False

    def test_deployer_skips_vm_with_broken_tag(self):
        """Deployer should skip VMs that have the 'broken' tag."""
        assert vm_matches_tags(
            vm_tags=["linux-test", "linux-test-v1", "used", "broken"],
            required_tags=["linux-test-v1"],
            exclude_tags=["used", "broken"],
        ) is False

    def test_vm_manager_monitors_vm_with_required_tag(self):
        """vm-manager should monitor VMs with the monitoring tag."""
        assert vm_matches_tags(
            vm_tags=["linux-test", "linux-test-v2", "used"],
            required_tags=["linux-test"],
            exclude_tags=[],
        ) is True
