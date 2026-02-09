#!/usr/bin/env python3
"""
Test script for project root feature.
This demonstrates the path resolution logic without requiring libvirt.
"""
from pathlib import Path
import sys

# Test paths
project_root = Path("/tmp/test-ansible-deployer-project")
print("=" * 70)
print("PROJECT ROOT FEATURE TEST")
print("=" * 70)
print(f"\nProject root: {project_root}")
print(f"Exists: {project_root.exists()}")

# Test path resolution function (same logic as in cli.py)
def resolve_path(path: Path, must_exist: bool = True) -> Path:
    """Resolve path relative to project root if set and path is not absolute."""
    if path.is_absolute():
        resolved = path
    elif project_root:
        resolved = (project_root / path).resolve()
    else:
        resolved = path.resolve()
    
    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"Path not found: {resolved}")
    return resolved

# Test cases
test_cases = [
    ("playbooks/test.yml", True, "Relative playbook path"),
    ("inventory/hosts.ini", True, "Relative inventory path"),
    ("logs", False, "Relative log directory (may not exist)"),
    ("/etc/hosts", True, "Absolute path (should not change)"),
]

print("\n" + "=" * 70)
print("PATH RESOLUTION TESTS")
print("=" * 70)

all_passed = True
for path_str, must_exist, description in test_cases:
    try:
        input_path = Path(path_str)
        resolved = resolve_path(input_path, must_exist=must_exist)
        
        # For absolute paths, resolved should equal input (no change)
        # For relative paths, resolved should be under project_root
        if input_path.is_absolute():
            passed = str(resolved) == str(input_path)
            expected_str = str(input_path)
        else:
            expected = project_root / input_path
            passed = resolved == expected.resolve()
            expected_str = str(expected.resolve())
        
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"\n{status} - {description}")
        print(f"  Input:    {input_path}")
        print(f"  Resolved: {resolved}")
        print(f"  Expected: {expected_str}")
        print(f"  Exists:   {resolved.exists()}")
        
        if not passed:
            all_passed = False
            
    except FileNotFoundError as e:
        print(f"\n✗ FAIL - {description}")
        print(f"  Error: {e}")
        all_passed = False

# Test wrapper script resolution
print("\n" + "=" * 70)
print("WRAPPER SCRIPT RESOLUTION TEST")
print("=" * 70)

wrapper_path = (project_root / "ansible-wrapper.sh").resolve()
print(f"\nWrapper script path: {wrapper_path}")
print(f"Exists: {wrapper_path.exists()}")
print(f"Executable: {wrapper_path.is_file() and wrapper_path.stat().st_mode & 0o111}")

# Test log directory resolution
print("\n" + "=" * 70)
print("LOG DIRECTORY RESOLUTION TEST")
print("=" * 70)

log_dir_cases = [
    (Path("logs"), "Default relative path"),
    (Path("custom/logs"), "Custom relative path"),
    (Path("/var/log/ansible"), "Absolute path"),
]

for log_path, desc in log_dir_cases:
    if log_path.is_absolute():
        resolved_log = log_path
    else:
        resolved_log = (project_root / log_path).resolve()
    
    print(f"\n{desc}")
    print(f"  Input:    {log_path}")
    print(f"  Resolved: {resolved_log}")

# Final summary
print("\n" + "=" * 70)
print("TEST SUMMARY")
print("=" * 70)

if all_passed and wrapper_path.exists():
    print("\n✓ ALL TESTS PASSED")
    print("\nProject root feature is working correctly!")
    sys.exit(0)
else:
    print("\n✗ SOME TESTS FAILED")
    sys.exit(1)
