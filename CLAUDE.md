# VesselProject

## What This Is
A phone-based isolated execution environment ("vessel") for MsWednesday to spawn sub-agents on. Brandon's Cloud Mobile Android phone runs a listener service in Termux that receives tasks via WebSocket from a relay server.

## Architecture
```
MsWednesday (Mac)
  → vessel_client.py (in her workspace)
  → POST http://192.168.1.146:8777/task
       ↕ WebSocket relay (server/app.py)
  Phone (Termux) → listener.py → executor.py → returns result
```

## Components

### Server (runs on Mac)
- `server/app.py` — FastAPI + WebSocket relay server
- Accepts tasks via REST from Wednesday, relays to phone via WebSocket
- Port: 8777, Auth: shared secret
- Start: `VESSEL_SECRET="mrsunday" python server/app.py`

### Vessel (runs on phone in Termux)
- `vessel/listener.py` — WebSocket client, connects to relay, receives tasks
- `vessel/executor.py` — Executes shell, python, and agent tasks
- `vessel/setup_phone.sh` — Termux setup script
- Server URL hardcoded in listener.py: `ws://192.168.1.146:8777`
- Start: `cd ~/vesselproject/vessel && python listener.py`

### Config
- `config.py` — Shared config (secret, model, limits)
- `secrets.txt` — API key (gitignored, lives on phone only)
- Phone path is lowercase: `~/vesselproject/` (not VesselProject)

### Wednesday's Client
- `~/Desktop/MsWednesday/vessel_client.py` — Helper to send tasks
- Usage: `from vessel_client import vessel; vessel.shell("ls")`

## Task Types
- **shell** — Run a command: `{"task_type": "shell", "payload": {"command": "ls"}}`
- **python** — Run Python code: `{"task_type": "python", "payload": {"code": "print(1)"}}`
- **agent** — Claude sub-agent: `{"task_type": "agent", "payload": {"prompt": "do X"}}`

## Auth
- Server + phone share secret: `mrsunday`
- REST calls use `Authorization: mrsunday` header
- WebSocket handshake sends `{"token": "mrsunday"}`

## Phone Details
- Device: Cloud Mobile (budget Android 15)
- Terminal: Termux (from F-Droid, NOT Play Store)
- Python packages can't compile Rust (no jiter/anthropic SDK)
- Agent tasks use raw urllib to call Anthropic API
- Model: claude-haiku-4-5-20251001 (cheap, fast)
- Keep alive: `termux-wake-lock` + `tmux new -s vessel`

## Current Status
- Shell tasks: WORKING
- Python tasks: WORKING
- Agent tasks: BLOCKED (Anthropic account needs credits)
- Phone connected on same WiFi as Mac (192.168.1.146)

## Known Issues
- Env vars don't work reliably in Termux — values hardcoded in config.py and listener.py
- Phone screen wraps long text, breaks copy-paste — use short commands or write files via python tasks
- nano is painful on phone — prefer writing files remotely via python tasks through the vessel
- Git pull requires repo to be temporarily public (phone has no auth token)
- Server must be restarted with `VESSEL_SECRET="mrsunday"` env var

## Updating Phone Code
1. Commit and push from Mac
2. `gh repo edit mrsunday777/VesselProject --visibility public --accept-visibility-change-consequences`
3. On phone: `cd ~/vesselproject && git checkout -- . && git pull`
4. `gh repo edit mrsunday777/VesselProject --visibility private --accept-visibility-change-consequences`
5. Re-hardcode `SERVER_URL` in listener.py (git pull resets it)
6. Restart listener

## Spawn Authority
MsWednesday is the ONLY agent authorized to spawn sub-agents (per SOUL.md). The phone vessel is a spawn target under her authority.

## Next Steps
- [ ] Add Anthropic API credits so agent tasks work
- [ ] Integrate with openclaw framework
- [ ] Set up ngrok or deploy server for remote access (currently local WiFi only)
- [ ] Add phone to swarm dashboard
- [ ] Build persistent task queue (current queue is in-memory, lost on server restart)
