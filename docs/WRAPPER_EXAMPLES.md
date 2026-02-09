# Ansible Wrapper Script Examples

This document provides practical examples for customizing the `ansible-wrapper.sh` script.

## Table of Contents

1. [How It Works](#how-it-works)
2. [Basic Usage](#basic-usage)
3. [Performance Tuning](#performance-tuning)
4. [Logging and Monitoring](#logging-and-monitoring)
5. [Validation and Safety](#validation-and-safety)
6. [Integration Examples](#integration-examples)
7. [Advanced Patterns](#advanced-patterns)

---

## How It Works

### Working Directory

When you use `--project-root`, the wrapper script is executed with its working directory set to the project root. This allows you to:
- Reference files using relative paths (e.g., `./scripts/pre-deploy.sh`)
- Source environment files (e.g., `. ./env/production.env`)
- Access configuration files without absolute paths

**Without `--project-root`:** Working directory is wherever you ran the command  
**With `--project-root`:** Working directory is set to the project root

### Arguments Passed to Wrapper

When `ansible-deployer` executes the wrapper script, it passes these arguments:

| Position | Argument | Example | When Present |
|----------|----------|---------|--------------|
| `$1` | Playbook path | `/path/to/playbook.yml` | Always |
| `$2+` | Inventory | `-i /path/to/inventory` | If `--inventory` specified |
| `$N` | Extra vars | `--extra-vars '{"key":"val"}'` | If `--extra-vars` specified |
| `$N+1` | Additional flags | `--check --diff -vvv` | If `--ansible-flags` specified |

**Full command example with all options:**
```bash
ansible-wrapper.sh /path/to/playbook.yml -i /path/to/inventory --extra-vars '{"version":"1.0"}' --check --diff -vvv
```

**Basic example (no extra flags):**
```bash
ansible-wrapper.sh /path/to/playbook.yml
```

**With verbosity (via --ansible-flags):**
```bash
ansible-wrapper.sh /path/to/playbook.yml -vvv
```

**Note:** Use the `--ansible-flags` CLI option to pass any flags including verbosity, check mode, etc.

### Environment Variables

All environment variables are passed to the wrapper:

| Variable | Example Value | Description |
|----------|---------------|-------------|
| `VM_IP_1` | `192.168.1.100` | First VM IP address |
| `VM_IP_2` | `192.168.1.101` | Second VM IP address (if multi-VM) |
| `VM_IP_ALL` | `192.168.1.100,192.168.1.101` | Comma-separated list of all VM IPs |

### Using `"$@"`

The `"$@"` variable expands to all arguments passed to the script:
```bash
exec ansible-playbook "$@"
# Becomes: ansible-playbook /path/to/playbook.yml -i /path/to/inventory --extra-vars '...' --check -vvv
```

---

## Basic Usage

### Default Wrapper (Shipped)

```bash
#!/usr/bin/env bash
set -e

# Execute ansible-playbook with all arguments
exec ansible-playbook "$@"
```

### Add Verbosity

```bash
#!/usr/bin/env bash
set -e

# Always run with extra verbosity
exec ansible-playbook "$@" -vvv
```

### Use Relative Paths (with --project-root)

```bash
#!/usr/bin/env bash
set -e

# Working directory is project root when --project-root is used
# Reference files relative to project root
source ./env/production.env

# Run pre-deployment checks
./scripts/pre-deploy-check.sh

# Execute ansible-playbook
exec ansible-playbook "$@"
```

**Project structure:**
```
/path/to/project/
├── ansible-wrapper.sh          # This script
├── env/
│   └── production.env
├── scripts/
│   └── pre-deploy-check.sh
└── playbooks/
    └── deploy.yml
```

**Run with:**
```bash
ansible-deployer --project-root /path/to/project deploy \
  --tag prod --playbook playbooks/deploy.yml
```

---

## Performance Tuning

### Increase Parallelism

```bash
#!/usr/bin/env bash
set -e

# Run with more parallel forks
CUSTOM_FLAGS=(
    "--forks" "50"
    "--timeout" "120"
)

exec ansible-playbook "$@" ${CUSTOM_FLAGS[@]}
```

### Optimize SSH Connections

```bash
#!/usr/bin/env bash
set -e

# Disable SSH host key checking and enable connection reuse
export ANSIBLE_SSH_ARGS="-o StrictHostKeyChecking=no -o ControlMaster=auto -o ControlPersist=60s"

exec ansible-playbook "$@" --forks 20
```

---

## Logging and Monitoring

### Detailed Deployment Logging

```bash
#!/usr/bin/env bash
set -e

LOG_DIR="/var/log/ansible-deployments"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/deployment_${TIMESTAMP}.log"

# Log deployment metadata
{
    echo "=== Deployment Started: $(date) ==="
    echo "VMs: $VM_IP_ALL"
    echo "Playbook: $1"
    echo "User: $USER"
    echo "=================================="
    echo ""
} >> "$LOG_FILE"

# Execute and capture timing
START_TIME=$(date +%s)
exec ansible-playbook "$@" 2>&1 | tee -a "$LOG_FILE"
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

echo "Duration: ${DURATION}s" >> "$LOG_FILE"
```

### Send Metrics to Monitoring

```bash
#!/usr/bin/env bash
set -e

# Send deployment metrics to StatsD/Graphite
function send_metric() {
    echo "deployments.started:1|c" | nc -u -w0 localhost 8125
}

send_metric

exec ansible-playbook "$@"
```

---

## Validation and Safety

### Pre-Flight Checks

```bash
#!/usr/bin/env bash
set -e

# Validate VMs are allocated
if [[ -z "$VM_IP_1" ]]; then
    echo "ERROR: No VMs allocated" >&2
    exit 1
fi

# Count VMs
VM_COUNT=$(echo "$VM_IP_ALL" | tr ',' '\n' | wc -l)
echo "Deploying to $VM_COUNT VM(s): $VM_IP_ALL"

# Check playbook exists
PLAYBOOK="$1"
if [[ ! -f "$PLAYBOOK" ]]; then
    echo "ERROR: Playbook not found: $PLAYBOOK" >&2
    exit 1
fi

# Connectivity check
echo "Checking connectivity..."
for ip in $(echo "$VM_IP_ALL" | tr ',' ' '); do
    if ! ping -c 1 -W 2 "$ip" > /dev/null 2>&1; then
        echo "WARNING: VM $ip not responding to ping" >&2
    else
        echo "  ✓ $ip reachable"
    fi
done

exec ansible-playbook "$@"
```

### Production Safety

```bash
#!/usr/bin/env bash
set -e

# Require explicit confirmation for production deployments
if [[ "$VM_IP_ALL" == *"10.0."* ]]; then
    echo "WARNING: Deploying to PRODUCTION network" >&2
    echo "VMs: $VM_IP_ALL" >&2
    
    # In non-interactive mode (CI), block production
    if [[ ! -t 0 ]]; then
        echo "ERROR: Production deployments require manual approval" >&2
        exit 1
    fi
    
    read -p "Continue? (type 'yes'): " confirm
    if [[ "$confirm" != "yes" ]]; then
        echo "Deployment cancelled" >&2
        exit 1
    fi
fi

exec ansible-playbook "$@"
```

---

## Integration Examples

### Slack Notifications

```bash
#!/usr/bin/env bash
set -e

SLACK_WEBHOOK="https://hooks.slack.com/services/YOUR/WEBHOOK/URL"

# Send start notification
function notify_start() {
    curl -X POST "$SLACK_WEBHOOK" \
        -H 'Content-Type: application/json' \
        -d "{
            \"text\": \"🚀 Deployment started\",
            \"attachments\": [{
                \"color\": \"#36a64f\",
                \"fields\": [
                    {\"title\": \"VMs\", \"value\": \"$VM_IP_ALL\", \"short\": false},
                    {\"title\": \"Playbook\", \"value\": \"$1\", \"short\": false}
                ]
            }]
        }" 2>/dev/null || true
}

notify_start

exec ansible-playbook "$@"
```

### Git Commit Tracking

```bash
#!/usr/bin/env bash
set -e

# Record which git commit is being deployed
GIT_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")

export ANSIBLE_CALLBACK_WHITELIST="profile_tasks"

echo "Deploying commit: $GIT_COMMIT (branch: $GIT_BRANCH)"
echo "To VMs: $VM_IP_ALL"

exec ansible-playbook "$@" \
    --extra-vars "git_commit=$GIT_COMMIT git_branch=$GIT_BRANCH"
```

### Database Change Log

```bash
#!/usr/bin/env bash
set -e

# Log deployment to database
function log_deployment() {
    psql -h db.example.com -U logger -d deployments -c "
        INSERT INTO deployment_log (timestamp, vms, playbook, user)
        VALUES (NOW(), '$VM_IP_ALL', '$1', '$USER');
    " 2>/dev/null || echo "Warning: Failed to log deployment"
}

log_deployment

exec ansible-playbook "$@"
```

---

## Advanced Patterns

### Dynamic Inventory Generation

```bash
#!/usr/bin/env bash
set -e

# Generate dynamic inventory file from VM_IP_ALL
INVENTORY_FILE=$(mktemp)
trap "rm -f $INVENTORY_FILE" EXIT

echo "[all]" > "$INVENTORY_FILE"
for ip in $(echo "$VM_IP_ALL" | tr ',' ' '); do
    echo "$ip" >> "$INVENTORY_FILE"
done

echo "Generated inventory:"
cat "$INVENTORY_FILE"

# Use generated inventory
exec ansible-playbook "$@" -i "$INVENTORY_FILE"
```

### Conditional Configuration

```bash
#!/usr/bin/env bash
set -e

# Select Ansible config based on VM network
if [[ "$VM_IP_ALL" == *"192.168.1."* ]]; then
    export ANSIBLE_CONFIG="/etc/ansible/mgmt-network.cfg"
    echo "Using management network config"
elif [[ "$VM_IP_ALL" == *"10.0."* ]]; then
    export ANSIBLE_CONFIG="/etc/ansible/production.cfg"
    echo "Using production network config"
else
    export ANSIBLE_CONFIG="/etc/ansible/default.cfg"
    echo "Using default config"
fi

exec ansible-playbook "$@"
```

### Retry Logic

```bash
#!/usr/bin/env bash
set -e

MAX_RETRIES=3
RETRY_DELAY=5

for attempt in $(seq 1 $MAX_RETRIES); do
    echo "Attempt $attempt of $MAX_RETRIES..."
    
    if ansible-playbook "$@"; then
        echo "Deployment successful"
        exit 0
    else
        if [[ $attempt -lt $MAX_RETRIES ]]; then
            echo "Deployment failed, retrying in ${RETRY_DELAY}s..."
            sleep $RETRY_DELAY
        else
            echo "Deployment failed after $MAX_RETRIES attempts"
            exit 1
        fi
    fi
done
```

### Multi-Stage Deployments

```bash
#!/usr/bin/env bash
set -e

# Run pre-deployment checks
echo "Running pre-deployment checks..."
ansible-playbook "$@" --tags "pre-check" --check

# Deploy to first VM only (canary)
if [[ -n "$VM_IP_1" ]] && [[ $(echo "$VM_IP_ALL" | tr ',' '\n' | wc -l) -gt 1 ]]; then
    echo "Canary deployment to $VM_IP_1..."
    ansible-playbook "$@" --limit "$VM_IP_1"
    
    echo "Canary deployed, waiting 30s for validation..."
    sleep 30
    
    # Could add health check here
fi

# Deploy to all VMs
echo "Deploying to all VMs..."
exec ansible-playbook "$@"
```

---

## Tips and Best Practices

1. **Always use `set -e`** to exit on errors
2. **Use `exec`** for the final ansible-playbook call to preserve exit codes
3. **Log errors to stderr** with `>&2`
4. **Test wrapper changes** before deploying to production
5. **Use environment variables** instead of hardcoded values
6. **Handle cleanup** with `trap` for temporary files
7. **Make it portable** - use `#!/usr/bin/env bash`

---

## Testing Your Wrapper

```bash
# Test with mock environment
VM_IP_1=192.168.1.100 \
VM_IP_2=192.168.1.101 \
VM_IP_ALL=192.168.1.100,192.168.1.101 \
./ansible-wrapper.sh --version

# Dry run
./ansible-wrapper.sh playbooks/test.yml --check

# With verbose output
./ansible-wrapper.sh playbooks/test.yml -vvv
```
