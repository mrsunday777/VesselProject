#!/usr/bin/env python3
"""
Spawn Gate Controller — MsWednesday's HMAC-signed enforcement layer.

Usage:
    python3 spawn_gate.py authorize <agent>          # Create signed gate (24h expiry)
    python3 spawn_gate.py authorize <agent> --hours N # Custom expiry
    python3 spawn_gate.py revoke <agent>             # Zero-fill and delete gate
    python3 spawn_gate.py status <agent>             # Check single agent
    python3 spawn_gate.py status-all                 # Check all agents
    python3 spawn_gate.py verify <workspace_path>    # Verify gate at path

Security:
    - Gates are HMAC-SHA256 signed with .spawn_secret
    - Signature covers: agent + timestamp + expires_at
    - Gates expire after 24 hours by default
    - Revocation zero-fills the gate file before deletion
    - .spawn_secret is chmod 600 (owner-only read)
"""

import sys
import json
import hmac
import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path

AUDIT_LOG = Path.home() / 'spawn_gate_audit.log'


def _audit(action: str, details: str):
    """Append to spawn gate audit log. Every authorize/revoke/verify/kill is recorded."""
    entry = f"{datetime.now().isoformat()} {action} {details}"
    try:
        with open(AUDIT_LOG, 'a') as f:
            f.write(entry + '\n')
    except IOError:
        pass

WEDNESDAY_DIR = Path(__file__).resolve().parent
SECRET_FILE = WEDNESDAY_DIR / '.spawn_secret'

AGENT_WORKSPACES = {
    'MsSunday': Path.home() / 'Desktop' / 'MsSunday',
    'msSunday': Path.home() / 'Desktop' / 'MsSunday',  # alias
    'cp1': Path.home() / 'Desktop' / 'cp1',
    'CP1': Path.home() / 'Desktop' / 'cp1',  # alias
    'CP9': Path.home() / 'Desktop' / 'CP9',
    'cp0': Path.home() / 'Desktop' / 'cp0',
    'CP0': Path.home() / 'Desktop' / 'cp0',  # alias
    'msCounsel': Path.home() / 'Desktop' / 'msCounsel',
    'Chopper': Path.home() / 'Desktop' / 'Chopper',
    'vessel-phone-01': Path.home() / 'Desktop' / 'VesselProject',
}

DEFAULT_EXPIRY_HOURS = 24


def _load_secret():
    """Load HMAC secret from .spawn_secret file."""
    if not SECRET_FILE.exists():
        print("FATAL: .spawn_secret not found at", SECRET_FILE)
        print("Generate with: python3 -c \"import secrets; print(secrets.token_hex(32))\" > .spawn_secret")
        sys.exit(1)

    secret = SECRET_FILE.read_text().strip()
    if len(secret) != 64:
        print("FATAL: .spawn_secret must be exactly 64 hex characters")
        sys.exit(1)

    return secret.encode()


def _compute_signature(secret: bytes, agent: str, timestamp: str, expires_at: str) -> str:
    """Compute HMAC-SHA256 signature over gate fields."""
    message = f"{agent}|{timestamp}|{expires_at}"
    return hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()


def _zero_fill_and_delete(path: Path):
    """Overwrite file contents with zeros before deleting (prevents disk recovery)."""
    if path.exists():
        size = path.stat().st_size
        with open(path, 'wb') as f:
            f.write(b'\x00' * size)
            f.flush()
            os.fsync(f.fileno())
        path.unlink()


def authorize(agent_name: str, expiry_hours: int = DEFAULT_EXPIRY_HOURS):
    """Create HMAC-signed spawn gate for an agent."""
    workspace = AGENT_WORKSPACES.get(agent_name)
    if not workspace:
        print(f"Unknown agent: {agent_name}")
        print(f"Known agents: {', '.join(AGENT_WORKSPACES.keys())}")
        return False

    if not workspace.exists():
        print(f"Workspace does not exist: {workspace}")
        return False

    secret = _load_secret()
    now = datetime.now()
    expires = now + timedelta(hours=expiry_hours)

    timestamp_str = now.isoformat()
    expires_str = expires.isoformat()
    signature = _compute_signature(secret, agent_name, timestamp_str, expires_str)

    gate_data = {
        'authorized_by': 'MsWednesday',
        'agent': agent_name,
        'timestamp': timestamp_str,
        'expires_at': expires_str,
        'signature': signature,
    }

    gate_file = workspace / '.spawn_gate'

    # Atomic write: write to temp file then rename
    tmp_path = gate_file.with_suffix('.spawn_gate.tmp')
    try:
        with open(tmp_path, 'w') as f:
            json.dump(gate_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(gate_file))
    except Exception:
        if tmp_path.exists():
            _zero_fill_and_delete(tmp_path)
        raise

    _audit('AUTHORIZE', f"{agent_name} expires={expires_str}")

    print(f"AUTHORIZED: {agent_name}")
    print(f"  Path: {gate_file}")
    print(f"  Created: {timestamp_str}")
    print(f"  Expires: {expires_str} ({expiry_hours}h)")
    print(f"  Signature: {signature[:16]}...")
    return True


def revoke(agent_name: str):
    """Zero-fill and delete spawn gate for an agent."""
    workspace = AGENT_WORKSPACES.get(agent_name)
    if not workspace:
        print(f"Unknown agent: {agent_name}")
        return False

    gate_file = workspace / '.spawn_gate'
    if gate_file.exists():
        _zero_fill_and_delete(gate_file)
        _audit('REVOKE', agent_name)
        print(f"REVOKED: {agent_name} — gate file zero-filled and deleted")
    else:
        _audit('REVOKE_NOOP', f"{agent_name} (no gate file)")
        print(f"{agent_name}: no gate file found (already unauthorized)")
    return True


def verify(workspace_path: str) -> bool:
    """Verify HMAC-signed spawn gate at a workspace path. Returns True if valid."""
    workspace = Path(workspace_path).resolve()
    gate_file = workspace / '.spawn_gate'

    if not gate_file.exists():
        _audit('VERIFY_FAIL', f"workspace={workspace} reason=no_gate_file")
        print(f"UNAUTHORIZED: No .spawn_gate found at {workspace}")
        return False

    try:
        with open(gate_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _audit('VERIFY_FAIL', f"workspace={workspace} reason=corrupt_file error={e}")
        print(f"UNAUTHORIZED: Failed to read gate file: {e}")
        return False

    # Check required fields
    required = ['authorized_by', 'agent', 'timestamp', 'expires_at', 'signature']
    for field in required:
        if field not in data:
            _audit('VERIFY_FAIL', f"workspace={workspace} reason=missing_field:{field}")
            print(f"UNAUTHORIZED: Missing field '{field}' in gate file")
            return False

    # Check authorized_by
    if data['authorized_by'] != 'MsWednesday':
        _audit('VERIFY_FAIL', f"agent={data.get('agent')} reason=wrong_authority:{data['authorized_by']}")
        print(f"UNAUTHORIZED: authorized_by is '{data['authorized_by']}', must be 'MsWednesday'")
        return False

    # Check expiry
    try:
        expires = datetime.fromisoformat(data['expires_at'])
    except ValueError:
        _audit('VERIFY_FAIL', f"agent={data.get('agent')} reason=invalid_expiry")
        print(f"UNAUTHORIZED: Invalid expires_at format")
        return False

    if datetime.now() > expires:
        _audit('VERIFY_FAIL', f"agent={data.get('agent')} reason=expired at={data['expires_at']}")
        print(f"UNAUTHORIZED: Gate expired at {data['expires_at']}")
        return False

    # Verify HMAC signature
    secret = _load_secret()
    expected_sig = _compute_signature(
        secret, data['agent'], data['timestamp'], data['expires_at']
    )

    if not hmac.compare_digest(data['signature'], expected_sig):
        _audit('VERIFY_FAIL', f"agent={data.get('agent')} reason=invalid_hmac")
        print(f"UNAUTHORIZED: Invalid HMAC signature (forged or tampered gate)")
        return False

    _audit('VERIFY_OK', f"agent={data['agent']} expires={data['expires_at']}")
    print(f"AUTHORIZED: {data['agent']}")
    print(f"  By: {data['authorized_by']}")
    print(f"  Since: {data['timestamp']}")
    print(f"  Expires: {data['expires_at']}")
    return True


def status(agent_name: str):
    """Check authorization status of an agent."""
    workspace = AGENT_WORKSPACES.get(agent_name)
    if not workspace:
        print(f"Unknown agent: {agent_name}")
        return

    gate_file = workspace / '.spawn_gate'
    if not gate_file.exists():
        print(f"{agent_name}: NOT AUTHORIZED (no gate file)")
        return

    try:
        with open(gate_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        print(f"{agent_name}: CORRUPT gate file")
        return

    # Check expiry
    try:
        expires = datetime.fromisoformat(data['expires_at'])
        expired = datetime.now() > expires
    except (ValueError, KeyError):
        expired = True

    # Check signature
    try:
        secret = _load_secret()
        expected_sig = _compute_signature(
            secret, data['agent'], data['timestamp'], data['expires_at']
        )
        sig_valid = hmac.compare_digest(data.get('signature', ''), expected_sig)
    except Exception:
        sig_valid = False

    if expired:
        print(f"{agent_name}: EXPIRED (gate expired at {data.get('expires_at', '?')})")
    elif not sig_valid:
        print(f"{agent_name}: INVALID SIGNATURE (forged or tampered)")
    else:
        print(f"{agent_name}: AUTHORIZED")
        print(f"  By: {data.get('authorized_by')}")
        print(f"  Since: {data.get('timestamp')}")
        print(f"  Expires: {data.get('expires_at')}")


def status_all():
    """Check all agent authorization statuses."""
    print("Spawn Gate Status (HMAC-Signed)")
    print("=" * 50)
    for name in AGENT_WORKSPACES:
        status(name)
        print()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    action = sys.argv[1]

    if action == 'status-all':
        status_all()
    elif action == 'verify':
        if len(sys.argv) < 3:
            print("Usage: python3 spawn_gate.py verify <workspace_path>")
            sys.exit(1)
        ok = verify(sys.argv[2])
        sys.exit(0 if ok else 1)
    elif len(sys.argv) < 3:
        print("Need agent name. Usage: python3 spawn_gate.py authorize MsSunday")
        sys.exit(1)
    elif action == 'authorize':
        hours = DEFAULT_EXPIRY_HOURS
        if '--hours' in sys.argv:
            idx = sys.argv.index('--hours')
            if idx + 1 < len(sys.argv):
                hours = int(sys.argv[idx + 1])
        authorize(sys.argv[2], hours)
    elif action == 'revoke':
        revoke(sys.argv[2])
    elif action == 'status':
        status(sys.argv[2])
    else:
        print(f"Unknown action: {action}")
        print(__doc__)
        sys.exit(1)
