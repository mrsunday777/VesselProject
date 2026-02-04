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
- Methods: `vessel.shell(cmd)`, `vessel.python(code)`, `vessel.agent(prompt)`, `vessel.status()`
- Zero dependencies (uses stdlib urllib)

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
- Agent tasks use raw urllib to call Anthropic API directly
- Model: claude-haiku-4-5-20251001 (cheap, fast)
- Keep alive: `termux-wake-lock` + `tmux new -s vessel`

## Spawn Authority & Gateway Config

### Policy
MsWednesday is the ONLY agent authorized to spawn sub-agents (per SOUL.md + AGENT_SPAWN_AUTHORITY.md).

### Gateway Config (NEW)
**File:** `~/Desktop/MsWednesday/gateway_config.json`

Machine-readable config for the openclaw framework:
- `allowedAgents` — Full roster: cp0, cp1, cp9, mssunday, vessel-phone-01
- `allowedSpawner` — "mswednesday" (exclusive)
- `rules` — Max 5 concurrent agents, require spawn gate, 24h expiry
- Vessel transport config (WebSocket relay URL, vessel ID, capabilities)

### Spawn Gate
- `~/Desktop/MsWednesday/spawn_gate.py` — HMAC-signed gate system
- Phone vessel added as `vessel-phone-01` in AGENT_WORKSPACES
- Wednesday authorizes: `python3 spawn_gate.py authorize vessel-phone-01`
- Gates expire after 24h, HMAC-SHA256 signed

### Files Updated for Gateway Fix
- `spawn_gate.py` — Added vessel-phone-01 to AGENT_WORKSPACES
- `gateway_config.json` — NEW: Machine-readable agent roster + spawn rules
- `AGENT_SPAWN_AUTHORITY.md` — Added vessel to agent table + documented gateway config

## Current Status
- Shell tasks: WORKING
- Python tasks: WORKING
- Agent tasks: BLOCKED (Anthropic account needs credits)
- Phone connected on same WiFi as Mac (192.168.1.146)
- Gateway config: CREATED (Wednesday can load it for openclaw)
- Spawn gate: UPDATED (vessel-phone-01 registered)

## Known Issues
- Env vars don't work reliably in Termux — values hardcoded in config.py and listener.py
- Phone screen wraps long text, breaks copy-paste — use short commands or write files via python tasks
- nano is painful on phone — prefer writing files remotely via python tasks through the vessel
- Git pull requires repo to be temporarily public (phone has no auth token)
- Server must be restarted with `VESSEL_SECRET="mrsunday"` env var
- Server task queue is in-memory — tasks lost on restart

## Updating Phone Code
1. Commit and push from Mac
2. `gh repo edit mrsunday777/VesselProject --visibility public --accept-visibility-change-consequences`
3. On phone: `cd ~/vesselproject && git checkout -- . && git pull`
4. `gh repo edit mrsunday777/VesselProject --visibility private --accept-visibility-change-consequences`
5. Re-hardcode `SERVER_URL` in listener.py (git pull resets it)
6. Restart listener

## Writing Files to Phone Remotely
Since nano is unreliable on the phone, use python tasks to write files:
```python
vessel.python("""
f = open('/data/data/com.termux/files/home/vesselproject/somefile.txt', 'w')
f.write('contents here')
f.close()
print('done')
""")
```

## Next Steps
- [ ] Add Anthropic API credits so agent tasks work
- [ ] Integrate with openclaw framework (gateway_config.json ready)
- [ ] Set up ngrok or deploy server for remote access (currently local WiFi only)
- [ ] Add phone vessel to swarm dashboard
- [ ] Build persistent task queue (SQLite or file-based)
- [ ] Wednesday to test vessel.agent() once credits are live
- [ ] Build openclaw agents framework (Brandon + Wednesday)
