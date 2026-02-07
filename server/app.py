"""
VesselProject - Relay Server
Sits between MsWednesday and the phone vessel.
Wednesday submits tasks via REST, vessel picks them up via WebSocket.
"""

import asyncio
import json
import tempfile
import uuid
import time
import hashlib
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from fastapi import Request

import hmac as hmac_mod
import httpx

# Import config from parent directory (absolute path to handle any working dir)
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
from config import SERVER_HOST, SERVER_PORT, VESSEL_SECRET, AGENT_SESSION_TIMEOUT, AGENT_MAX_TURNS


# --- Spawn Gate Enforcement ---
# Inline HMAC verification (no imports from MsWednesday dir).
# Checks .spawn_gate files before allowing trade-executing endpoints.
# MsWednesday is exempt (she's the spawn authority).

SPAWN_SECRET_PATH = Path.home() / 'Desktop' / 'MsWednesday' / '.spawn_secret'
AGENT_GATE_WORKSPACES = {
    'MsSunday': Path.home() / 'Desktop' / 'MsSunday',
    'msSunday': Path.home() / 'Desktop' / 'MsSunday',  # alias
    'cp1': Path.home() / 'Desktop' / 'cp1',
    'CP9': Path.home() / 'Desktop' / 'CP9',
    'cp0': Path.home() / 'Desktop' / 'cp0',
    'CP0': Path.home() / 'Desktop' / 'cp0',  # alias
    'CP1': Path.home() / 'Desktop' / 'cp1',  # alias
    'msCounsel': Path.home() / 'Desktop' / 'msCounsel',
    'Chopper': Path.home() / 'Desktop' / 'Chopper',
}

# Cache: agent_name -> (valid_until_epoch, is_authorized, gate_mtime)
_gate_cache: dict = {}
_GATE_CACHE_TTL = 60  # seconds


# --- Rate Limiting (in-memory sliding window) ---

# agent_name -> list of timestamps
_rate_limit_trades: dict = {}  # Trade operations (buy/sell/transfer)
_rate_limit_reads: dict = {}   # Read operations (wallet/transactions/positions)

RATE_LIMIT_TRADES_MAX = 5      # max trade operations per window
RATE_LIMIT_TRADES_WINDOW = 60  # seconds
RATE_LIMIT_READS_MAX = 30      # max read operations per window
RATE_LIMIT_READS_WINDOW = 60   # seconds


def _rate_limit_check(agent_name: str, bucket: dict, max_requests: int, window: int, action: str) -> bool:
    """
    Sliding window rate limiter. Returns True if request is allowed, False if rate-limited.
    Prunes expired entries on each call.
    """
    now = time.time()
    cutoff = now - window

    if agent_name not in bucket:
        bucket[agent_name] = []

    # Prune expired entries
    bucket[agent_name] = [ts for ts in bucket[agent_name] if ts > cutoff]

    if len(bucket[agent_name]) >= max_requests:
        relay_log('RATE_LIMITED', {
            'agent_name': agent_name,
            'blocked_action': action,
            'count': len(bucket[agent_name]),
            'max': max_requests,
            'window_seconds': window,
        })
        return False

    bucket[agent_name].append(now)
    return True


def _check_trade_rate_limit(agent_name: str, action: str):
    """Check trade rate limit. Raises HTTPException(429) if exceeded."""
    if agent_name == 'MsWednesday':
        return  # Apex authority not rate-limited
    if not _rate_limit_check(agent_name, _rate_limit_trades, RATE_LIMIT_TRADES_MAX, RATE_LIMIT_TRADES_WINDOW, action):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {RATE_LIMIT_TRADES_MAX} trades per {RATE_LIMIT_TRADES_WINDOW}s for '{agent_name}'"
        )


def _check_read_rate_limit(agent_name: str, action: str):
    """Check read rate limit. Raises HTTPException(429) if exceeded."""
    if agent_name == 'MsWednesday':
        return  # Apex authority not rate-limited
    if not _rate_limit_check(agent_name, _rate_limit_reads, RATE_LIMIT_READS_MAX, RATE_LIMIT_READS_WINDOW, action):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {RATE_LIMIT_READS_MAX} reads per {RATE_LIMIT_READS_WINDOW}s for '{agent_name}'"
        )

_spawn_secret: bytes = b''
try:
    if SPAWN_SECRET_PATH.exists():
        _spawn_secret = SPAWN_SECRET_PATH.read_text().strip().encode()
        print(f"[server] Spawn secret loaded ({len(_spawn_secret)} bytes)")
    else:
        print("[server] WARNING: .spawn_secret not found — gate enforcement disabled")
except Exception as e:
    print(f"[server] WARNING: Failed to load .spawn_secret: {e}")


def _verify_agent_gate(agent_name: str) -> bool:
    """
    Verify an agent has a valid HMAC-signed spawn gate.
    Returns True if authorized, False if not.
    MsWednesday is always exempt.
    Results cached for 60s, invalidated on gate file mtime change.
    """
    # MsWednesday is the spawn authority — always exempt
    if agent_name == 'MsWednesday':
        return True

    # If spawn secret not loaded, we can't verify — FAIL CLOSED
    if not _spawn_secret:
        relay_log('GATE_FAIL_CLOSED', {
            'agent_name': agent_name,
            'reason': 'spawn_secret_missing',
        })
        return False

    workspace = AGENT_GATE_WORKSPACES.get(agent_name)
    if not workspace:
        return False  # Unknown agent — not gated, but also not in whitelist

    gate_file = workspace / '.spawn_gate'

    # Check cache
    now = time.time()
    cached = _gate_cache.get(agent_name)
    if cached:
        valid_until, authorized, cached_mtime = cached
        if now < valid_until:
            # Check if gate file changed
            try:
                current_mtime = gate_file.stat().st_mtime if gate_file.exists() else 0
            except OSError:
                current_mtime = 0
            if current_mtime == cached_mtime:
                return authorized

    # Full verification
    gate_mtime = 0
    try:
        if not gate_file.exists():
            _gate_cache[agent_name] = (now + _GATE_CACHE_TTL, False, 0)
            return False

        gate_mtime = gate_file.stat().st_mtime

        with open(gate_file) as f:
            data = json.load(f)

        required = ['authorized_by', 'agent', 'timestamp', 'expires_at', 'signature']
        for field in required:
            if field not in data:
                _gate_cache[agent_name] = (now + _GATE_CACHE_TTL, False, gate_mtime)
                return False

        if data['authorized_by'] != 'MsWednesday':
            _gate_cache[agent_name] = (now + _GATE_CACHE_TTL, False, gate_mtime)
            return False

        expires = datetime.fromisoformat(data['expires_at'])
        if datetime.now() > expires:
            _gate_cache[agent_name] = (now + _GATE_CACHE_TTL, False, gate_mtime)
            return False

        message = f"{data['agent']}|{data['timestamp']}|{data['expires_at']}"
        expected_sig = hmac_mod.new(_spawn_secret, message.encode(), hashlib.sha256).hexdigest()
        authorized = hmac_mod.compare_digest(data['signature'], expected_sig)

        _gate_cache[agent_name] = (now + _GATE_CACHE_TTL, authorized, gate_mtime)
        return authorized

    except Exception as e:
        print(f"[gate] Error verifying {agent_name}: {e}")
        _gate_cache[agent_name] = (now + _GATE_CACHE_TTL, False, gate_mtime)
        return False


async def _gate_check_or_403(agent_name: str, action: str, requester: str = None):
    """
    Check spawn gate for agent. Raises 403 + notifies Brandon if unauthorized.
    MsWednesday is exempt — both as target agent AND as requester (apex authority).
    Called at the top of trade-executing endpoints.
    """
    if agent_name == 'MsWednesday':
        return  # Always allowed
    if requester == 'MsWednesday':
        return  # Apex authority can operate on any agent's wallet

    if not _verify_agent_gate(agent_name):
        relay_log(f'{action}_GATE_DENIED', {
            'agent_name': agent_name,
            'requester': requester or agent_name,
            'reason': 'no_valid_spawn_gate',
        })
        # Async notify Brandon
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                if AGENT_API_TOKEN:
                    await client.post(
                        f"{SXAN_API_BASE}/api/notify",
                        json={
                            'user_id': '6265463172',
                            'message': f"**GATE DENIED: {agent_name}**\n\nAttempted {action} without valid spawn gate.\nRequester: {requester or agent_name}\nTime: {datetime.now().isoformat()}",
                        },
                        headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
                    )
        except Exception as e:
            print(f"[gate] WARNING: Failed to notify Brandon about gate denial: {e}", file=sys.stderr)
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{agent_name}' has no valid spawn gate. Trade operations require MsWednesday authorization."
        )

# Position state file (written by vessel_monitor_service on this machine)
POSITION_STATE_FILE = Path.home() / 'position_state.json'

# Vessel state file (trade manager assignment, dynamic config)
VESSEL_STATE_FILE = Path(PROJECT_ROOT) / 'vessel_state.json'

# Agent availability state (1 agent = 1 position isolation model)
AGENT_AVAILABILITY_FILE = Path(PROJECT_ROOT) / 'agent_availability.json'

# Manager timeout: auto-release after 5 hours with no check-in
MANAGER_TIMEOUT_HOURS = 5

# --- Automated Capital Flow Constants ---
AGENT_GAS_SOL = 0.01       # SOL sent to agent for gas on entry
MIN_RETURNABLE = 0.002     # Minimum SOL worth auto-returning

# SXAN Dashboard (local to this machine — never exposed to phone directly)
SXAN_API_BASE = "http://localhost:5001"

# Agent API token for SXAN dashboard auth (read from bot .env)
AGENT_API_TOKEN = ""
try:
    env_path = Path.home() / 'Desktop' / 'Projects' / 'Sxan' / 'bot' / '.env'
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                if line.startswith('AGENT_API_TOKEN='):
                    AGENT_API_TOKEN = line.split('=', 1)[1].strip().strip('"').strip("'")
except Exception as e:
    print(f"[server] WARNING: Failed to load AGENT_API_TOKEN: {e}", file=sys.stderr)

# Relay audit log
RELAY_LOG = Path(PROJECT_ROOT) / 'relay_audit.log'

# Solana address pattern (base58, 32-44 chars)
SOLANA_ADDR_RE = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')

# Whitelisted agent names (only these agents can trade through the relay)
AGENT_WHITELIST = {"MsWednesday", "CP0", "CP1", "CP9", "msSunday", "msCounsel", "Chopper"}

# Agent context docs (Mac-side source of truth for agent identity)
AGENT_CONTEXTS_DIR = Path(PROJECT_ROOT) / 'agent_contexts'

# Local spawn (MCP proxy) — Claude Code CLI path and MCP server
CLAUDE_CLI_PATH = Path.home() / '.local' / 'bin' / 'claude'
MCP_SERVER_PATH = Path(PROJECT_ROOT) / 'vessel' / 'vessel_mcp_server.py'
MCP_PYTHON_PATH = Path(PROJECT_ROOT) / 'venv' / 'bin' / 'python3'

# Active agent sessions (relay-side tracking)
# session_id -> {agent_name, job_type, task_id, started_at, status, ...}
_agent_sessions: dict = {}

# Compliance audit log file (msCounsel decisions)
COMPLIANCE_AUDIT_PATH = Path(PROJECT_ROOT) / 'compliance_audit.json'

# MsWednesday wallet (for telegram feed proxy)
MSWEDNESDAY_WALLET = "J5G2Z5yTgprEiwKEr3NLpKLghAVksez8twitJJwfiYsh"


# --- Persistence (SQLite) ---

DB_PATH = os.path.join(PROJECT_ROOT, "vessel_tasks.db")

def init_db():
    """Initialize SQLite database for task persistence."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            vessel_id TEXT NOT NULL,
            task_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            priority INTEGER DEFAULT 0,
            timeout INTEGER DEFAULT 300,
            status TEXT DEFAULT 'queued',
            result TEXT,
            submitted_at REAL,
            completed_at REAL
        )
    """)
    conn.commit()
    conn.close()

def save_task(task_dict: dict):
    """Save task to persistent storage."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO tasks 
        (task_id, vessel_id, task_type, payload, priority, timeout, status, result, submitted_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        task_dict.get("task_id"),
        task_dict.get("vessel_id"),
        task_dict.get("task_type"),
        json.dumps(task_dict.get("payload", {})),
        task_dict.get("priority", 0),
        task_dict.get("timeout", 300),
        task_dict.get("status", "queued"),
        json.dumps(task_dict.get("result")) if task_dict.get("result") else None,
        task_dict.get("submitted_at"),
        task_dict.get("completed_at"),
    ))
    conn.commit()
    conn.close()

def load_task(task_id: str) -> dict:
    """Load task from persistent storage."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return None
    
    return {
        "task_id": row[0],
        "vessel_id": row[1],
        "task_type": row[2],
        "payload": json.loads(row[3]),
        "priority": row[4],
        "timeout": row[5],
        "status": row[6],
        "result": json.loads(row[7]) if row[7] else None,
        "submitted_at": row[8],
        "completed_at": row[9],
    }


# --- State ---

tasks = {}           # task_id -> task dict (in-memory cache)
vessels = {}         # vessel_id -> WebSocket connection
task_queue = {}      # vessel_id -> asyncio.Queue


# --- Models ---

class TaskSubmit(BaseModel):
    vessel_id: str = "phone-01"
    task_type: str = "execute"       # execute | shell | python | agent
    payload: dict                     # contents depend on task_type
    priority: int = 0
    timeout: int = 300


class TaskResponse(BaseModel):
    task_id: str
    status: str
    result: Optional[dict] = None


# --- Auth ---

def verify_token(token: str) -> bool:
    # Accept both "Bearer <token>" and raw "<token>"
    raw = token.removeprefix('Bearer ').strip() if token.startswith('Bearer ') else token
    return hashlib.sha256(raw.encode()).hexdigest() == hashlib.sha256(VESSEL_SECRET.encode()).hexdigest()


def get_requester(request: Request) -> Optional[str]:
    """Extract requester identity from X-Requester header. Validated against whitelist."""
    val = request.headers.get('x-requester', '')
    if val and val in AGENT_WHITELIST:
        return val
    return None


# Job types that are exempt from read-data isolation (can read other agents' data)
HEALTH_JOB_TYPES = {"health", "health_monitor"}


def _check_agent_authorization(requester: Optional[str], target_agent: str, action: str):
    """
    Verify the requester is authorized to act on the target agent's wallet.
    MsWednesday is exempt (apex authority).
    Raises HTTPException(403) if unauthorized.
    """
    if requester == 'MsWednesday':
        return  # Apex authority
    if requester and requester != target_agent:
        relay_log(f'{action}_CROSS_AGENT_DENIED', {
            'requester': requester,
            'target_agent': target_agent,
            'reason': 'cross_agent_not_allowed',
        })
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{requester}' cannot perform {action} on '{target_agent}' wallet"
        )


def _get_requester_job_type(requester: Optional[str]) -> Optional[str]:
    """Look up the active job_type for a requester from agent sessions."""
    if not requester:
        return None
    for session in _agent_sessions.values():
        if session.get("agent_name") == requester and session.get("status") == "running":
            return session.get("job_type")
    return None


def _check_read_authorization(requester: Optional[str], target_agent: str, action: str):
    """
    Verify the requester is authorized to read the target agent's data.
    MsWednesday exempt (apex authority).
    Health monitors exempt (need cross-agent visibility).
    Raises HTTPException(403) if unauthorized.
    """
    if requester == 'MsWednesday':
        return  # Apex authority
    if requester and requester != target_agent:
        # Check if requester is a health monitor
        job_type = _get_requester_job_type(requester)
        if job_type in HEALTH_JOB_TYPES:
            return  # Health monitors can read any agent's data
        relay_log(f'{action}_CROSS_AGENT_READ_DENIED', {
            'requester': requester,
            'target_agent': target_agent,
            'requester_job_type': job_type,
            'reason': 'cross_agent_read_not_allowed',
        })
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{requester}' cannot read '{target_agent}' data"
        )


def relay_log(action: str, details: dict):
    """Audit log for all relay operations. Every agent action is recorded."""
    entry = {
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'action': action,
        **details,
    }
    try:
        with open(RELAY_LOG, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except IOError as e:
        print(f"[relay] CRITICAL: Audit log write failed for {action}: {e}", file=sys.stderr)
    print(f"[relay] {action}: {json.dumps(details)}")


# --- Agent Availability State ---

def _read_availability() -> dict:
    """Read agent availability state. Returns default if file missing."""
    if not AGENT_AVAILABILITY_FILE.exists():
        return {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'agents': {
                'CP0': {'status': 'idle', 'position': None, 'assigned_at': None, 'type': None, 'last_checkin': None},
                'CP1': {'status': 'idle', 'position': None, 'assigned_at': None, 'type': None, 'last_checkin': None},
                'CP9': {'status': 'idle', 'position': None, 'assigned_at': None, 'type': None, 'last_checkin': None},
                'msSunday': {'status': 'idle', 'position': None, 'assigned_at': None, 'type': None, 'last_checkin': None},
                'msCounsel': {'status': 'idle', 'position': None, 'assigned_at': None, 'type': None, 'last_checkin': None},
                'Chopper': {'status': 'idle', 'position': None, 'assigned_at': None, 'type': None, 'last_checkin': None},
            },
        }
    try:
        with open(AGENT_AVAILABILITY_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {'timestamp': None, 'agents': {}}


def _write_availability(state: dict):
    """Atomic write agent availability state."""
    import tempfile
    state['timestamp'] = datetime.utcnow().isoformat() + 'Z'
    fd, tmp_path = tempfile.mkstemp(dir=PROJECT_ROOT, suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, AGENT_AVAILABILITY_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError as e:
            print(f"[server] WARNING: Failed to clean up temp file {tmp_path}: {e}", file=sys.stderr)
        raise


def _find_available_agent(state: dict) -> str:
    """Return first idle agent name, or None if all busy."""
    for agent_name, data in state.get('agents', {}).items():
        if data.get('status') == 'idle':
            return agent_name
    return None


def _check_manager_timeouts(state: dict) -> list:
    """Check for manager agents past timeout. Returns list of released agent names."""
    released = []
    now = datetime.utcnow()
    for agent_name, data in state.get('agents', {}).items():
        if data.get('type') != 'manager' or data.get('status') != 'busy':
            continue
        last_checkin = data.get('last_checkin')
        if not last_checkin:
            continue
        try:
            checkin_dt = datetime.fromisoformat(last_checkin.replace('Z', '+00:00').replace('+00:00', ''))
            elapsed_hours = (now - checkin_dt).total_seconds() / 3600
            if elapsed_hours > MANAGER_TIMEOUT_HOURS:
                data['status'] = 'idle'
                data['position'] = None
                data['assigned_at'] = None
                data['type'] = None
                data['last_checkin'] = None
                released.append(agent_name)
                relay_log('MANAGER_TIMEOUT', {'agent': agent_name, 'elapsed_hours': round(elapsed_hours, 1)})
        except (ValueError, TypeError) as e:
            print(f"[server] WARNING: Failed to parse checkin time for {agent_name}: {e}", file=sys.stderr)
    return released


# --- App ---

# Background task handles
_timeout_task = None
_session_watchdog_task = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _timeout_task, _session_watchdog_task
    print(f"[server] Vessel relay starting on {SERVER_HOST}:{SERVER_PORT}")
    init_db()
    print(f"[server] Task database initialized: {DB_PATH}")

    # Start background manager timeout checker
    _timeout_task = asyncio.create_task(_manager_timeout_loop())
    print("[server] Manager timeout checker started (5min interval)")

    # Start agent session watchdog
    _session_watchdog_task = asyncio.create_task(_session_watchdog_loop())
    print("[server] Agent session watchdog started (5min interval)")

    yield

    # Cleanup background tasks
    for task in [_timeout_task, _session_watchdog_task]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    print("[server] Shutting down")

app = FastAPI(title="VesselProject Relay", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


async def _manager_timeout_loop():
    """Background: check manager agent timeouts every 5 minutes."""
    while True:
        await asyncio.sleep(300)  # 5 minutes
        try:
            state = _read_availability()
            released = _check_manager_timeouts(state)
            if released:
                _write_availability(state)
                print(f"[server] Manager timeout: released {released}")
        except Exception as e:
            print(f"[server] Manager timeout check error: {e}")


async def _session_watchdog_loop():
    """Background: check agent session timeouts every 5 minutes."""
    while True:
        await asyncio.sleep(300)  # 5 minutes
        try:
            now = time.time()
            timed_out = []
            for session_id, session in list(_agent_sessions.items()):
                if session.get("status") != "running":
                    continue
                elapsed = now - session.get("started_at", now)
                if elapsed > AGENT_SESSION_TIMEOUT:
                    timed_out.append(session_id)

            for session_id in timed_out:
                session = _agent_sessions[session_id]
                agent_name = session.get("agent_name", "unknown")
                task_id = session.get("task_id")

                relay_log("SESSION_TIMEOUT", {
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "elapsed_hours": round((now - session["started_at"]) / 3600, 1),
                })

                # Kill local process or send cancel to phone
                if session.get("mode") == "local":
                    process = session.get("process")
                    if process and process.returncode is None:
                        try:
                            process.kill()
                        except Exception as e:
                            print(f"[watchdog] WARNING: Failed to kill local process for {session_id}: {e}", file=sys.stderr)
                elif task_id:
                    vessel_id = session.get("vessel_id", "phone-01")
                    ws = vessels.get(vessel_id)
                    if ws:
                        try:
                            await ws.send_json({"type": "cancel_task", "task_id": task_id})
                        except Exception as e:
                            print(f"[watchdog] WARNING: Failed to send cancel for {session_id}: {e}", file=sys.stderr)

                # Mark session as timed_out
                session["status"] = "timed_out"
                session["completed_at"] = now

                # Release agent
                await _auto_release_agent(agent_name)

                # Notify Brandon
                await _notify_brandon(
                    f"**Session Timeout**: {agent_name}\n"
                    f"Session {session_id} exceeded {AGENT_SESSION_TIMEOUT // 3600}h limit.\n"
                    f"Agent released to idle."
                )

            # Check for orphaned sessions (phone disconnected while agents running)
            # Local sessions don't need a vessel connection — skip them
            for session_id, session in list(_agent_sessions.items()):
                if session.get("status") != "running":
                    continue
                if session.get("mode") == "local":
                    continue  # Local sessions manage their own lifecycle
                vessel_id = session.get("vessel_id", "phone-01")
                if vessel_id not in vessels:
                    agent_name = session.get("agent_name", "unknown")
                    session["status"] = "orphaned"
                    session["completed_at"] = now

                    relay_log("SESSION_ORPHANED", {
                        "session_id": session_id,
                        "agent_name": agent_name,
                        "reason": "vessel_disconnected",
                    })

                    await _auto_release_agent(agent_name)
                    await _notify_brandon(
                        f"**Orphaned Session**: {agent_name}\n"
                        f"Phone disconnected while session {session_id} was running.\n"
                        f"Agent released to idle."
                    )

        except Exception as e:
            print(f"[server] Session watchdog error: {e}")


# --- Automated Capital Flow Helpers ---
# After a sell, auto-return SOL proceeds to MsWednesday.
# On final sell (no tokens left), return ALL SOL and release agent.

async def _get_agent_holdings(agent_name: str) -> dict:
    """Check agent wallet status (SOL balance + token holdings)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{SXAN_API_BASE}/api/agent-wallet/status/{agent_name}",
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )
        if resp.status_code == 200:
            return resp.json()
        return {'success': False, 'error': resp.text}
    except Exception as e:
        print(f"[capital-flow] Error checking holdings for {agent_name}: {e}")
        return {'success': False, 'error': str(e)}


async def _auto_return_sol(from_agent: str, to_agent: str, amount_sol: float = None):
    """
    Transfer SOL back to apex wallet.
    amount_sol=None → transfer all minus 0.005 buffer.
    """
    payload = {'to_agent': to_agent}
    if amount_sol is not None:
        payload['amount_sol'] = amount_sol

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SXAN_API_BASE}/api/agent-wallet/transfer-sol/{from_agent}",
                json=payload,
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )
        result = resp.json() if resp.status_code == 200 else {'success': False, 'error': resp.text}
        if result.get('success'):
            relay_log('AUTO_RETURN_SOL', {
                'from_agent': from_agent,
                'to_agent': to_agent,
                'amount_sol': amount_sol or 'ALL',
                'signature': result.get('signature', 'none'),
            })
        else:
            relay_log('AUTO_RETURN_SOL_FAILED', {
                'from_agent': from_agent,
                'to_agent': to_agent,
                'error': result.get('error', 'unknown'),
            })
        return result
    except Exception as e:
        relay_log('AUTO_RETURN_SOL_ERROR', {'from_agent': from_agent, 'error': str(e)})
        return {'success': False, 'error': str(e)}


async def _auto_release_agent(agent_name: str):
    """Release agent to idle after final sell (no tokens remaining)."""
    try:
        state = _read_availability()
        agents = state.get('agents', {})
        if agent_name in agents:
            agents[agent_name]['status'] = 'idle'
            agents[agent_name]['position'] = None
            agents[agent_name]['assigned_at'] = None
            agents[agent_name]['type'] = None
            agents[agent_name]['last_checkin'] = None
            _write_availability(state)
            relay_log('AUTO_RELEASE_AGENT', {'agent': agent_name, 'reason': 'no_tokens_remaining'})
            return True
    except Exception as e:
        relay_log('AUTO_RELEASE_ERROR', {'agent': agent_name, 'error': str(e)})
    return False


async def _notify_brandon(message: str):
    """Send notification to Brandon via SXAN dashboard."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{SXAN_API_BASE}/api/notify",
                json={'user_id': '6265463172', 'message': message},
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )
    except Exception as e:
        print(f"[relay] WARNING: _notify_brandon failed: {e}", file=sys.stderr)


DUST_GAS_THRESHOLD = 0.003  # SOL needed to execute a sell tx
DUST_USD_THRESHOLD = 0.50   # Tokens worth less than this are dust (write-off)


async def _get_token_usd_value(mint: str, ui_amount: float) -> float | None:
    """Price-check a token via DexScreener. Returns USD value or None if unavailable."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
        if resp.status_code != 200:
            return None
        pairs = resp.json().get('pairs') or []
        if not pairs:
            return None
        price_usd = float(pairs[0].get('priceUsd', 0))
        return ui_amount * price_usd
    except Exception as e:
        print(f"[capital-flow] DexScreener price check failed for {mint}: {e}")
        return None


async def _handle_post_sell_capital_flow(agent_name: str, sell_percent: int = 100):
    """
    Called after a successful sell. Handles automated capital return.

    Logic:
    - If agent still has meaningful tokens: return SOL above gas reserve (keep 0.01 for next sell)
    - If agent has no tokens (or only dust): return ALL SOL and auto-release agent
    - Dust detection:
      1) After 100% sell: remaining tokens are rounding artifacts → always dust
      2) Agent has no gas AND tokens worth < $0.50 → dust (write-off)
      3) Agent has no gas BUT tokens worth >= $0.50 → NOT dust, notify Brandon
      4) Can't price-check → fail safe: don't release, notify Brandon
    - MsWednesday exempt (she IS the apex wallet)
    """
    if agent_name == 'MsWednesday':
        return  # Apex wallet — no auto-return

    if not AGENT_API_TOKEN:
        return  # Can't make API calls without token

    # Check what agent still holds
    holdings = await _get_agent_holdings(agent_name)
    if not holdings.get('success'):
        print(f"[capital-flow] Could not check holdings for {agent_name}, skipping auto-return")
        return

    tokens = holdings.get('tokens', [])
    raw_has_tokens = any(t.get('ui_amount', 0) > 0 for t in tokens)
    sol_balance = holdings.get('sol_balance', 0)

    # Dust detection
    has_tokens = raw_has_tokens
    if raw_has_tokens:
        if sell_percent >= 100:
            # 100% sell — remaining is rounding artifacts
            print(f"[capital-flow] {agent_name}: 100% sell, remaining tokens are dust. Releasing.")
            has_tokens = False
        elif sol_balance < DUST_GAS_THRESHOLD:
            # No gas to sell — check if tokens are worth writing off
            total_usd = 0.0
            price_failed = False
            for t in tokens:
                if t.get('ui_amount', 0) > 0:
                    val = await _get_token_usd_value(t['mint'], t['ui_amount'])
                    if val is None:
                        price_failed = True
                        break
                    total_usd += val

            if price_failed:
                # Can't price → fail safe: don't release, notify Brandon
                print(f"[capital-flow] {agent_name}: can't price tokens, not releasing")
                await _notify_brandon(
                    f"**Stuck Agent**: {agent_name}\n"
                    f"Has tokens but no gas. Could not price-check.\n"
                    f"Manual review needed."
                )
            elif total_usd < DUST_USD_THRESHOLD:
                # Tokens worth < $0.50 — dust, write off
                print(f"[capital-flow] {agent_name}: tokens worth ${total_usd:.4f} "
                      f"(< ${DUST_USD_THRESHOLD}). Dust. Releasing.")
                has_tokens = False
            else:
                # Tokens worth real money but no gas — alert Brandon
                print(f"[capital-flow] {agent_name}: tokens worth ${total_usd:.2f} "
                      f"but no gas to sell. Alerting Brandon.")
                await _notify_brandon(
                    f"**Stuck Agent**: {agent_name}\n"
                    f"Tokens worth ~${total_usd:.2f} but only {sol_balance:.6f} SOL (no gas).\n"
                    f"Needs gas funding to sell, or manual intervention."
                )

    if has_tokens:
        # Partial sell — keep gas, return the rest
        returnable = sol_balance - AGENT_GAS_SOL - 0.002  # keep gas + tx fee buffer
        if returnable > MIN_RETURNABLE:
            result = await _auto_return_sol(agent_name, 'MsWednesday', amount_sol=round(returnable, 9))
            if result.get('success'):
                await _notify_brandon(
                    f"**Auto-Return (partial)**: {agent_name} → MsWednesday\n"
                    f"Returned: {returnable:.6f} SOL\n"
                    f"Agent keeps {AGENT_GAS_SOL} SOL gas + tokens"
                )
    else:
        # Final sell — no tokens left, return EVERYTHING and release
        if sol_balance > MIN_RETURNABLE:
            result = await _auto_return_sol(agent_name, 'MsWednesday')  # None = all minus buffer
            if result.get('success'):
                await _notify_brandon(
                    f"**Auto-Return (final)**: {agent_name} → MsWednesday\n"
                    f"All SOL returned. Position fully closed."
                )

        # Release agent to idle regardless of transfer success
        released = await _auto_release_agent(agent_name)
        if released:
            await _notify_brandon(
                f"**Agent Released**: {agent_name}\n"
                f"No tokens remaining. Agent returned to idle pool."
            )


# --- REST endpoints (for MsWednesday to submit tasks) ---

@app.post("/task", response_model=TaskResponse)
async def submit_task(task: TaskSubmit, authorization: str = Header()):
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    task_id = str(uuid.uuid4())
    task_dict = {
        "task_id": task_id,
        "vessel_id": task.vessel_id,
        "task_type": task.task_type,
        "payload": task.payload,
        "priority": task.priority,
        "timeout": task.timeout,
        "status": "queued",
        "submitted_at": time.time(),
        "result": None,
        "completed_at": None,
    }
    tasks[task_id] = task_dict

    # Save to persistent storage
    save_task(task_dict)

    # Queue it for the vessel
    if task.vessel_id not in task_queue:
        task_queue[task.vessel_id] = asyncio.Queue()
    await task_queue[task.vessel_id].put(task_dict)

    print(f"[server] Task {task_id} queued for vessel {task.vessel_id} ({task.task_type})")
    return TaskResponse(task_id=task_id, status="queued")


@app.get("/task/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, authorization: str = Header()):
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")
    
    # Check in-memory cache first
    if task_id not in tasks:
        # Try to load from persistent storage
        t = load_task(task_id)
        if not t:
            raise HTTPException(status_code=404, detail="Task not found")
        tasks[task_id] = t
    else:
        t = tasks[task_id]

    return TaskResponse(task_id=t["task_id"], status=t["status"], result=t["result"])


@app.get("/vessels")
async def list_vessels(authorization: str = Header()):
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")
    return {
        "vessels": [
            {"vessel_id": vid, "connected": True}
            for vid in vessels
        ]
    }


# --- Read-only data endpoint (for vessel display) ---
# This endpoint is STRICTLY READ-ONLY. It serves position monitoring data
# to the phone display. No write path exists through this endpoint.
# Auth required to prevent unauthenticated network access.

@app.get("/position-state")
async def get_position_state(authorization: str = Header()):
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    if not POSITION_STATE_FILE.exists():
        return JSONResponse(
            status_code=404,
            content={"error": "No position state available", "status": "waiting"}
        )

    try:
        with open(POSITION_STATE_FILE) as f:
            state = json.load(f)
        # Strip wallet_pubkey from response (not needed on phone, minimize exposure)
        state.pop('wallet_pubkey', None)
        return state
    except (json.JSONDecodeError, IOError) as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"State file read error: {str(e)}"}
        )


# --- Agent operation endpoints (proxied to SXAN API on localhost) ---
# These are the ONLY write paths from the vessel to the Mac.
# Each endpoint is specific, validated, auth'd, and audit-logged.
# Phone agents call these instead of the Mac's SXAN API directly.


class BuyRequest(BaseModel):
    token_mint: str
    amount_sol: float
    slippage_bps: int = 75
    agent_name: str = "MsWednesday"


class SellRequest(BaseModel):
    token_mint: str
    percent: float = 100.0
    slippage_bps: int = 75
    agent_name: str = "MsWednesday"


class TransferRequest(BaseModel):
    token_mint: str
    to_agent: str
    amount: Optional[float] = None
    percent: int = 100
    from_agent: str = "MsWednesday"


class NotifyRequest(BaseModel):
    title: str
    details: str
    tx_hash: Optional[str] = None


@app.post("/execute/sell")
async def relay_sell(req: SellRequest, request: Request, authorization: str = Header()):
    """
    Proxy sell command to SXAN wallet API.
    Validates input, logs the action, forwards to localhost:5001.
    Routes to the correct agent wallet based on agent_name.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    # Validate agent_name against whitelist
    if req.agent_name not in AGENT_WHITELIST:
        relay_log('SELL_REJECTED', {'reason': 'invalid_agent', 'agent_name': req.agent_name[:50]})
        raise HTTPException(status_code=403, detail=f"Agent '{req.agent_name}' not in whitelist")

    # Validate token_mint (Solana base58 address)
    if not SOLANA_ADDR_RE.match(req.token_mint):
        relay_log('SELL_REJECTED', {'reason': 'invalid_mint', 'agent': req.agent_name, 'mint': req.token_mint[:20]})
        raise HTTPException(status_code=400, detail="Invalid token mint address")

    # Validate percent (0-100)
    if not (0 < req.percent <= 100):
        relay_log('SELL_REJECTED', {'reason': 'invalid_percent', 'agent': req.agent_name, 'percent': req.percent})
        raise HTTPException(status_code=400, detail="Percent must be between 0 and 100")

    # Validate slippage (1-500 bps)
    if not (1 <= req.slippage_bps <= 500):
        relay_log('SELL_REJECTED', {'reason': 'invalid_slippage', 'agent': req.agent_name, 'slippage': req.slippage_bps})
        raise HTTPException(status_code=400, detail="Slippage must be between 1 and 500 bps")

    if not AGENT_API_TOKEN:
        relay_log('SELL_REJECTED', {'reason': 'no_agent_token', 'agent': req.agent_name})
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    # Rate limit check
    _check_trade_rate_limit(req.agent_name, 'SELL')

    # Per-agent authorization: requester can only sell from own wallet
    _check_agent_authorization(requester, req.agent_name, 'SELL')

    # Spawn gate check — agent must have valid HMAC gate to execute trades
    await _gate_check_or_403(req.agent_name, 'SELL', requester)

    relay_log('SELL_REQUESTED', {
        'agent_name': req.agent_name,
        'requester': requester or req.agent_name,
        'token_mint': req.token_mint,
        'percent': req.percent,
        'slippage_bps': req.slippage_bps,
    })

    # Forward to SXAN API (localhost — relay runs on the Mac)
    # Route to the correct agent wallet
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SXAN_API_BASE}/api/agent-wallet/sell/{req.agent_name}",
                json={
                    'token_mint': req.token_mint,
                    'percent': req.percent,
                    'slippage_bps': req.slippage_bps,
                },
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )

        result = resp.json() if resp.status_code == 200 else {'error': resp.text}

        relay_log('SELL_RESULT', {
            'agent_name': req.agent_name,
            'status_code': resp.status_code,
            'signature': result.get('signature', 'none'),
        })

        # Automated capital flow: return SOL proceeds after successful sell
        if resp.status_code == 200 and result.get('success'):
            asyncio.create_task(_handle_post_sell_capital_flow(req.agent_name, req.percent))

        if resp.status_code == 200:
            return result
        else:
            return JSONResponse(status_code=resp.status_code, content=result)

    except Exception as e:
        relay_log('SELL_ERROR', {'agent_name': req.agent_name, 'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'SXAN API unreachable: {str(e)}'})


@app.post("/execute/buy")
async def relay_buy(req: BuyRequest, request: Request, authorization: str = Header()):
    """
    Proxy buy command to SXAN wallet API.
    Validates input, logs the action, forwards to localhost:5001.
    Routes to the correct agent wallet based on agent_name.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    # Validate agent_name against whitelist
    if req.agent_name not in AGENT_WHITELIST:
        relay_log('BUY_REJECTED', {'reason': 'invalid_agent', 'agent_name': req.agent_name[:50]})
        raise HTTPException(status_code=403, detail=f"Agent '{req.agent_name}' not in whitelist")

    # Validate token_mint (Solana base58 address)
    if not SOLANA_ADDR_RE.match(req.token_mint):
        relay_log('BUY_REJECTED', {'reason': 'invalid_mint', 'agent': req.agent_name, 'mint': req.token_mint[:20]})
        raise HTTPException(status_code=400, detail="Invalid token mint address")

    # Validate amount_sol (0 < amount <= 1.0 SOL)
    if not (0 < req.amount_sol <= 1.0):
        relay_log('BUY_REJECTED', {'reason': 'invalid_amount', 'agent': req.agent_name, 'amount': req.amount_sol})
        raise HTTPException(status_code=400, detail="amount_sol must be between 0 and 1.0 SOL")

    # Validate slippage (1-500 bps)
    if not (1 <= req.slippage_bps <= 500):
        relay_log('BUY_REJECTED', {'reason': 'invalid_slippage', 'agent': req.agent_name, 'slippage': req.slippage_bps})
        raise HTTPException(status_code=400, detail="Slippage must be between 1 and 500 bps")

    if not AGENT_API_TOKEN:
        relay_log('BUY_REJECTED', {'reason': 'no_agent_token', 'agent': req.agent_name})
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    # Rate limit check
    _check_trade_rate_limit(req.agent_name, 'BUY')

    # Per-agent authorization: requester can only buy from own wallet
    _check_agent_authorization(requester, req.agent_name, 'BUY')

    # Spawn gate check — agent must have valid HMAC gate to execute trades
    await _gate_check_or_403(req.agent_name, 'BUY', requester)

    relay_log('BUY_REQUESTED', {
        'agent_name': req.agent_name,
        'requester': requester or req.agent_name,
        'token_mint': req.token_mint,
        'amount_sol': req.amount_sol,
        'slippage_bps': req.slippage_bps,
    })

    # Forward to SXAN API (localhost — relay runs on the Mac)
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"{SXAN_API_BASE}/api/agent-wallet/buy/{req.agent_name}",
                json={
                    'token_mint': req.token_mint,
                    'amount_sol': req.amount_sol,
                    'slippage_bps': req.slippage_bps,
                },
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )

        result = resp.json() if resp.status_code == 200 else {'error': resp.text}

        relay_log('BUY_RESULT', {
            'agent_name': req.agent_name,
            'status_code': resp.status_code,
            'signature': result.get('signature', 'none'),
        })

        if resp.status_code == 200:
            return result
        else:
            return JSONResponse(status_code=resp.status_code, content=result)

    except Exception as e:
        relay_log('BUY_ERROR', {'agent_name': req.agent_name, 'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'SXAN API unreachable: {str(e)}'})


@app.post("/execute/transfer")
async def relay_transfer(req: TransferRequest, request: Request, authorization: str = Header()):
    """
    Proxy transfer command to SXAN wallet API.
    Transfers SPL tokens from one agent wallet to another.
    Used for transfer-on-entry model.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    # Validate both agents against whitelist
    if req.from_agent not in AGENT_WHITELIST:
        relay_log('TRANSFER_REJECTED', {'reason': 'invalid_from_agent', 'from_agent': req.from_agent[:50]})
        raise HTTPException(status_code=403, detail=f"Agent '{req.from_agent}' not in whitelist")

    if req.to_agent not in AGENT_WHITELIST:
        relay_log('TRANSFER_REJECTED', {'reason': 'invalid_to_agent', 'to_agent': req.to_agent[:50]})
        raise HTTPException(status_code=403, detail=f"Agent '{req.to_agent}' not in whitelist")

    # Validate token_mint (Solana base58 address)
    if not SOLANA_ADDR_RE.match(req.token_mint):
        relay_log('TRANSFER_REJECTED', {'reason': 'invalid_mint', 'from_agent': req.from_agent, 'mint': req.token_mint[:20]})
        raise HTTPException(status_code=400, detail="Invalid token mint address")

    # Validate percent (1-100)
    if not (1 <= req.percent <= 100):
        relay_log('TRANSFER_REJECTED', {'reason': 'invalid_percent', 'from_agent': req.from_agent, 'percent': req.percent})
        raise HTTPException(status_code=400, detail="Percent must be between 1 and 100")

    # Validate amount if provided
    if req.amount is not None and req.amount <= 0:
        relay_log('TRANSFER_REJECTED', {'reason': 'invalid_amount', 'from_agent': req.from_agent, 'amount': req.amount})
        raise HTTPException(status_code=400, detail="Amount must be positive")

    if not AGENT_API_TOKEN:
        relay_log('TRANSFER_REJECTED', {'reason': 'no_agent_token', 'from_agent': req.from_agent})
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    # Rate limit check
    _check_trade_rate_limit(req.from_agent, 'TRANSFER')

    # Per-agent authorization: requester can only transfer from own wallet
    _check_agent_authorization(requester, req.from_agent, 'TRANSFER')

    # Spawn gate check — agent initiating transfer must have valid gate
    await _gate_check_or_403(req.from_agent, 'TRANSFER', requester)

    relay_log('TRANSFER_REQUESTED', {
        'from_agent': req.from_agent,
        'to_agent': req.to_agent,
        'requester': requester or req.from_agent,
        'token_mint': req.token_mint,
        'percent': req.percent,
        'amount': req.amount,
    })

    # Build request payload
    payload = {
        'to_agent': req.to_agent,
        'token_mint': req.token_mint,
        'percent': req.percent,
    }
    if req.amount is not None:
        payload['amount'] = req.amount

    # Forward to SXAN API (localhost — relay runs on the Mac)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{SXAN_API_BASE}/api/agent-wallet/transfer/{req.from_agent}",
                json=payload,
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )

        result = resp.json() if resp.status_code == 200 else {'error': resp.text}

        relay_log('TRANSFER_RESULT', {
            'from_agent': req.from_agent,
            'to_agent': req.to_agent,
            'status_code': resp.status_code,
            'signature': result.get('signature', 'none'),
        })

        if resp.status_code == 200:
            return result
        else:
            return JSONResponse(status_code=resp.status_code, content=result)

    except Exception as e:
        relay_log('TRANSFER_ERROR', {'from_agent': req.from_agent, 'to_agent': req.to_agent, 'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'SXAN API unreachable: {str(e)}'})


@app.get("/wallet-status/{agent_name}")
async def relay_wallet_status(agent_name: str, request: Request, authorization: str = Header()):
    """
    Proxy wallet status request to SXAN wallet API.
    Returns pubkey, sol_balance, tokens, enabled status.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    if agent_name not in AGENT_WHITELIST:
        relay_log('WALLET_STATUS_REJECTED', {'reason': 'invalid_agent', 'agent_name': agent_name[:50]})
        raise HTTPException(status_code=403, detail=f"Agent '{agent_name}' not in whitelist")

    # Rate limit check
    _check_read_rate_limit(requester or agent_name, 'WALLET_STATUS')

    # Per-agent read isolation: agents can only query own wallet
    _check_read_authorization(requester, agent_name, 'WALLET_STATUS')

    if not AGENT_API_TOKEN:
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    relay_log('WALLET_STATUS', {'agent_name': agent_name, 'requester': requester or agent_name})

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{SXAN_API_BASE}/api/agent-wallet/status/{agent_name}",
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )

        if resp.status_code == 200:
            return resp.json()
        else:
            return JSONResponse(status_code=resp.status_code, content={'error': resp.text})

    except Exception as e:
        relay_log('WALLET_STATUS_ERROR', {'agent_name': agent_name, 'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'SXAN API unreachable: {str(e)}'})


@app.get("/transactions/{agent_name}")
async def relay_transactions(agent_name: str, request: Request, authorization: str = Header(), limit: int = 20):
    """
    Proxy transaction history request to SXAN wallet API.
    Returns recent trades for the specified agent.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    if agent_name not in AGENT_WHITELIST:
        relay_log('TRANSACTIONS_REJECTED', {'reason': 'invalid_agent', 'agent_name': agent_name[:50]})
        raise HTTPException(status_code=403, detail=f"Agent '{agent_name}' not in whitelist")

    # Rate limit check
    _check_read_rate_limit(requester or agent_name, 'TRANSACTIONS')

    # Per-agent read isolation: agents can only query own transactions
    _check_read_authorization(requester, agent_name, 'TRANSACTIONS')

    if not AGENT_API_TOKEN:
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    limit = max(1, min(limit, 100))

    relay_log('TRANSACTIONS', {'agent_name': agent_name, 'requester': requester or agent_name, 'limit': limit})

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{SXAN_API_BASE}/api/agent-wallet/transactions/{agent_name}",
                params={'limit': limit},
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )

        if resp.status_code == 200:
            return resp.json()
        else:
            return JSONResponse(status_code=resp.status_code, content={'error': resp.text})

    except Exception as e:
        relay_log('TRANSACTIONS_ERROR', {'agent_name': agent_name, 'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'SXAN API unreachable: {str(e)}'})


@app.get("/positions/{agent_name}")
async def relay_positions(agent_name: str, request: Request, authorization: str = Header()):
    """
    Return positions filtered for a specific agent from position_state.json.
    Reads from local file (same machine), no proxy needed.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    if agent_name not in AGENT_WHITELIST:
        relay_log('POSITIONS_REJECTED', {'reason': 'invalid_agent', 'agent_name': agent_name[:50]})
        raise HTTPException(status_code=403, detail=f"Agent '{agent_name}' not in whitelist")

    # Rate limit check
    _check_read_rate_limit(requester or agent_name, 'POSITIONS')

    # Per-agent read isolation: agents can only query own positions
    _check_read_authorization(requester, agent_name, 'POSITIONS')

    relay_log('POSITIONS', {'agent_name': agent_name, 'requester': requester or agent_name})

    if not POSITION_STATE_FILE.exists():
        return JSONResponse(content={
            'positions': [],
            'sol_balance': 0,
            'timestamp': None,
            'status': 'no_data',
        })

    try:
        with open(POSITION_STATE_FILE) as f:
            state = json.load(f)

        all_positions = state.get('positions', [])
        agent_positions = [p for p in all_positions if p.get('agent') == agent_name]

        return {
            'positions': agent_positions,
            'total': len(agent_positions),
            'sol_balance': state.get('sol_balance', 0),
            'timestamp': state.get('timestamp'),
            'status': 'ok',
        }
    except (json.JSONDecodeError, IOError) as e:
        relay_log('POSITIONS_ERROR', {'agent_name': agent_name, 'error': str(e)})
        return JSONResponse(
            status_code=500,
            content={'error': f'Position state read error: {str(e)}'}
        )


@app.post("/notify")
async def relay_notify(req: NotifyRequest, request: Request, authorization: str = Header()):
    """
    Proxy notification to SXAN dashboard (Telegram alert to Brandon).
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    # Sanitize: limit lengths
    title = req.title[:100]
    details = req.details[:500]

    relay_log('NOTIFY_REQUESTED', {'title': title, 'requester': requester})

    message = f"**{title}**\n\n{details}"
    if req.tx_hash:
        message += f"\n\nTX: {req.tx_hash[:88]}"

    if not AGENT_API_TOKEN:
        relay_log('NOTIFY_REJECTED', {'reason': 'no_agent_token'})
        return JSONResponse(status_code=500, content={'error': 'AGENT_API_TOKEN not configured on relay'})

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{SXAN_API_BASE}/api/notify",
                json={'user_id': '6265463172', 'message': message},
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )

        relay_log('NOTIFY_RESULT', {'status_code': resp.status_code})
        return {'status': 'sent' if resp.status_code == 200 else 'error'}

    except Exception as e:
        relay_log('NOTIFY_ERROR', {'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'Notification failed: {str(e)}'})


# --- Read-only feed proxy endpoints (for vessel agents to access market data) ---
# All feeds proxy to SXAN dashboard APIs on localhost. Read-only, auth required.

@app.get("/feeds/telegram")
async def feed_telegram(request: Request, authorization: str = Header(), limit: int = 50):
    """
    Proxy Telegram token feed from SXAN dashboard.
    Returns tokens extracted from monitored Telegram chats.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    if not AGENT_API_TOKEN:
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    # Clamp limit to sane range
    limit = max(1, min(limit, 200))

    relay_log('FEED_TELEGRAM', {'limit': limit, 'requester': requester})

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{SXAN_API_BASE}/api/telegram/feed",
                params={'wallet': MSWEDNESDAY_WALLET, 'limit': limit},
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )

        if resp.status_code == 200:
            return resp.json()
        else:
            return JSONResponse(status_code=resp.status_code, content={'error': resp.text})

    except Exception as e:
        relay_log('FEED_TELEGRAM_ERROR', {'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'SXAN API unreachable: {str(e)}'})


@app.get("/feeds/graduating")
async def feed_graduating(request: Request, authorization: str = Header(), limit: int = 30):
    """
    Proxy almost-graduated tokens feed from SXAN swarm API.
    Returns tokens approaching graduation with progress %.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    if not AGENT_API_TOKEN:
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    limit = max(1, min(limit, 100))

    relay_log('FEED_GRADUATING', {'limit': limit, 'requester': requester})

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{SXAN_API_BASE}/api/swarm/graduating",
                params={'limit': limit},
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )

        if resp.status_code == 200:
            return resp.json()
        else:
            return JSONResponse(status_code=resp.status_code, content={'error': resp.text})

    except Exception as e:
        relay_log('FEED_GRADUATING_ERROR', {'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'SXAN API unreachable: {str(e)}'})


@app.get("/activity")
async def get_activity(authorization: str = Header(), limit: int = 5):
    """
    Return recent relay audit log entries.
    Tails relay_audit.log and returns last N parsed JSON lines.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    limit = max(1, min(limit, 50))

    if not RELAY_LOG.exists():
        return []

    try:
        with open(RELAY_LOG, 'r') as f:
            all_lines = f.readlines()

        entries = []
        for line in all_lines[-(limit * 2):]:  # read extra in case some fail to parse
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        return entries[-limit:]
    except IOError:
        return []


# --- Vessel State (trade manager assignment) ---

@app.get("/trade-manager")
async def get_trade_manager(authorization: str = Header()):
    """
    Get current trade manager assignment.
    Returns who receives positions after MsWednesday buys.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    if not VESSEL_STATE_FILE.exists():
        return {'trade_manager': None, 'error': 'No vessel state configured'}

    try:
        with open(VESSEL_STATE_FILE) as f:
            state = json.load(f)
        return {
            'trade_manager': state.get('trade_manager'),
            'updated_at': state.get('updated_at'),
            'updated_by': state.get('updated_by'),
        }
    except (json.JSONDecodeError, IOError) as e:
        return JSONResponse(status_code=500, content={'error': str(e)})


class SetTradeManagerRequest(BaseModel):
    agent_name: str


@app.post("/trade-manager")
async def set_trade_manager(req: SetTradeManagerRequest, request: Request, authorization: str = Header()):
    """
    Set current trade manager.
    Only whitelisted agents can be assigned as trade manager.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    if req.agent_name not in AGENT_WHITELIST:
        relay_log('SET_TRADE_MANAGER_REJECTED', {'reason': 'invalid_agent', 'agent_name': req.agent_name[:50]})
        raise HTTPException(status_code=403, detail=f"Agent '{req.agent_name}' not in whitelist")

    # Read existing state
    state = {}
    if VESSEL_STATE_FILE.exists():
        try:
            with open(VESSEL_STATE_FILE) as f:
                state = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[server] WARNING: Failed to read vessel state: {e}", file=sys.stderr)

    old_manager = state.get('trade_manager')
    state['trade_manager'] = req.agent_name
    state['updated_at'] = datetime.utcnow().isoformat() + 'Z'
    state['updated_by'] = requester or 'unknown'

    # Atomic write
    import tempfile
    try:
        fd, tmp_path = tempfile.mkstemp(dir=PROJECT_ROOT, suffix='.json')
        with os.fdopen(fd, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, VESSEL_STATE_FILE)
    except Exception as e:
        relay_log('SET_TRADE_MANAGER_ERROR', {'error': str(e)})
        return JSONResponse(status_code=500, content={'error': str(e)})

    relay_log('TRADE_MANAGER_CHANGED', {
        'old_manager': old_manager,
        'new_manager': req.agent_name,
        'requester': requester,
    })

    return {
        'success': True,
        'trade_manager': req.agent_name,
        'previous': old_manager,
    }


# --- Agent Availability (Multi-Position Isolation Model) ---
# 1 agent = 1 position. Agents tracked as idle/busy.
# Trader agents manage positions, manager agents do monitoring.
# Manager agents auto-release after 5h no-checkin.


class AssignRequest(BaseModel):
    agent_name: str
    token_mint: str
    agent_type: str = "trader"  # "trader" or "manager"


class ReleaseRequest(BaseModel):
    agent_name: str


class CheckinRequest(BaseModel):
    agent_name: str


class TransferSolRequest(BaseModel):
    from_agent: str
    to_agent: str
    amount_sol: Optional[float] = None  # None = transfer all minus buffer


@app.get("/agents/availability")
async def get_agents_availability(authorization: str = Header()):
    """Get agent availability state. Shows who is idle vs busy."""
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    state = _read_availability()
    return state


@app.post("/agents/assign")
async def assign_agent(req: AssignRequest, request: Request, authorization: str = Header()):
    """
    DEPRECATED — Use POST /agents/spawn instead.

    /agents/spawn creates a session with automatic lifecycle management:
    agents are marked busy on spawn and auto-released when the session
    ends, times out, or is killed. This prevents agents from getting
    stuck as "busy" when callers forget to release them.
    """
    relay_log('ASSIGN_DEPRECATED', {
        'agent': req.agent_name,
        'requester': get_requester(request),
    })
    return JSONResponse(status_code=410, content={
        'success': False,
        'error': 'DEPRECATED: /agents/assign is removed. Use POST /agents/spawn instead — it handles busy/idle lifecycle automatically.',
    })


@app.post("/agents/release")
async def release_agent(req: ReleaseRequest, request: Request, authorization: str = Header()):
    """
    Release an agent from their assignment. Marks them as idle.
    Emergency/manual override only — normal lifecycle is handled by
    /agents/spawn sessions which auto-release on end/kill/timeout.
    Does NOT auto-transfer SOL — caller handles that separately.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    if req.agent_name not in AGENT_WHITELIST:
        relay_log('RELEASE_REJECTED', {'reason': 'invalid_agent', 'agent_name': req.agent_name[:50]})
        raise HTTPException(status_code=403, detail=f"Agent '{req.agent_name}' not in whitelist")

    state = _read_availability()
    agents = state.get('agents', {})

    if req.agent_name not in agents:
        return JSONResponse(status_code=404, content={'success': False, 'error': 'Agent not tracked'})

    agent = agents[req.agent_name]
    old_position = agent.get('position')
    old_type = agent.get('type')

    agent['status'] = 'idle'
    agent['position'] = None
    agent['assigned_at'] = None
    agent['type'] = None
    agent['last_checkin'] = None

    _write_availability(state)

    relay_log('AGENT_RELEASED', {
        'agent': req.agent_name,
        'old_position': old_position,
        'old_type': old_type,
        'requester': requester,
    })

    return {
        'success': True,
        'agent_name': req.agent_name,
        'status': 'idle',
        'released_from': old_position,
        'previous_type': old_type,
    }


@app.post("/agents/checkin")
async def agent_checkin(req: CheckinRequest, request: Request, authorization: str = Header()):
    """
    Manager agent heartbeat. Resets the timeout clock.
    Only meaningful for agents with type='manager'.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    if req.agent_name not in AGENT_WHITELIST:
        raise HTTPException(status_code=403, detail=f"Agent '{req.agent_name}' not in whitelist")

    state = _read_availability()
    agents = state.get('agents', {})

    if req.agent_name not in agents:
        return JSONResponse(status_code=404, content={'success': False, 'error': 'Agent not tracked'})

    agent = agents[req.agent_name]
    if agent.get('type') != 'manager':
        return JSONResponse(status_code=400, content={
            'success': False,
            'error': f"Agent '{req.agent_name}' is not a manager (type: {agent.get('type')})",
        })

    now = datetime.utcnow().isoformat() + 'Z'
    agent['last_checkin'] = now

    _write_availability(state)

    relay_log('MANAGER_CHECKIN', {'agent': req.agent_name, 'requester': requester})

    return {'success': True, 'agent_name': req.agent_name, 'last_checkin': now}


@app.post("/execute/transfer-sol")
async def relay_transfer_sol(req: TransferSolRequest, request: Request, authorization: str = Header()):
    """
    Proxy SOL transfer between agent wallets.
    Used for capital return: trader sells → SOL goes back to MsWednesday.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    if req.from_agent not in AGENT_WHITELIST:
        relay_log('TRANSFER_SOL_REJECTED', {'reason': 'invalid_from_agent', 'from_agent': req.from_agent[:50]})
        raise HTTPException(status_code=403, detail=f"Agent '{req.from_agent}' not in whitelist")

    if req.to_agent not in AGENT_WHITELIST:
        relay_log('TRANSFER_SOL_REJECTED', {'reason': 'invalid_to_agent', 'to_agent': req.to_agent[:50]})
        raise HTTPException(status_code=403, detail=f"Agent '{req.to_agent}' not in whitelist")

    if req.amount_sol is not None and req.amount_sol <= 0:
        raise HTTPException(status_code=400, detail="amount_sol must be positive")

    if not AGENT_API_TOKEN:
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    # Rate limit check
    _check_trade_rate_limit(req.from_agent, 'TRANSFER_SOL')

    # Per-agent authorization: requester can only transfer SOL from own wallet
    _check_agent_authorization(requester, req.from_agent, 'TRANSFER_SOL')

    # Spawn gate check — agent initiating SOL transfer must have valid gate
    await _gate_check_or_403(req.from_agent, 'TRANSFER_SOL', requester)

    relay_log('TRANSFER_SOL_REQUESTED', {
        'from_agent': req.from_agent,
        'to_agent': req.to_agent,
        'amount_sol': req.amount_sol,
        'requester': requester or req.from_agent,
    })

    payload = {'to_agent': req.to_agent}
    if req.amount_sol is not None:
        payload['amount_sol'] = req.amount_sol

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{SXAN_API_BASE}/api/agent-wallet/transfer-sol/{req.from_agent}",
                json=payload,
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )

        result = resp.json() if resp.status_code == 200 else {'error': resp.text}

        relay_log('TRANSFER_SOL_RESULT', {
            'from_agent': req.from_agent,
            'to_agent': req.to_agent,
            'status_code': resp.status_code,
            'signature': result.get('signature', 'none'),
        })

        if resp.status_code == 200:
            return result
        else:
            return JSONResponse(status_code=resp.status_code, content=result)

    except Exception as e:
        relay_log('TRANSFER_SOL_ERROR', {'from_agent': req.from_agent, 'to_agent': req.to_agent, 'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'SXAN API unreachable: {str(e)}'})


@app.get("/feeds/launches")
async def feed_launches(request: Request, authorization: str = Header(), limit: int = 30):
    """
    Proxy new token launches feed from SXAN swarm API.
    Returns recently launched pump.fun tokens.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    if not AGENT_API_TOKEN:
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    limit = max(1, min(limit, 100))

    relay_log('FEED_LAUNCHES', {'limit': limit, 'requester': requester})

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{SXAN_API_BASE}/api/swarm/launches",
                params={'limit': limit},
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )

        if resp.status_code == 200:
            return resp.json()
        else:
            return JSONResponse(status_code=resp.status_code, content={'error': resp.text})

    except Exception as e:
        relay_log('FEED_LAUNCHES_ERROR', {'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'SXAN API unreachable: {str(e)}'})


# Catalyst state file (written by vessel_catalyst_aggregator on this machine)
CATALYST_STATE_FILE = Path.home() / 'catalyst_events.json'


@app.get("/feeds/catalysts")
async def feed_catalysts(request: Request, authorization: str = Header(), limit: int = 20, min_score: float = 0):
    """
    Serve catalyst events from local state file.
    Written by vessel_catalyst_aggregator.py, read here directly (same machine).
    Returns trending events from Google Trends, News RSS, Reddit.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    limit = max(1, min(limit, 50))
    min_score = max(0, min(min_score, 100))

    relay_log('FEED_CATALYSTS', {'limit': limit, 'min_score': min_score, 'requester': requester})

    if not CATALYST_STATE_FILE.exists():
        return JSONResponse(content={
            'events': [],
            'total': 0,
            'timestamp': None,
            'status': 'no_data',
        })

    try:
        with open(CATALYST_STATE_FILE) as f:
            data = json.load(f)

        events = data.get('events', [])
        if min_score > 0:
            events = [e for e in events if e.get('trend_score', 0) >= min_score]

        return {
            'events': events[:limit],
            'total': len(events),
            'timestamp': data.get('timestamp'),
            'status': 'ok',
        }
    except (json.JSONDecodeError, IOError) as e:
        relay_log('FEED_CATALYSTS_ERROR', {'error': str(e)})
        return JSONResponse(
            status_code=500,
            content={'error': f'Catalyst state read error: {str(e)}'}
        )


# --- Content Pipeline Proxy (read/write content, no spawn gate needed) ---


class ContentScanRequest(BaseModel):
    days_back: int = 7


class ContentSubmitRequest(BaseModel):
    lesson_id: str
    content: str
    platform: str = "twitter"
    author_agent: str = "unknown"


@app.post("/content/scan")
async def relay_content_scan(req: ContentScanRequest, authorization: str = Header()):
    """Proxy content scan to SXAN dashboard."""
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    if not AGENT_API_TOKEN:
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    relay_log('CONTENT_SCAN', {'days_back': req.days_back})

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SXAN_API_BASE}/api/content/scan",
                json={'days_back': req.days_back},
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )
        if resp.status_code == 200:
            return resp.json()
        return JSONResponse(status_code=resp.status_code, content={'error': resp.text})
    except Exception as e:
        relay_log('CONTENT_SCAN_ERROR', {'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'SXAN API unreachable: {str(e)}'})


@app.get("/content/lessons")
async def relay_content_lessons(authorization: str = Header(), category: str = None, limit: int = 50):
    """Proxy content lessons list from SXAN dashboard."""
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    if not AGENT_API_TOKEN:
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    limit = max(1, min(limit, 200))

    params = {'limit': limit}
    if category:
        params['category'] = category

    relay_log('CONTENT_LESSONS', {'category': category, 'limit': limit})

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{SXAN_API_BASE}/api/content/lessons",
                params=params,
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )
        if resp.status_code == 200:
            return resp.json()
        return JSONResponse(status_code=resp.status_code, content={'error': resp.text})
    except Exception as e:
        relay_log('CONTENT_LESSONS_ERROR', {'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'SXAN API unreachable: {str(e)}'})


@app.post("/content/submit")
async def relay_content_submit(req: ContentSubmitRequest, request: Request, authorization: str = Header()):
    """Proxy draft submission to SXAN dashboard."""
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    if not AGENT_API_TOKEN:
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    if len(req.content) > 2000:
        raise HTTPException(status_code=400, detail="Content too long (max 2000 chars)")

    relay_log('CONTENT_SUBMIT', {
        'lesson_id': req.lesson_id,
        'platform': req.platform,
        'author': req.author_agent,
        'requester': requester,
    })

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{SXAN_API_BASE}/api/content/drafts",
                json={
                    'lesson_id': req.lesson_id,
                    'content': req.content,
                    'platform': req.platform,
                    'author_agent': req.author_agent,
                },
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )
        if resp.status_code == 200:
            return resp.json()
        return JSONResponse(status_code=resp.status_code, content={'error': resp.text})
    except Exception as e:
        relay_log('CONTENT_SUBMIT_ERROR', {'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'SXAN API unreachable: {str(e)}'})


@app.get("/content/queue")
async def relay_content_queue(authorization: str = Header()):
    """Proxy content queue from SXAN dashboard."""
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    if not AGENT_API_TOKEN:
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    relay_log('CONTENT_QUEUE', {})

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{SXAN_API_BASE}/api/content/queue",
                headers={'Authorization': f'Bearer {AGENT_API_TOKEN}'},
            )
        if resp.status_code == 200:
            return resp.json()
        return JSONResponse(status_code=resp.status_code, content={'error': resp.text})
    except Exception as e:
        relay_log('CONTENT_QUEUE_ERROR', {'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'SXAN API unreachable: {str(e)}'})


# --- Agent Spawn & Session Management ---


class SpawnRequest(BaseModel):
    agent_name: str
    job_type: str = "general"   # scanner, trader, manager, health, content_manager, compliance_counsel, general
    prompt: str
    token_mint: Optional[str] = None
    max_turns: int = AGENT_MAX_TURNS
    mode: str = "oneshot"       # "oneshot", "continuous", or "local" (Mac-side Claude Code)
    vessel_id: str = "phone-01"
    max_budget_usd: float = 1.0  # Budget cap for local mode (per spawn)


@app.post("/agents/spawn")
async def spawn_agent(req: SpawnRequest, request: Request, authorization: str = Header()):
    """
    Spawn an agent via phone vessel or locally via Claude Code CLI (MCP proxy).

    Modes:
    - "oneshot" / "continuous": Dispatch to phone vessel (existing behavior)
    - "local": Run Claude Code CLI on Mac with vessel_tools via MCP
      (uses Claude subscription instead of API credits)

    Only MsWednesday can spawn agents.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    # Only MsWednesday can spawn agents
    if requester != "MsWednesday":
        relay_log("SPAWN_DENIED", {
            "agent_name": req.agent_name,
            "requester": requester,
            "reason": "not_spawn_authority",
        })
        raise HTTPException(
            status_code=403,
            detail="Only MsWednesday can spawn agents"
        )

    # Validate agent name
    if req.agent_name not in AGENT_WHITELIST or req.agent_name == "MsWednesday":
        relay_log("SPAWN_DENIED", {
            "agent_name": req.agent_name,
            "reason": "invalid_agent",
        })
        raise HTTPException(status_code=400, detail=f"Cannot spawn '{req.agent_name}'")

    # Gate check
    if not _verify_agent_gate(req.agent_name):
        relay_log("SPAWN_GATE_DENIED", {
            "agent_name": req.agent_name,
            "requester": requester,
        })
        await _notify_brandon(
            f"**SPAWN GATE DENIED**: {req.agent_name}\n"
            f"MsWednesday tried to spawn without valid gate."
        )
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{req.agent_name}' has no valid spawn gate"
        )

    # Check agent isn't already busy
    avail_state = _read_availability()
    agents = avail_state.get("agents", {})
    if req.agent_name in agents and agents[req.agent_name].get("status") == "busy":
        raise HTTPException(
            status_code=409,
            detail=f"Agent '{req.agent_name}' is already busy"
        )

    # --- Mode: Local (Claude Code CLI on Mac) ---
    if req.mode == "local":
        return await _spawn_local(req, requester, avail_state, agents)

    # --- Mode: Vessel (phone dispatch) ---
    # Check vessel is connected
    if req.vessel_id not in vessels:
        raise HTTPException(
            status_code=503,
            detail=f"Vessel '{req.vessel_id}' is not connected"
        )

    # Load agent context (CLAUDE.md) from Mac-side storage
    identity = ""
    context_file = AGENT_CONTEXTS_DIR / req.agent_name / "CLAUDE.md"
    if context_file.exists():
        try:
            identity = context_file.read_text()
        except IOError as e:
            print(f"[spawn] Warning: could not read context for {req.agent_name}: {e}")

    # Generate session ID (full UUID for 122-bit entropy)
    session_id = str(uuid.uuid4())

    # Mark agent as busy
    _mark_agent_busy(req, agents, avail_state)

    # Create task for phone
    task_id = str(uuid.uuid4())
    task_dict = {
        "task_id": task_id,
        "vessel_id": req.vessel_id,
        "task_type": "agent",
        "payload": {
            "prompt": req.prompt,
            "agent_name": req.agent_name,
            "max_turns": req.max_turns,
            "identity": identity,
            "job_type": req.job_type,
            "session_id": session_id,
        },
        "priority": 1,
        "timeout": AGENT_SESSION_TIMEOUT,
        "status": "queued",
        "submitted_at": time.time(),
        "result": None,
        "completed_at": None,
    }
    tasks[task_id] = task_dict
    save_task(task_dict)

    # Queue for vessel
    if req.vessel_id not in task_queue:
        task_queue[req.vessel_id] = asyncio.Queue()
    await task_queue[req.vessel_id].put(task_dict)

    # Track session
    _agent_sessions[session_id] = {
        "agent_name": req.agent_name,
        "job_type": req.job_type,
        "task_id": task_id,
        "vessel_id": req.vessel_id,
        "mode": req.mode,
        "started_at": time.time(),
        "status": "running",
        "prompt_preview": req.prompt[:200],
        "completed_at": None,
        "result": None,
    }

    relay_log("AGENT_SPAWNED", {
        "session_id": session_id,
        "agent_name": req.agent_name,
        "job_type": req.job_type,
        "task_id": task_id,
        "mode": req.mode,
        "requester": requester,
    })

    return {
        "success": True,
        "session_id": session_id,
        "agent_name": req.agent_name,
        "job_type": req.job_type,
        "task_id": task_id,
        "status": "dispatched",
    }


def _mark_agent_busy(req: SpawnRequest, agents: dict, avail_state: dict):
    """Mark an agent as busy in the availability state."""
    agent_type_map = {
        "scanner": "scanner",
        "trader": "trader",
        "manager": "manager",
        "health": "health",
        "health_monitor": "health",
        "content_manager": "content_manager",
        "news_reporter": "content_manager",
        "compliance_counsel": "compliance_counsel",
        "compliance": "compliance_counsel",
        "scout": "scout",
        "vessel_scout": "scout",
        "intelligence_scout": "scout",
        "general": "trader",
    }
    avail_type = agent_type_map.get(req.job_type, "trader")
    now_str = datetime.utcnow().isoformat() + "Z"

    if req.agent_name not in agents:
        agents[req.agent_name] = {
            "status": "idle", "position": None,
            "assigned_at": None, "type": None, "last_checkin": None,
        }
    agents[req.agent_name]["status"] = "busy"
    agents[req.agent_name]["position"] = req.token_mint
    agents[req.agent_name]["assigned_at"] = now_str
    agents[req.agent_name]["type"] = avail_type
    if avail_type == "manager":
        agents[req.agent_name]["last_checkin"] = now_str
    _write_availability(avail_state)


async def _spawn_local(req: SpawnRequest, requester: str, avail_state: dict, agents: dict):
    """
    Spawn an agent locally via Claude Code CLI with MCP vessel_tools.
    Uses Claude subscription instead of API credits.

    Security:
    - --tools "" disables ALL built-in tools (no filesystem access)
    - --strict-mcp-config ensures only vessel_tools MCP server is loaded
    - --dangerously-skip-permissions is safe because only vessel_tools are available
    """
    # Verify Claude CLI exists
    if not CLAUDE_CLI_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail=f"Claude CLI not found at {CLAUDE_CLI_PATH}"
        )

    # Load agent context (CLAUDE.md) from Mac-side storage
    identity = ""
    context_file = AGENT_CONTEXTS_DIR / req.agent_name / "CLAUDE.md"
    if context_file.exists():
        try:
            identity = context_file.read_text()
        except IOError as e:
            print(f"[spawn-local] Warning: could not read context for {req.agent_name}: {e}")

    # Generate session ID
    session_id = str(uuid.uuid4())

    # Write temp MCP config with agent-specific env vars
    mcp_config = {
        "mcpServers": {
            "vessel-tools": {
                "command": str(MCP_PYTHON_PATH),
                "args": [str(MCP_SERVER_PATH)],
                "env": {
                    "AGENT_NAME": req.agent_name,
                    "JOB_TYPE": req.job_type,
                    "RELAY_URL": f"http://localhost:{SERVER_PORT}",
                    "VESSEL_SECRET": VESSEL_SECRET,
                },
            }
        }
    }

    mcp_config_path = None
    try:
        # Write temp config file
        fd, mcp_config_path = tempfile.mkstemp(
            prefix=f"vessel_mcp_{req.agent_name}_",
            suffix=".json",
        )
        with os.fdopen(fd, "w") as f:
            json.dump(mcp_config, f)

        # Build system prompt
        system_prompt = _build_local_system_prompt(req.agent_name, req.job_type, identity)

        # Mark agent as busy
        _mark_agent_busy(req, agents, avail_state)

        # Track session (before spawn so it's visible immediately)
        _agent_sessions[session_id] = {
            "agent_name": req.agent_name,
            "job_type": req.job_type,
            "task_id": None,  # No phone task for local mode
            "vessel_id": "local",
            "mode": "local",
            "started_at": time.time(),
            "status": "running",
            "prompt_preview": req.prompt[:200],
            "completed_at": None,
            "result": None,
            "process": None,  # Will be set by background task
            "mcp_config_path": mcp_config_path,
        }

        relay_log("AGENT_SPAWNED_LOCAL", {
            "session_id": session_id,
            "agent_name": req.agent_name,
            "job_type": req.job_type,
            "mode": "local",
            "requester": requester,
            "max_budget_usd": req.max_budget_usd,
        })

        # Spawn Claude CLI as background task
        asyncio.create_task(
            _run_local_agent(session_id, req, system_prompt, mcp_config_path)
        )

        return {
            "success": True,
            "session_id": session_id,
            "agent_name": req.agent_name,
            "job_type": req.job_type,
            "task_id": None,
            "status": "spawned_local",
            "mode": "local",
        }

    except Exception as e:
        # Clean up on error
        if mcp_config_path and os.path.exists(mcp_config_path):
            os.unlink(mcp_config_path)
        relay_log("SPAWN_LOCAL_ERROR", {
            "agent_name": req.agent_name,
            "error": str(e),
        })
        raise HTTPException(status_code=500, detail=f"Local spawn failed: {str(e)}")


def _build_local_system_prompt(agent_name: str, job_type: str, identity: str) -> str:
    """Build the system prompt for a locally-spawned agent."""
    parts = []

    # Agent identity
    if identity:
        parts.append(f"<agent-identity>\n{identity}\n</agent-identity>")

    # Constraints
    parts.append(f"""<agent-constraints>
You are {agent_name}, a vessel agent in the SXAN trading system.
Your job_type is: {job_type}

CRITICAL RULES:
- You can ONLY use the vessel-tools MCP tools. You have NO other capabilities.
- You cannot read files, write files, or run shell commands.
- You cannot access the internet except through your vessel tools.
- Complete your task using ONLY the tools available to you.
- When done, output a final summary of what you accomplished.
- Be concise and efficient. Do not waste tool calls.
</agent-constraints>""")

    return "\n\n".join(parts)


async def _run_local_agent(session_id: str, req: SpawnRequest, system_prompt: str, mcp_config_path: str):
    """
    Background task: run Claude CLI for a local agent spawn.
    Parses output, updates session, and cleans up.
    """
    agent_name = req.agent_name
    process = None

    try:
        # Build claude CLI command
        cmd = [
            str(CLAUDE_CLI_PATH),
            "--print",
            "--tools", "",
            "--mcp-config", mcp_config_path,
            "--strict-mcp-config",
            "--system-prompt", system_prompt,
            "--model", "haiku",
            "--output-format", "json",
            "--no-session-persistence",
            "--dangerously-skip-permissions",
            "--max-budget-usd", str(req.max_budget_usd),
            req.prompt,
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Store process reference for kill support
        if session_id in _agent_sessions:
            _agent_sessions[session_id]["process"] = process

        # Wait for completion with timeout
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=AGENT_SESSION_TIMEOUT,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            if session_id in _agent_sessions:
                _agent_sessions[session_id]["status"] = "timed_out"
                _agent_sessions[session_id]["completed_at"] = time.time()
            await _auto_release_agent(agent_name)
            relay_log("LOCAL_AGENT_TIMEOUT", {
                "session_id": session_id,
                "agent_name": agent_name,
            })
            return

        stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""

        # Parse JSON output
        result = None
        try:
            result = json.loads(stdout_text)
        except (json.JSONDecodeError, ValueError):
            result = {"raw_output": stdout_text[:5000]}

        # Determine status
        exit_code = process.returncode
        status = "completed" if exit_code == 0 else "error"

        if stderr_text and exit_code != 0:
            if result is None:
                result = {}
            if isinstance(result, dict):
                result["stderr"] = stderr_text[:2000]

        # Update session
        if session_id in _agent_sessions:
            session = _agent_sessions[session_id]
            session["status"] = status
            session["completed_at"] = time.time()
            session["result"] = result
            session["exit_code"] = exit_code

            # Extract cost if available
            if isinstance(result, dict):
                cost = result.get("cost_usd") or result.get("total_cost_usd")
                if cost:
                    session["cost_usd"] = cost

        relay_log("LOCAL_AGENT_COMPLETED", {
            "session_id": session_id,
            "agent_name": agent_name,
            "status": status,
            "exit_code": exit_code,
            "cost_usd": session.get("cost_usd") if session_id in _agent_sessions else None,
        })

    except Exception as e:
        print(f"[spawn-local] ERROR running {agent_name}: {e}", file=sys.stderr)
        if session_id in _agent_sessions:
            _agent_sessions[session_id]["status"] = "error"
            _agent_sessions[session_id]["completed_at"] = time.time()
            _agent_sessions[session_id]["result"] = {"error": str(e)}
        relay_log("LOCAL_AGENT_ERROR", {
            "session_id": session_id,
            "agent_name": agent_name,
            "error": str(e),
        })
    finally:
        # Auto-release agent
        await _auto_release_agent(agent_name)

        # Clean up temp MCP config
        try:
            if mcp_config_path and os.path.exists(mcp_config_path):
                os.unlink(mcp_config_path)
        except OSError:
            pass

        # Clear process reference
        if session_id in _agent_sessions:
            _agent_sessions[session_id].pop("process", None)
            _agent_sessions[session_id].pop("mcp_config_path", None)


@app.get("/agents/context/{agent_name}")
async def get_agent_context(agent_name: str, authorization: str = Header()):
    """
    Get agent identity docs from Mac-side storage.
    Used by phone to sync agent docs on startup.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    if agent_name not in AGENT_WHITELIST or agent_name == "MsWednesday":
        raise HTTPException(status_code=400, detail=f"No context for '{agent_name}'")

    context_file = AGENT_CONTEXTS_DIR / agent_name / "CLAUDE.md"
    config_file = AGENT_CONTEXTS_DIR / agent_name / "config.json"

    result = {"agent_name": agent_name, "identity": "", "config": {}}

    if context_file.exists():
        try:
            result["identity"] = context_file.read_text()
        except IOError as e:
            print(f"[server] WARNING: Failed to read context for {agent_name}: {e}", file=sys.stderr)

    if config_file.exists():
        try:
            result["config"] = json.loads(config_file.read_text())
        except (IOError, json.JSONDecodeError) as e:
            print(f"[server] WARNING: Failed to read config for {agent_name}: {e}", file=sys.stderr)

    return result


@app.get("/agents/sessions")
async def list_agent_sessions(request: Request, authorization: str = Header()):
    """List agent sessions. Non-MsWednesday agents only see own sessions."""
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    sessions = []
    for session_id, session in _agent_sessions.items():
        # Per-agent session isolation: agents can only see own sessions
        if requester and requester != 'MsWednesday' and session.get("agent_name") != requester:
            continue
        sessions.append({
            "session_id": session_id,
            "agent_name": session.get("agent_name"),
            "job_type": session.get("job_type"),
            "status": session.get("status"),
            "mode": session.get("mode"),
            "started_at": session.get("started_at"),
            "completed_at": session.get("completed_at"),
            "elapsed_seconds": round(
                (session.get("completed_at") or time.time()) - session.get("started_at", time.time()),
                1,
            ),
        })

    return {"sessions": sessions, "total": len(sessions)}


@app.get("/agents/sessions/{session_id}")
async def get_agent_session(session_id: str, request: Request, authorization: str = Header()):
    """Get detailed status for a specific agent session."""
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    session = _agent_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Per-agent session isolation
    if requester and requester != 'MsWednesday' and session.get("agent_name") != requester:
        raise HTTPException(status_code=403, detail="Cannot view another agent's session")

    # If session has a task_id, check for result
    task_id = session.get("task_id")
    result = None
    if task_id and task_id in tasks:
        task = tasks[task_id]
        if task.get("result"):
            result = task["result"]
            # Update session status from task result
            if task.get("status") in ("completed", "error", "timeout"):
                session["status"] = task["status"]
                session["completed_at"] = task.get("completed_at", time.time())
                session["result"] = result

    return {
        "session_id": session_id,
        **session,
        "result_preview": str(result)[:500] if result else None,
    }


@app.post("/agents/sessions/{session_id}/kill")
async def kill_agent_session(session_id: str, request: Request, authorization: str = Header()):
    """Cancel a running agent session."""
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    session = _agent_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Per-agent session isolation: agents can only kill own sessions
    if requester and requester != 'MsWednesday' and session.get("agent_name") != requester:
        relay_log("SESSION_KILL_DENIED", {
            "session_id": session_id,
            "requester": requester,
            "target_agent": session.get("agent_name"),
            "reason": "cross_agent_not_allowed",
        })
        raise HTTPException(status_code=403, detail="Cannot kill another agent's session")

    if session.get("status") != "running":
        return {
            "success": False,
            "error": f"Session is not running (status: {session.get('status')})",
        }

    task_id = session.get("task_id")
    vessel_id = session.get("vessel_id", "phone-01")
    agent_name = session.get("agent_name", "unknown")

    # Kill local process or send cancel to phone
    if session.get("mode") == "local":
        process = session.get("process")
        if process and process.returncode is None:
            try:
                process.terminate()
                # Give it 5s to clean up, then force kill
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
            except Exception as e:
                relay_log("SESSION_KILL_PROCESS_ERROR", {
                    "session_id": session_id,
                    "error": str(e),
                })
    else:
        ws = vessels.get(vessel_id)
        if ws and task_id:
            try:
                await ws.send_json({"type": "cancel_task", "task_id": task_id})
            except Exception as e:
                relay_log("SESSION_KILL_SEND_ERROR", {
                    "session_id": session_id,
                    "error": str(e),
                })

    # Mark session as killed
    session["status"] = "killed"
    session["completed_at"] = time.time()

    # Release agent
    await _auto_release_agent(agent_name)

    relay_log("SESSION_KILLED", {
        "session_id": session_id,
        "agent_name": agent_name,
        "requester": requester,
    })

    return {
        "success": True,
        "session_id": session_id,
        "agent_name": agent_name,
        "status": "killed",
    }


# --- Compliance Audit (msCounsel) ---


def _read_compliance_log() -> list:
    """Read compliance audit entries."""
    if not COMPLIANCE_AUDIT_PATH.exists():
        return []
    try:
        return json.loads(COMPLIANCE_AUDIT_PATH.read_text())
    except (json.JSONDecodeError, IOError):
        return []


def _write_compliance_log(entries: list):
    """Atomic write compliance audit log."""
    import tempfile
    fd, tmp_path = tempfile.mkstemp(dir=str(COMPLIANCE_AUDIT_PATH.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(entries, f, indent=2)
        os.replace(tmp_path, str(COMPLIANCE_AUDIT_PATH))
    except Exception as e:
        print(f"[compliance] CRITICAL: Failed to write compliance log: {e}", file=sys.stderr)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


class ComplianceEntry(BaseModel):
    question: str
    decision: str           # COMPLIANT, NOT_COMPLIANT, GRAY_ZONE
    reasoning: str
    jurisdiction: str = "US"
    reference: str = ""
    human_review_required: bool = False
    requested_by: str = ""
    next_action: str = ""


@app.post("/compliance/log")
async def post_compliance_entry(entry: ComplianceEntry, request: Request, authorization: str = Header()):
    """
    Store a compliance audit decision. Used by msCounsel to log rulings.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    requester = get_requester(request)

    # Generate audit ID
    now = datetime.utcnow()
    audit_id = f"COUNSEL-{now.strftime('%Y%m%d')}-{now.strftime('%H%M%S')}"

    record = {
        "audit_id": audit_id,
        "timestamp": now.isoformat() + "Z",
        "question": entry.question,
        "decision": entry.decision,
        "reasoning": entry.reasoning,
        "jurisdiction": entry.jurisdiction,
        "reference": entry.reference,
        "human_review_required": entry.human_review_required,
        "requested_by": entry.requested_by or requester,
        "next_action": entry.next_action,
        "logged_by": requester,
    }

    entries = _read_compliance_log()
    entries.append(record)
    _write_compliance_log(entries)

    relay_log("COMPLIANCE_DECISION", {
        "audit_id": audit_id,
        "decision": entry.decision,
        "question_preview": entry.question[:100],
        "logged_by": requester,
    })

    return {"success": True, "audit_id": audit_id, "record": record}


@app.get("/compliance/log")
async def get_compliance_log(
    authorization: str = Header(),
    limit: int = 50,
    decision: Optional[str] = None,
):
    """
    Read compliance audit entries. Filterable by decision type.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    entries = _read_compliance_log()

    # Filter by decision type if specified
    if decision:
        entries = [e for e in entries if e.get("decision") == decision]

    # Return most recent first, limited
    entries = list(reversed(entries))[:limit]

    return {"entries": entries, "total": len(entries)}


@app.get("/compliance/report")
async def get_compliance_report(authorization: str = Header()):
    """
    Generate compliance summary report (for msCounsel weekly reports).
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    entries = _read_compliance_log()
    total = len(entries)

    # Count by decision type
    compliant = sum(1 for e in entries if e.get("decision") == "COMPLIANT")
    not_compliant = sum(1 for e in entries if e.get("decision") == "NOT_COMPLIANT")
    gray_zone = sum(1 for e in entries if e.get("decision") == "GRAY_ZONE")
    human_review = sum(1 for e in entries if e.get("human_review_required"))

    # Recent entries (last 7 days)
    week_ago = time.time() - (7 * 86400)
    recent = [e for e in entries if _parse_timestamp(e.get("timestamp", "")) > week_ago]
    recent_total = len(recent)
    recent_compliant = sum(1 for e in recent if e.get("decision") == "COMPLIANT")
    recent_not_compliant = sum(1 for e in recent if e.get("decision") == "NOT_COMPLIANT")
    recent_gray = sum(1 for e in recent if e.get("decision") == "GRAY_ZONE")

    return {
        "all_time": {
            "total": total,
            "compliant": compliant,
            "not_compliant": not_compliant,
            "gray_zone": gray_zone,
            "human_review_required": human_review,
        },
        "last_7_days": {
            "total": recent_total,
            "compliant": recent_compliant,
            "not_compliant": recent_not_compliant,
            "gray_zone": recent_gray,
        },
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


def _parse_timestamp(ts: str) -> float:
    """Parse ISO timestamp to epoch seconds."""
    try:
        return datetime.fromisoformat(ts.rstrip("Z")).timestamp()
    except (ValueError, AttributeError):
        return 0


# --- WebSocket (for vessel to connect and receive tasks) ---

MAX_WS_CONNECTIONS = 3  # Max concurrent WebSocket connections (only 1 phone expected)

@app.websocket("/ws/{vessel_id}")
async def vessel_socket(websocket: WebSocket, vessel_id: str):
    # Reject duplicate vessel_id connections
    if vessel_id in vessels:
        await websocket.accept()
        await websocket.send_json({"error": "vessel_id_already_connected"})
        await websocket.close()
        relay_log('WS_DUPLICATE_REJECTED', {'vessel_id': vessel_id})
        return

    # Enforce max concurrent connections
    if len(vessels) >= MAX_WS_CONNECTIONS:
        await websocket.accept()
        await websocket.send_json({"error": "max_connections_reached", "limit": MAX_WS_CONNECTIONS})
        await websocket.close()
        relay_log('WS_CONNECTION_LIMIT', {'vessel_id': vessel_id, 'current': len(vessels), 'max': MAX_WS_CONNECTIONS})
        return

    # Auth handshake
    await websocket.accept()
    try:
        auth_msg = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        if auth_msg.get("token") != VESSEL_SECRET:
            await websocket.send_json({"error": "auth_failed"})
            await websocket.close()
            return
    except (asyncio.TimeoutError, Exception):
        await websocket.close()
        return

    await websocket.send_json({"status": "connected", "vessel_id": vessel_id})
    vessels[vessel_id] = websocket

    if vessel_id not in task_queue:
        task_queue[vessel_id] = asyncio.Queue()

    print(f"[server] Vessel {vessel_id} connected")

    try:
        # Two concurrent loops: send tasks + receive results
        await asyncio.gather(
            _send_tasks(websocket, vessel_id),
            _receive_results(websocket, vessel_id),
        )
    except WebSocketDisconnect:
        print(f"[server] Vessel {vessel_id} disconnected")
    finally:
        vessels.pop(vessel_id, None)


async def _send_tasks(websocket: WebSocket, vessel_id: str):
    """Pull from queue and send to vessel."""
    queue = task_queue[vessel_id]
    while True:
        task = await queue.get()
        task["status"] = "sent"
        tasks[task["task_id"]] = task
        await websocket.send_json({"type": "task", "data": task})
        print(f"[server] Sent task {task['task_id']} to {vessel_id}")


async def _receive_results(websocket: WebSocket, vessel_id: str):
    """Receive results from vessel."""
    while True:
        msg = await websocket.receive_json()
        if msg.get("type") == "result":
            task_id = msg["task_id"]
            if task_id in tasks:
                tasks[task_id]["status"] = msg.get("status", "completed")
                tasks[task_id]["result"] = msg.get("result")
                tasks[task_id]["completed_at"] = time.time()
                # Persist the completed task
                save_task(tasks[task_id])
                print(f"[server] Result for task {task_id}: {msg.get('status')}")

                # Update agent session if this was a spawned agent task
                result_data = msg.get("result", {})
                session_id = result_data.get("session_id") if isinstance(result_data, dict) else None
                if session_id and session_id in _agent_sessions:
                    session = _agent_sessions[session_id]
                    session["status"] = msg.get("status", "completed")
                    session["completed_at"] = time.time()
                    session["result"] = result_data
                    agent_name = session.get("agent_name")
                    if agent_name:
                        await _auto_release_agent(agent_name)
                    relay_log("SESSION_COMPLETED", {
                        "session_id": session_id,
                        "agent_name": agent_name,
                        "status": session["status"],
                        "turns": result_data.get("turns"),
                    })

        elif msg.get("type") == "cancel_ack":
            task_id = msg.get("task_id", "")
            cancelled = msg.get("cancelled", False)
            print(f"[server] Cancel ack for {task_id}: {'ok' if cancelled else 'failed'}")

        elif msg.get("type") == "heartbeat":
            await websocket.send_json({"type": "heartbeat_ack"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SERVER_HOST, port=int(SERVER_PORT))
