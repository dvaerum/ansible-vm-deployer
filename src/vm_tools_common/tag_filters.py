"""
Tag filtering logic for VM selection.
"""
from typing import List, Optional


def vm_matches_tags(
    vm_tags: List[str],
    required_tags: List[str],
    exclude_tags: Optional[List[str]] = None
) -> bool:
    """Check if a VM's tags match the required/excluded criteria.
    
    Args:
        vm_tags: List of tags the VM has
        required_tags: List of tags to match (VM must have at least one)
        exclude_tags: List of tags to exclude (VM must have none of these)
        
    Returns:
        True if VM matches criteria, False otherwise
    """
    exclude_tags = exclude_tags or []
    
    # Skip if VM has any excluded tags
    if exclude_tags and any(tag in vm_tags for tag in exclude_tags):
        return False
    
    # Check if VM has any of the required tags
    if any(tag in vm_tags for tag in required_tags):
        return True
    
    return False
