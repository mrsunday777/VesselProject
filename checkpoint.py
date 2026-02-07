#!/usr/bin/env python3
"""
Checkpoint System — Automated Save/Restore for MsWednesday + VesselProject

One-command checkpoint creation and restore with safety checklist.
No external dependencies — stdlib only.

Usage:
    python3 checkpoint.py create "Feature Name"
    python3 checkpoint.py list
    python3 checkpoint.py restore "Feature Name"
    python3 checkpoint.py status
"""

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "checkpoint_config.json"
REGISTRY_PATH = SCRIPT_DIR / "CHECKPOINT_REGISTRY.md"
AUDIT_LOG_PATH = SCRIPT_DIR / "CHECKPOINT_AUDIT.log"
CHECKPOINTS_DIR = SCRIPT_DIR / "checkpoints"

# ── Helpers ──────────────────────────────────────────────────────────────────

def load_config():
    if not CONFIG_PATH.exists():
        print(f"ERROR: Config not found at {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def slugify(name):
    """Convert a checkpoint name to a filesystem-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = slug.strip('-')
    return slug


def now_stamp():
    """Return current timestamp in multiple formats."""
    now = datetime.now()
    return {
        "iso": now.strftime("%Y-%m-%d %H:%M:%S"),
        "tag": now.strftime("%Y-%m-%d-%H%M%S"),
        "display": now.strftime("%Y-%m-%d %H:%M:%S PST"),
    }


def run_git(repo_path, *args):
    """Run a git command in the given repo. Returns (returncode, stdout, stderr)."""
    cmd = ["git", "-C", str(repo_path)] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def get_head_sha(repo_path):
    """Get the short SHA of HEAD."""
    rc, out, _ = run_git(repo_path, "rev-parse", "--short", "HEAD")
    return out if rc == 0 else "unknown"


def repo_is_clean(repo_path):
    """Check if a repo has no uncommitted changes."""
    rc, out, _ = run_git(repo_path, "status", "--porcelain")
    return rc == 0 and out == ""


def audit_log(message):
    """Append a timestamped entry to the audit log."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(f"[{ts}] {message}\n")


def print_ok(msg):
    print(f"  \u2705 {msg}")


def print_fail(msg):
    print(f"  \u274c {msg}")


def print_warn(msg):
    print(f"  \u26a0\ufe0f  {msg}")


def print_header(msg):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}\n")


# ── CREATE ───────────────────────────────────────────────────────────────────

def cmd_create(name):
    """Create a checkpoint: commit + tag + snapshot both repos."""
    config = load_config()
    ts = now_stamp()
    slug = slugify(name)
    checkpoint_id = f"{slug}-{ts['tag']}"
    tag_name = f"checkpoint-{ts['tag']}-{slug}"

    print_header(f"CREATE CHECKPOINT: {name}")
    print(f"  ID:  {checkpoint_id}")
    print(f"  Tag: {tag_name}")
    print()

    # Create snapshot directory
    snapshot_dir = CHECKPOINTS_DIR / checkpoint_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    commit_shas = {}

    # Process each repo
    for repo_name, repo_conf in config["repos"].items():
        repo_path = Path(repo_conf["path"])
        print(f"--- {repo_name} ({repo_path}) ---")

        if not repo_path.exists():
            print_fail(f"Repo path does not exist: {repo_path}")
            continue

        # Stage all changes
        run_git(repo_path, "add", "-A")

        # Commit (skip if nothing to commit)
        rc, out, err = run_git(repo_path, "commit", "-m", f"CHECKPOINT: {name}")
        if rc == 0:
            print_ok(f"Committed: CHECKPOINT: {name}")
        else:
            if "nothing to commit" in (out + err):
                print_warn(f"Nothing to commit (clean tree)")
            else:
                print_fail(f"Commit failed: {err}")

        # Tag
        rc, out, err = run_git(repo_path, "tag", tag_name)
        if rc == 0:
            print_ok(f"Tagged: {tag_name}")
        else:
            if "already exists" in err:
                print_warn(f"Tag already exists: {tag_name}")
            else:
                print_fail(f"Tag failed: {err}")

        # Push
        rc, out, err = run_git(repo_path, "push", "origin", "main", "--tags")
        if rc == 0:
            print_ok("Pushed to origin (main + tags)")
        else:
            # Push might fail if no remote configured — not fatal
            print_warn(f"Push skipped or failed: {err[:100]}")

        # Record SHA
        sha = get_head_sha(repo_path)
        commit_shas[repo_name] = sha
        print(f"  HEAD: {sha}")

        # Snapshot state files
        state_dir = snapshot_dir / repo_name
        state_dir.mkdir(parents=True, exist_ok=True)
        captured = 0
        for state_file in repo_conf.get("state_files", []):
            src = repo_path / state_file
            if src.exists():
                dst = state_dir / state_file
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))
                captured += 1
            else:
                print_warn(f"State file not found (skipped): {state_file}")

        print_ok(f"Captured {captured} state files")
        print()

    # Save checkpoint metadata
    meta = {
        "name": name,
        "id": checkpoint_id,
        "tag": tag_name,
        "timestamp": ts["iso"],
        "commits": commit_shas,
        "state_file_count": sum(
            len([f for f in repo_conf.get("state_files", [])
                 if (Path(repo_conf["path"]) / f).exists()])
            for repo_conf in config["repos"].values()
        ),
    }
    with open(snapshot_dir / "checkpoint_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    # Append to registry
    _append_registry(name, ts, tag_name, commit_shas, meta["state_file_count"])

    # Audit log
    audit_log(f"CREATE checkpoint='{name}' id={checkpoint_id} tag={tag_name} "
              f"commits={json.dumps(commit_shas)}")

    print_header("CHECKPOINT CREATED SUCCESSFULLY")
    print(f"  Name:  {name}")
    print(f"  Tag:   {tag_name}")
    for rn, sha in commit_shas.items():
        print(f"  {rn}: {sha}")
    print(f"  State: {meta['state_file_count']} files captured")
    print(f"  Dir:   {snapshot_dir}")
    print()


def _append_registry(name, ts, tag_name, commit_shas, file_count):
    """Append an entry to CHECKPOINT_REGISTRY.md."""
    entry = f"""
## {name}
- **Date:** {ts['display']}
- **Tag:** {tag_name}
"""
    for repo_name, sha in commit_shas.items():
        entry += f"- **{repo_name}:** {sha}\n"
    entry += f"- **State:** {file_count} files captured\n"
    entry += f'- **Restore:** `checkpoint restore "{name}"`\n'

    with open(REGISTRY_PATH, "a") as f:
        f.write(entry)


# ── LIST ─────────────────────────────────────────────────────────────────────

def cmd_list():
    """List all checkpoints from the registry."""
    print_header("ALL CHECKPOINTS")

    if not CHECKPOINTS_DIR.exists():
        print("  No checkpoints found.")
        return

    checkpoints = _load_all_checkpoints()
    if not checkpoints:
        print("  No checkpoints found.")
        return

    for cp in checkpoints:
        commits = "  ".join(f"{k}:{v}" for k, v in cp.get("commits", {}).items())
        print(f"  [{cp['timestamp']}] {cp['name']}")
        print(f"    Tag: {cp['tag']}  |  {commits}")
        print(f"    State: {cp.get('state_file_count', '?')} files")
        print()


def _load_all_checkpoints():
    """Load metadata from all checkpoint directories, sorted by timestamp."""
    checkpoints = []
    if not CHECKPOINTS_DIR.exists():
        return checkpoints

    for entry in sorted(CHECKPOINTS_DIR.iterdir()):
        meta_path = entry / "checkpoint_meta.json"
        if meta_path.exists():
            with open(meta_path) as f:
                checkpoints.append(json.load(f))

    checkpoints.sort(key=lambda c: c.get("timestamp", ""))
    return checkpoints


# ── STATUS ───────────────────────────────────────────────────────────────────

def cmd_status():
    """Show last 5 checkpoints and current repo state."""
    config = load_config()

    print_header("CHECKPOINT STATUS")

    # Current repo state
    print("--- Repository State ---")
    for repo_name, repo_conf in config["repos"].items():
        repo_path = Path(repo_conf["path"])
        sha = get_head_sha(repo_path)
        clean = repo_is_clean(repo_path)
        status = "clean" if clean else "dirty (uncommitted changes)"
        print(f"  {repo_name}: {sha} [{status}]")
    print()

    # Last 5 checkpoints
    checkpoints = _load_all_checkpoints()
    recent = checkpoints[-5:] if len(checkpoints) > 5 else checkpoints

    if not recent:
        print("  No checkpoints found.")
        return

    print(f"--- Last {len(recent)} Checkpoint(s) ---")
    for cp in recent:
        commits = "  ".join(f"{k}:{v}" for k, v in cp.get("commits", {}).items())
        print(f"  [{cp['timestamp']}] {cp['name']}")
        print(f"    Tag: {cp['tag']}  |  {commits}")
        print()


# ── RESTORE ──────────────────────────────────────────────────────────────────

def cmd_restore(name):
    """Restore a checkpoint with safety checklist."""
    config = load_config()

    print_header(f"RESTORE CHECKPOINT: {name}")

    # Find the checkpoint
    checkpoint = _find_checkpoint(name)
    if not checkpoint:
        print_fail(f'Checkpoint not found: "{name}"')
        print("\nAvailable checkpoints:")
        for cp in _load_all_checkpoints():
            print(f'  - "{cp["name"]}"')
        sys.exit(1)

    checkpoint_dir = CHECKPOINTS_DIR / checkpoint["id"]
    tag_name = checkpoint["tag"]

    # ── Phase 1: Pre-flight checks ───────────────────────────────────────

    print("--- Phase 1: Pre-flight Safety Checklist ---\n")
    checks_passed = True
    check_results = []

    # Check 1: Relay reachable
    relay_ok = _check_relay(config)
    check_results.append(("Relay reachable", relay_ok, None))
    if not relay_ok:
        checks_passed = False

    # Check 2: All agents idle
    agents_idle, agent_msg = _check_agents_idle(config)
    check_results.append(("All agents idle", agents_idle, agent_msg))
    if not agents_idle:
        checks_passed = False

    # Check 3: No running sessions
    no_sessions, session_msg = _check_no_sessions(config)
    check_results.append(("No running sessions", no_sessions, session_msg))
    if not no_sessions:
        checks_passed = False

    # Check 4: Both repos clean
    repos_clean = True
    dirty_repos = []
    for repo_name, repo_conf in config["repos"].items():
        repo_path = Path(repo_conf["path"])
        if not repo_is_clean(repo_path):
            repos_clean = False
            dirty_repos.append(repo_name)
    clean_msg = None if repos_clean else f"Dirty: {', '.join(dirty_repos)}"
    check_results.append(("Both repos clean", repos_clean, clean_msg))
    if not repos_clean:
        checks_passed = False

    # Check 5: Checkpoint exists (tag + snapshot)
    cp_exists = True
    cp_msg = None
    if not checkpoint_dir.exists():
        cp_exists = False
        cp_msg = "Snapshot directory missing"
    else:
        # Verify tags exist in repos
        for repo_name, repo_conf in config["repos"].items():
            repo_path = Path(repo_conf["path"])
            rc, _, _ = run_git(repo_path, "rev-parse", tag_name)
            if rc != 0:
                cp_exists = False
                cp_msg = f"Tag missing in {repo_name}"
                break
    check_results.append(("Checkpoint found", cp_exists, cp_msg))
    if not cp_exists:
        checks_passed = False

    # Print results
    for label, passed, msg in check_results:
        if passed:
            print_ok(label)
        else:
            detail = f" ({msg})" if msg else ""
            print_fail(f"{label}{detail}")

    print()

    if not checks_passed:
        print("RESTORE BLOCKED — fix the above issues and retry.")
        actions = []
        for label, passed, msg in check_results:
            if not passed:
                if "agent" in label.lower():
                    actions.append(f"Wait for agents to finish, then retry.")
                elif "session" in label.lower():
                    actions.append(f"Wait for sessions to complete.")
                elif "clean" in label.lower():
                    actions.append(f"Commit or stash changes in: {msg}")
                elif "relay" in label.lower():
                    actions.append(f"Start the relay server (port 8777).")
                elif "checkpoint" in label.lower():
                    actions.append(f"Checkpoint data incomplete: {msg}")
        if actions:
            print("\nSuggested actions:")
            for a in actions:
                print(f"  - {a}")
        print()
        audit_log(f"RESTORE BLOCKED checkpoint='{name}' reason=preflight_failed")
        sys.exit(1)

    # ── Phase 2: Pre-restore snapshot ────────────────────────────────────

    print("--- Phase 2: Pre-restore Snapshot ---\n")
    pre_restore_ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    pre_restore_dir = CHECKPOINTS_DIR / f"pre-restore-{pre_restore_ts}"
    pre_restore_dir.mkdir(parents=True, exist_ok=True)

    total_backed = 0
    for repo_name, repo_conf in config["repos"].items():
        repo_path = Path(repo_conf["path"])
        backup_dir = pre_restore_dir / repo_name
        backup_dir.mkdir(parents=True, exist_ok=True)
        for state_file in repo_conf.get("state_files", []):
            src = repo_path / state_file
            if src.exists():
                dst = backup_dir / state_file
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))
                total_backed += 1

    print_ok(f"Current state backed up ({total_backed} files)")
    print(f"    Location: {pre_restore_dir}")
    print()

    # ── Phase 3: User confirmation ───────────────────────────────────────

    print("--- Phase 3: Confirmation ---\n")
    print(f'  READY TO RESTORE: "{name}"')
    print(f"  Tag: {tag_name}")
    for repo_name, sha in checkpoint.get("commits", {}).items():
        print(f"  {repo_name} commit: {sha}")
    print(f"  State files: {checkpoint.get('state_file_count', '?')} will be restored")
    print(f"  Current state backed up to: {pre_restore_dir.name}/")
    print()

    try:
        answer = input("  Proceed? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""

    if answer != "y":
        print("\n  Restore cancelled.")
        # Clean up the pre-restore snapshot since we're not restoring
        shutil.rmtree(str(pre_restore_dir), ignore_errors=True)
        audit_log(f"RESTORE CANCELLED checkpoint='{name}' (user declined)")
        return

    # ── Phase 4: Execute restore ─────────────────────────────────────────

    print("\n--- Phase 4: Executing Restore ---\n")

    # Checkout tag in each repo
    for repo_name, repo_conf in config["repos"].items():
        repo_path = Path(repo_conf["path"])
        print(f"  {repo_name}: checking out {tag_name}...")
        rc, out, err = run_git(repo_path, "checkout", tag_name)
        if rc == 0:
            print_ok(f"{repo_name}: checked out {tag_name}")
        else:
            print_fail(f"{repo_name}: checkout failed — {err}")
            print("  RESTORE ABORTED — repos may be in inconsistent state.")
            print(f"  Emergency rollback: use files in {pre_restore_dir}")
            audit_log(f"RESTORE FAILED checkpoint='{name}' repo={repo_name} error={err}")
            sys.exit(1)

    # Copy state files from checkpoint snapshot
    total_restored = 0
    for repo_name, repo_conf in config["repos"].items():
        repo_path = Path(repo_conf["path"])
        snapshot_repo_dir = checkpoint_dir / repo_name
        for state_file in repo_conf.get("state_files", []):
            src = snapshot_repo_dir / state_file
            dst = repo_path / state_file
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))
                total_restored += 1

    print_ok(f"Restored {total_restored} state files")

    # Verify critical files exist
    print("\n  Verifying critical files...")
    missing = []
    for repo_name, repo_conf in config["repos"].items():
        repo_path = Path(repo_conf["path"])
        for state_file in repo_conf.get("state_files", []):
            if not (repo_path / state_file).exists():
                missing.append(f"{repo_name}/{state_file}")
    if missing:
        print_warn(f"Missing after restore: {', '.join(missing)}")
    else:
        print_ok("All expected state files present")

    # Ping relay
    relay_ok = _check_relay(config)
    if relay_ok:
        print_ok("Relay still running")
    else:
        print_warn("Relay not reachable — may need restart")

    # Audit
    audit_log(f"RESTORE COMPLETED checkpoint='{name}' tag={tag_name} "
              f"files_restored={total_restored}")

    print_header("RESTORE COMPLETE")
    print(f'  Checkpoint: "{name}"')
    print(f"  Tag: {tag_name}")
    print(f"  Files restored: {total_restored}")
    print(f"  Pre-restore backup: {pre_restore_dir}")
    print()
    print("  NOTE: Repos are in detached HEAD state.")
    print("  To return to main: git checkout main (in each repo)")
    print()


def _find_checkpoint(name):
    """Find a checkpoint by exact name match."""
    for cp in _load_all_checkpoints():
        if cp["name"] == name:
            return cp
    return None


def _check_relay(config):
    """Check if the relay server is reachable."""
    relay_url = config.get("relay_url", "http://localhost:8777")
    try:
        req = urllib.request.Request(
            f"{relay_url}/agents/availability",
            headers={"Authorization": f"Bearer {config.get('relay_secret', '')}"},
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def _check_agents_idle(config):
    """Check if all agents are idle via relay."""
    relay_url = config.get("relay_url", "http://localhost:8777")
    try:
        req = urllib.request.Request(
            f"{relay_url}/agents/availability",
            headers={"Authorization": f"Bearer {config.get('relay_secret', '')}"},
        )
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode())

        # Response format: {"timestamp": "...", "agents": {"CP0": {...}, ...}}
        agents = data.get("agents", {}) if isinstance(data, dict) else {}

        busy = []
        for agent_name, info in agents.items():
            status = info.get("status", "unknown") if isinstance(info, dict) else str(info)
            if status not in ("idle", "available", "offline"):
                busy.append(f"{agent_name} (status: {status})")

        if busy:
            return False, "; ".join(busy)
        return True, None
    except Exception as e:
        return False, f"Could not reach relay: {e}"


def _check_no_sessions(config):
    """Check that no agent sessions are currently running."""
    relay_url = config.get("relay_url", "http://localhost:8777")
    try:
        req = urllib.request.Request(
            f"{relay_url}/agents/sessions",
            headers={"Authorization": f"Bearer {config.get('relay_secret', '')}"},
        )
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode())

        sessions = data if isinstance(data, list) else data.get("sessions", [])
        running = [s for s in sessions
                   if isinstance(s, dict) and s.get("status") == "running"]

        if running:
            names = [s.get("agent_name", s.get("agent", "unknown")) for s in running]
            return False, f"Running: {', '.join(names)}"
        return True, None
    except Exception as e:
        return False, f"Could not reach relay: {e}"


# ── CLI Entry Point ──────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "create":
        if len(sys.argv) < 3:
            print("Usage: checkpoint create \"Feature Name\"")
            sys.exit(1)
        cmd_create(sys.argv[2])

    elif command == "list":
        cmd_list()

    elif command == "status":
        cmd_status()

    elif command == "restore":
        if len(sys.argv) < 3:
            print("Usage: checkpoint restore \"Feature Name\"")
            sys.exit(1)
        cmd_restore(sys.argv[2])

    else:
        print(f"Unknown command: {command}")
        print_usage()
        sys.exit(1)


def print_usage():
    print("""
Checkpoint System — Save/Restore for MsWednesday + VesselProject

Usage:
  checkpoint create "Feature Name"    Commit + tag + snapshot both repos
  checkpoint list                     Show all checkpoints
  checkpoint restore "Feature Name"   Safety checklist -> restore
  checkpoint status                   Last 5 checkpoints + repo state
""")


if __name__ == "__main__":
    main()
