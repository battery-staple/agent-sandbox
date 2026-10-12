# Future Improvement: State Snapshotting & Instant Rollbacks for Agent Sandbox

This document outlines an optional future enhancement for the **Agent Sandbox**: adding **Instant Container Snapshots & State Rollbacks** to checkpoint the sandbox environment before executing experimental or destructive agent tasks.

---

## 1. Overview & Motivation

In the base sandbox architecture, system-level packages and development toolchains are declaratively installed and managed in `Dockerfile.sandbox`. 

While this ensures a clean, reproducible base image, developers occasionally want the ability to:
- **Test experimental agent workflows**: Give an agent freedom to modify system files or test package migrations with a 1-click safety net.
- **Instant Rollbacks**: Revert the container image and persistent user volumes to an earlier timestamped checkpoint in ~2 seconds without rebuilding from scratch.

---

## 2. Architecture & Under-the-Hood Mechanics

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer
    participant Host as Agent Sandbox CLI / Docker
    participant Ctr as Docker Container (agent-sandbox-<engine>)
    participant Vol as Persistent Volume (agent_home_<engine>)
    participant Snap as Snapshot Storage (~/.agent-sandbox/snapshots)

    Note over Dev,Snap: Phase 1: Create Snapshot Checkpoint
    Dev->>Host: agent-sandbox snapshot create "checkpoint-1"
    Host->>Ctr: docker commit (Save container rootfs to image tag)
    Host->>Vol: tar -czf (Archive /home/developer)
    Vol-->>Snap: Save checkpoint-1_home.tar.gz

    Note over Dev,Snap: Phase 2: Agent Executes Tasks
    Ctr->>Ctr: Agent runs arbitrary shell commands / modifications

    Note over Dev,Snap: Phase 3: Instant Rollback (2 Seconds)
    Dev->>Host: agent-sandbox snapshot rollback "checkpoint-1"
    Host->>Ctr: docker stop & docker tag "checkpoint-1" latest
    Host->>Snap: Extract checkpoint-1_home.tar.gz
    Snap-->>Vol: Restore /home/developer files
    Host->>Ctr: docker compose up -d --force-recreate (Restored)
```

---

## 3. Proposed CLI Implementation

To add snapshot management to `bin/agent-sandbox`:

The example below targets the `antigravity` engine (container `agent-sandbox-antigravity`, volume `agent_home_antigravity`); substitute the target engine name for any other engine.

```bash
# 1. Create a snapshot before a risky task
agent-sandbox snapshot create "pre-migration-v1"

# 2. List available checkpoints
agent-sandbox snapshot list

# 3. Rollback to checkpoint in 2 seconds
agent-sandbox snapshot rollback "pre-migration-v1"
```

### Script Implementation Logic
```bash
case "$1" in
  snapshot)
    ACTION="$2"
    TAG="$3"
    SNAPSHOT_DIR="$HOME/.agent-sandbox/snapshots"
    mkdir -p "$SNAPSHOT_DIR"

    case "$ACTION" in
      create)
        TAG="${TAG:-snapshot-$(date +%Y%m%d_%H%M%S)}"
        echo "[Snapshot] Checkpointing container rootfs: $TAG..."
        docker commit agent-sandbox-antigravity "agent-sandbox:$TAG"
        
        echo "[Snapshot] Archiving persistent home volume..."
        docker run --rm -v agent_home_antigravity:/data -v "$SNAPSHOT_DIR:/backup" \
          agent-sandbox:latest tar -czf "/backup/${TAG}_home.tar.gz" -C /data .
        echo "[Snapshot] Checkpoint $TAG created successfully."
        ;;
      list)
        docker images "agent-sandbox" --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}"
        ;;
      rollback)
        if [ -z "$TAG" ]; then echo "Error: specify tag to rollback"; exit 1; fi
        echo "[Snapshot] Rolling back to $TAG..."
        docker stop agent-sandbox-antigravity
        docker tag "agent-sandbox:$TAG" agent-sandbox:latest
        
        # Restore volume
        docker run --rm -v agent_home_antigravity:/data -v "$SNAPSHOT_DIR:/backup" \
          agent-sandbox:latest sh -c "rm -rf /data/*; tar -xzf /backup/${TAG}_home.tar.gz -C /data"
        
        docker compose up -d --force-recreate
        echo "[Snapshot] Rollback to $TAG complete."
        ;;
    esac
    ;;
esac
```

---

## 4. Why This is Recommended as a Future Add-on

1. **Keep Base Setup Ultra-Light**: By relying on `Dockerfile.sandbox` for standard package management, the base sandbox avoids auxiliary backup directory tracking and tarball storage overhead.
2. **Modular Extension**: Because the snapshotting logic only interacts with standard Docker APIs (`docker commit`, named volume mounts), it can be dropped into the CLI script whenever instant rollback capabilities are desired.
