"""
VesselProject - Relay Server
Sits between MsWednesday and the phone vessel.
Wednesday submits tasks via REST, vessel picks them up via WebSocket.
"""

import asyncio
import json
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

import httpx

# Import config from parent directory (absolute path to handle any working dir)
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
from config import SERVER_HOST, SERVER_PORT, VESSEL_SECRET

# Position state file (written by vessel_monitor_service on this machine)
POSITION_STATE_FILE = Path.home() / 'position_state.json'

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
except Exception:
    pass

# Relay audit log
RELAY_LOG = Path(PROJECT_ROOT) / 'relay_audit.log'

# Solana address pattern (base58, 32-44 chars)
SOLANA_ADDR_RE = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')

# Whitelisted agent names (only these agents can trade through the relay)
AGENT_WHITELIST = {"MsWednesday", "CP0", "CP1", "CP9", "msSunday"}

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
    return hashlib.sha256(token.encode()).hexdigest() == hashlib.sha256(VESSEL_SECRET.encode()).hexdigest()


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
    except IOError:
        pass
    print(f"[relay] {action}: {json.dumps(details)}")


# --- App ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[server] Vessel relay starting on {SERVER_HOST}:{SERVER_PORT}")
    init_db()
    print(f"[server] Task database initialized: {DB_PATH}")
    yield
    print("[server] Shutting down")

app = FastAPI(title="VesselProject Relay", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# --- REST endpoints (for MsWednesday to submit tasks) ---

@app.post("/task", response_model=TaskResponse)
async def submit_task(task: TaskSubmit, authorization: str = Header()):
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    task_id = str(uuid.uuid4())[:8]
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


class SellRequest(BaseModel):
    token_mint: str
    percent: float = 100.0
    slippage_bps: int = 75
    agent_name: str = "MsWednesday"


class NotifyRequest(BaseModel):
    title: str
    details: str
    tx_hash: Optional[str] = None


@app.post("/execute/sell")
async def relay_sell(req: SellRequest, authorization: str = Header()):
    """
    Proxy sell command to SXAN wallet API.
    Validates input, logs the action, forwards to localhost:5001.
    Routes to the correct agent wallet based on agent_name.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

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

    relay_log('SELL_REQUESTED', {
        'agent_name': req.agent_name,
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

        if resp.status_code == 200:
            return result
        else:
            return JSONResponse(status_code=resp.status_code, content=result)

    except Exception as e:
        relay_log('SELL_ERROR', {'agent_name': req.agent_name, 'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'SXAN API unreachable: {str(e)}'})


@app.post("/notify")
async def relay_notify(req: NotifyRequest, authorization: str = Header()):
    """
    Proxy notification to SXAN dashboard (Telegram alert to Brandon).
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    # Sanitize: limit lengths
    title = req.title[:100]
    details = req.details[:500]

    relay_log('NOTIFY_REQUESTED', {'title': title})

    message = f"**{title}**\n\n{details}"
    if req.tx_hash:
        message += f"\n\nTX: {req.tx_hash[:88]}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{SXAN_API_BASE}/api/notify",
                json={'user_id': '6265463172', 'message': message},
            )

        relay_log('NOTIFY_RESULT', {'status_code': resp.status_code})
        return {'status': 'sent' if resp.status_code == 200 else 'error'}

    except Exception as e:
        relay_log('NOTIFY_ERROR', {'error': str(e)})
        return JSONResponse(status_code=502, content={'error': f'Notification failed: {str(e)}'})


# --- Read-only feed proxy endpoints (for vessel agents to access market data) ---
# All feeds proxy to SXAN dashboard APIs on localhost. Read-only, auth required.

@app.get("/feeds/telegram")
async def feed_telegram(authorization: str = Header(), limit: int = 50):
    """
    Proxy Telegram token feed from SXAN dashboard.
    Returns tokens extracted from monitored Telegram chats.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    if not AGENT_API_TOKEN:
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    # Clamp limit to sane range
    limit = max(1, min(limit, 200))

    relay_log('FEED_TELEGRAM', {'limit': limit})

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
async def feed_graduating(authorization: str = Header(), limit: int = 30):
    """
    Proxy almost-graduated tokens feed from SXAN swarm API.
    Returns tokens approaching graduation with progress %.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    if not AGENT_API_TOKEN:
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    limit = max(1, min(limit, 100))

    relay_log('FEED_GRADUATING', {'limit': limit})

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


@app.get("/feeds/launches")
async def feed_launches(authorization: str = Header(), limit: int = 30):
    """
    Proxy new token launches feed from SXAN swarm API.
    Returns recently launched pump.fun tokens.
    """
    if not verify_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    if not AGENT_API_TOKEN:
        raise HTTPException(status_code=500, detail="AGENT_API_TOKEN not configured on relay")

    limit = max(1, min(limit, 100))

    relay_log('FEED_LAUNCHES', {'limit': limit})

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


# --- WebSocket (for vessel to connect and receive tasks) ---

@app.websocket("/ws/{vessel_id}")
async def vessel_socket(websocket: WebSocket, vessel_id: str):
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
        elif msg.get("type") == "heartbeat":
            await websocket.send_json({"type": "heartbeat_ack"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SERVER_HOST, port=int(SERVER_PORT))
