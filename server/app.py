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
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header
from pydantic import BaseModel
from typing import Optional

import sys
sys.path.insert(0, "..")
from config import SERVER_HOST, SERVER_PORT, VESSEL_SECRET


# --- State ---

tasks = {}           # task_id -> task dict
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


# --- App ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[server] Vessel relay starting on {SERVER_HOST}:{SERVER_PORT}")
    yield
    print("[server] Shutting down")

app = FastAPI(title="VesselProject Relay", lifespan=lifespan)


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
    }
    tasks[task_id] = task_dict

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
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")

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
                print(f"[server] Result for task {task_id}: {msg.get('status')}")
        elif msg.get("type") == "heartbeat":
            await websocket.send_json({"type": "heartbeat_ack"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SERVER_HOST, port=int(SERVER_PORT))
