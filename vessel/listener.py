"""
VesselProject - Phone Listener
Runs on the phone in Termux. Connects outbound to the relay server
via WebSocket, receives tasks, executes them, sends results back.
"""

import asyncio
import json
import signal
import sys
import os
import time

sys.path.insert(0, "..")
from config import VESSEL_SECRET, VESSEL_ID, SERVER_PORT

from executor import execute_task, cancel_session


# Server URL - set via env var (your computer's IP or a deployed server)
DEFAULT_SERVER_IP = "100.78.3.119"
SERVER_URL = os.getenv("VESSEL_SERVER_URL", f"ws://{DEFAULT_SERVER_IP}:{SERVER_PORT}")

RECONNECT_DELAY = 5      # seconds between reconnect attempts
HEARTBEAT_INTERVAL = 30   # seconds between heartbeats

# Track running agent tasks for cancellation
# task_id -> session_id mapping
_running_agent_tasks = {}


async def connect_and_listen():
    """Main loop: connect to server, receive tasks, execute, return results."""
    try:
        import websockets
    except ImportError:
        print("[vessel] ERROR: websockets not installed. Run: pip install websockets")
        sys.exit(1)

    while True:
        try:
            url = f"{SERVER_URL}/ws/{VESSEL_ID}"
            print(f"[vessel] Connecting to {url}...")

            async with websockets.connect(url) as ws:
                # Auth handshake
                await ws.send(json.dumps({"token": VESSEL_SECRET}))
                response = json.loads(await ws.recv())

                if response.get("status") != "connected":
                    print(f"[vessel] Auth failed: {response}")
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue

                print(f"[vessel] Connected as {VESSEL_ID}")

                # Run task handler and heartbeat concurrently
                await asyncio.gather(
                    _handle_messages(ws),
                    _heartbeat(ws),
                )

        except (ConnectionRefusedError, OSError) as e:
            print(f"[vessel] Connection failed: {e}. Retrying in {RECONNECT_DELAY}s...")
        except Exception as e:
            print(f"[vessel] Error: {e}. Reconnecting in {RECONNECT_DELAY}s...")

        await asyncio.sleep(RECONNECT_DELAY)


async def _handle_messages(ws):
    """Receive and process messages from server."""
    async for raw in ws:
        msg = json.loads(raw)

        if msg.get("type") == "task":
            task = msg["data"]
            task_id = task["task_id"]
            print(f"[vessel] Received task {task_id} ({task.get('task_type', 'unknown')})")

            # Track agent tasks for cancellation
            if task.get("task_type") == "agent":
                session_id = task.get("payload", {}).get("session_id", task_id)
                _running_agent_tasks[task_id] = session_id

            # Execute in background so we can keep receiving
            asyncio.create_task(_execute_and_report(ws, task))

        elif msg.get("type") == "cancel_task":
            # Cancel a running agent session
            task_id = msg.get("task_id", "")
            session_id = _running_agent_tasks.get(task_id)
            if session_id:
                cancelled = cancel_session(session_id)
                print(f"[vessel] Cancel requested for task {task_id} (session {session_id}): {'ok' if cancelled else 'not found'}")
                await ws.send(json.dumps({
                    "type": "cancel_ack",
                    "task_id": task_id,
                    "cancelled": cancelled,
                }))
            else:
                print(f"[vessel] Cancel requested for unknown task {task_id}")
                await ws.send(json.dumps({
                    "type": "cancel_ack",
                    "task_id": task_id,
                    "cancelled": False,
                    "error": "task not found",
                }))

        elif msg.get("type") == "heartbeat_ack":
            pass  # server acknowledged our heartbeat


async def _execute_and_report(ws, task: dict):
    """Execute a task and send the result back."""
    task_id = task["task_id"]
    start = time.time()

    try:
        result = await execute_task(task)
        elapsed = round(time.time() - start, 2)
        result["elapsed_seconds"] = elapsed

        await ws.send(json.dumps({
            "type": "result",
            "task_id": task_id,
            "status": result.get("status", "completed"),
            "result": result,
        }))

        print(f"[vessel] Task {task_id} done ({elapsed}s): {result.get('status')}")

    except Exception as e:
        await ws.send(json.dumps({
            "type": "result",
            "task_id": task_id,
            "status": "error",
            "result": {"error": str(e)},
        }))
        print(f"[vessel] Task {task_id} failed: {e}")

    finally:
        # Clean up tracking
        _running_agent_tasks.pop(task_id, None)


async def _heartbeat(ws):
    """Send periodic heartbeats to keep connection alive."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            await ws.send(json.dumps({"type": "heartbeat", "vessel_id": VESSEL_ID}))
        except Exception:
            break  # connection lost, outer loop will reconnect


def main():
    print(f"[vessel] VesselProject Listener - {VESSEL_ID}")
    print(f"[vessel] Server: {SERVER_URL}")
    print(f"[vessel] Press Ctrl+C to stop")

    loop = asyncio.new_event_loop()

    def shutdown(sig, frame):
        print("\n[vessel] Shutting down...")
        loop.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    loop.run_until_complete(connect_and_listen())


if __name__ == "__main__":
    main()
