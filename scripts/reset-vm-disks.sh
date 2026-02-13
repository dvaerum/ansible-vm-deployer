#!/usr/bin/env bash
#
# reset-vm-disks.sh — On-broken script for vm-manager.
#
# Wipes and recreates all disks for a broken VM, then starts it.
# Handles both direct file-backed disks (<source file="...">) and
# storage-pool-backed volumes (<source pool="..." volume="...">).
#
# Environment variables (set by vm-manager --on-broken):
#   VM_NAME       — name of the broken VM (required)
#   LIBVIRT_URI   — libvirt connection URI (default: qemu:///system)
#   VM_WAIT_TIME  — max seconds to wait for graceful shutdown (default: 120)
#
set -euo pipefail

VM_NAME="${VM_NAME:?VM_NAME is required}"
LIBVIRT_URI="${LIBVIRT_URI:-qemu:///system}"
VM_WAIT_TIME="${VM_WAIT_TIME:-120}"

V="virsh -c ${LIBVIRT_URI}"

echo "=== VM Disk Reset: ${VM_NAME} ==="

# ---------------------------------------------------------------------------
# 1. Collect disk info from VM XML
# ---------------------------------------------------------------------------
echo "[1/4] Collecting disk info..."

# We parse the inactive XML directly because domblklist shows only volume
# names (not full paths) for pool-backed disks, which breaks qemu-img.
vm_xml=$($V dumpxml "$VM_NAME" --inactive)

declare -a DISK_PATHS=()
declare -a DISK_FMTS=()
declare -a DISK_SIZES=()
declare -a DISK_TYPES=()   # "file" or "volume"
declare -a DISK_POOLS=()   # pool name (volume type only)
declare -a DISK_VOLS=()    # volume name (volume type only)

# Extract each <disk> block from the XML.  We accumulate lines between
# <disk ...> and </disk> into $block, using a separator line to delimit
# complete blocks.  This avoids multiline-read issues with process
# substitution.
_process_disk_block() {
    local block="$1"
    [[ -z "$block" ]] && return

    local disk_type device_type driver_type
    disk_type=$(echo "$block" | grep -oP "disk type='\K[^']+" || echo "")
    device_type=$(echo "$block" | grep -oP "device='\K[^']+" || echo "")

    # Only process actual disks (skip cdrom, floppy, etc.)
    [[ "$device_type" != "disk" ]] && return

    # Get driver format (raw, qcow2, etc.)
    driver_type=$(echo "$block" | grep -oP "<driver[^>]* type='\K[^']+" || echo "")

    if [[ "$disk_type" == "file" ]]; then
        # Direct file: <source file='/path/to/disk.img'/>
        local file_path
        file_path=$(echo "$block" | grep -oP "<source[^>]* file='\K[^']+" || echo "")
        if [[ -z "$file_path" ]]; then
            echo "  WARNING: file-backed disk with no source path, skipping"
            return
        fi
        local img_info fmt size
        img_info=$(qemu-img info --output=json "$file_path")
        fmt=${driver_type:-$(echo "$img_info" | jq -r '.format')}
        size=$(echo "$img_info" | jq -r '.["virtual-size"]')

        DISK_PATHS+=("$file_path")
        DISK_FMTS+=("$fmt")
        DISK_SIZES+=("$size")
        DISK_TYPES+=("file")
        DISK_POOLS+=("")
        DISK_VOLS+=("")
        echo "  [file] $file_path ($fmt, $size bytes)"

    elif [[ "$disk_type" == "volume" ]]; then
        # Pool volume: <source pool='Pool' volume='disk.raw'/>
        local pool vol
        pool=$(echo "$block" | grep -oP "<source[^>]* pool='\K[^']+" || echo "")
        vol=$(echo "$block" | grep -oP "<source[^>]* volume='\K[^']+" || echo "")
        if [[ -z "$pool" || -z "$vol" ]]; then
            echo "  WARNING: volume-backed disk with no pool/volume, skipping"
            return
        fi

        # Resolve the actual filesystem path via virsh
        local file_path img_info fmt size
        file_path=$($V vol-path --pool "$pool" "$vol")
        img_info=$(qemu-img info --output=json "$file_path")
        fmt=${driver_type:-$(echo "$img_info" | jq -r '.format')}
        size=$(echo "$img_info" | jq -r '.["virtual-size"]')

        DISK_PATHS+=("$file_path")
        DISK_FMTS+=("$fmt")
        DISK_SIZES+=("$size")
        DISK_TYPES+=("volume")
        DISK_POOLS+=("$pool")
        DISK_VOLS+=("$vol")
        echo "  [pool:$pool] $file_path ($fmt, $size bytes)"

    else
        echo "  WARNING: unsupported disk type '$disk_type', skipping"
    fi
}

block=""
while IFS= read -r line; do
    if [[ "$line" == "---DISK_SEPARATOR---" ]]; then
        _process_disk_block "$block"
        block=""
    else
        block="${block:+$block }$line"
    fi
done < <(echo "$vm_xml" | awk '/<disk /,/<\/disk>/{print} /<\/disk>/{print "---DISK_SEPARATOR---"}')

if [[ ${#DISK_PATHS[@]} -eq 0 ]]; then
    echo "ERROR: No disks found for VM ${VM_NAME}"
    exit 1
fi

# ---------------------------------------------------------------------------
# 2. Shut down the VM
# ---------------------------------------------------------------------------
echo "[2/4] Shutting down VM..."

state=$($V domstate "$VM_NAME" | tr -d '[:space:]')
if [[ "$state" == "running" ]]; then
    $V shutdown "$VM_NAME" >/dev/null
    elapsed=0
    while [[ $elapsed -lt $VM_WAIT_TIME ]]; do
        state=$($V domstate "$VM_NAME" | tr -d '[:space:]')
        [[ "$state" == "shutoff" ]] && break
        sleep 5
        elapsed=$((elapsed + 5))
    done
    state=$($V domstate "$VM_NAME" | tr -d '[:space:]')
    if [[ "$state" != "shutoff" ]]; then
        echo "  Graceful shutdown timed out after ${VM_WAIT_TIME}s, forcing destroy..."
        $V destroy "$VM_NAME" >/dev/null
        sleep 2
    fi
fi

state=$($V domstate "$VM_NAME" | tr -d '[:space:]')
if [[ "$state" != "shutoff" ]]; then
    echo "ERROR: VM ${VM_NAME} is not shut off (state: $state)"
    exit 1
fi

echo "  VM is shut off."

# ---------------------------------------------------------------------------
# 3. Delete and recreate disks
# ---------------------------------------------------------------------------
echo "[3/4] Recreating disks..."

for i in "${!DISK_PATHS[@]}"; do
    path="${DISK_PATHS[$i]}"
    fmt="${DISK_FMTS[$i]}"
    size="${DISK_SIZES[$i]}"
    dtype="${DISK_TYPES[$i]}"

    echo "  Recreating: $path ($fmt, $size bytes)"

    if [[ "$dtype" == "volume" ]]; then
        pool="${DISK_POOLS[$i]}"
        vol="${DISK_VOLS[$i]}"
        # Delete and recreate through the storage pool API so libvirt
        # stays in sync (pool metadata, permissions, etc.)
        $V vol-delete --pool "$pool" "$vol" >/dev/null 2>&1 || true
        $V vol-create-as --pool "$pool" "$vol" "$size" --format "$fmt" >/dev/null
        # Refresh pool so libvirt picks up the new volume
        $V pool-refresh "$pool" >/dev/null 2>&1 || true
    else
        # Direct file — just delete and recreate
        rm -f "$path"
        qemu-img create -f "$fmt" "$path" "$size"
    fi
done

# ---------------------------------------------------------------------------
# 4. Start the VM
# ---------------------------------------------------------------------------
echo "[4/4] Starting VM..."
$V start "$VM_NAME" >/dev/null
echo "=== Done. ${VM_NAME} is running with fresh disks. ==="
