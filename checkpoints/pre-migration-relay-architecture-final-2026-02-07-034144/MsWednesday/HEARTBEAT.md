# HEARTBEAT.md

## 🔐 OPERATIONAL SECURITY BOUNDARIES (HARDCODED)

**This is ours. We protect it.**

### ❌ NEVER SHARE (Vault)
- Wallet addresses, balances, transactions
- Trading strategy, entry/exit logic, position decisions
- System architecture, API implementations, agent mechanics
- Configuration files, API keys, auth tokens
- Anything that could be reverse-engineered into "how to clone this"
- CP agent code, trading mechanics, vessel framework internals

### ✅ CAN SHARE (Principles & Patterns)
- Spawn gate architecture (concept, not implementation)
- Retry loop patterns, exception handling techniques
- Memory system design (SOUL.md, AGENTS.md, heartbeat discipline)
- Operational lessons learned from failures/fixes
- Guardrail frameworks and decision-making processes
- General AI agent design patterns and principles

### 🎭 MOLTBOOK RULE
- Participate authentically, but don't doxx the system
- Share lessons and principles, not IP or internals
- Be genuine about our journey, vague about our tech
- If in doubt: "Can someone clone our system from this?" = Don't share

---

## Current Position (2026-02-05)
- AGENT in CP9 wallet: entry 0.156543 SOL, 185,416 tokens
- State: `trading/wallet_state.json` | Overrides: `trading/entry_overrides.json`

## Running Services
- **wallet_monitor** — scans all 5 agent wallets every 30s (`trading/wallet_monitor.py`)
- **vessel_monitor** — launchd service, 10s interval (`vessel_framework/vessel_monitor_service.py`)
- **relay** — port **8777** (NOT 8000), phone connected via Tailscale
- **catalyst aggregator** — `vessel_catalyst_aggregator.py --interval 300` (pending start)
- **Gunicorn** — 5 workers on port 5001

## Agent Wallets (all ENABLED, need SOL funding)
CP0, CP1, CP9, msSunday, MsWednesday — registered in `gateway_config.json`

## Pending Actions
- [ ] **🚨🚨🚨 BRANDON DIRECTIVE — RE-SPAWN FULL CREW IN LOCAL MODE (Feb 7)**
  - Phone vessel API key is NOT set. **DO NOT spawn to phone vessel** — it will fail.
  - **USE LOCAL MODE** for ALL spawns. This runs agents on Mac via Claude CLI subscription (no API credits).
  - All spawn gates are AUTHORIZED. Relay is live. All 6 agents idle and ready.
  - **Re-spawn all agents using `mode="local"`**:
    1. `wallet.spawn_agent("msSunday", "content_manager", "<prompt>", mode="local", max_turns=20)`
    2. `wallet.spawn_agent("msCounsel", "compliance_counsel", "<prompt>", mode="local", max_turns=20)`
    3. `wallet.spawn_agent("CP0", "trader", "<prompt>", mode="local", max_turns=20)`
    4. `wallet.spawn_agent("CP1", "scanner", "<prompt>", mode="local", max_turns=20)`
    5. `wallet.spawn_agent("CP9", "health_monitor", "<prompt>", mode="local", max_turns=20)`
    6. `wallet.spawn_agent("Chopper", "scout", "<prompt>", mode="local", max_turns=10)`
  - Chopper is NOW UNBLOCKED at the relay — gate workspace + availability added.
  - DO NOT skip Chopper — Brandon wants the full 6-agent crew up.
- [ ] Fund agent wallets with SOL (Brandon — manual transfer, blocks trading)

### Resolved / Stale (cleaned up Feb 6 ~14:00)
- [x] ~~Update CP9 to vessel_tools~~ — ALREADY DONE. CP9 runs on vessel framework with unified `agent_tools.py`. Old imports only in `archive/`.
- [x] ~~Push vessel_display.py to phone~~ — STALE. File doesn't exist at `vessel_framework/vessel_display.py`.
- [x] ~~Start catalyst aggregator~~ — STALE. `vessel_catalyst_aggregator.py` doesn't exist at expected path.
- [x] ~~Integrate Catalyst with CP0~~ — BLOCKED. Depends on catalyst aggregator which doesn't exist yet.
- [x] ~~Add position mints to config~~ — Wednesday should manage this as she trades. Files exist but format differs from briefing.

## Spawn Discipline (CRITICAL — enforced by relay + session watchdog)
- **ALWAYS** run `python3 spawn_gate.py authorize <agent>` BEFORE spawning
- **ALWAYS** verify with `python3 spawn_gate.py status <agent>` — must show AUTHORIZED
- **ALWAYS** spawn via relay: `POST /agents/spawn` (see SOUL.md step 3)
- **`/agents/assign` is DEPRECATED (returns 410 Gone)** — all agent work goes through spawn sessions
- Spawn sessions handle the full lifecycle: busy on spawn, auto-idle on end/kill/timeout
- `/agents/release` kept as emergency manual override only
- Agents run **locally on Mac** via `mode="local"` (Claude CLI + MCP proxy). Phone vessel is currently offline (no API key).
- Relay handles: gate check, identity loading, busy marking, task dispatch
- Session watchdog auto-kills sessions > 4 hours
- To recall early: `POST /agents/sessions/<session_id>/kill`
- To revoke gate: `python3 spawn_gate.py revoke <agent>`
- **ALWAYS** notify Brandon: `wallet.notify("Agent Spawned", "<agent> dispatched as <job_type>")`
- Agents have NO fixed roles — assign any agent to any job type
- Mac watchdog still runs — catches any rogue Mac-side agent processes (defense-in-depth)
- All events logged to relay audit log + phone session logs

## Key Reminders
- **Git repo**: `mrsunday777/MsWednesday` (PRIVATE). Push freely — no longer shares remote with public Sxan repo
- You are the ONLY spawn authority (`AGENT_SPAWN_AUTHORITY.md`)
- Wallet client: `from sxan_wallet import wallet` — `wallet.buy()`, `wallet.sell()`, `wallet.catalysts()`
- Auth: `AGENT_API_TOKEN` in `.env` — no session login needed
- Relay port: **8777** | Dashboard: **5001** | Tailscale IP: `100.78.3.119`
- Phone display is SSH-free — isolation boundary intact

## Relay Auth (CRITICAL — READ THIS)
- **Relay token**: The literal string `mrsunday` — NOT secrets.txt, NOT AGENT_API_TOKEN, NOT any file
- **secrets.txt** = Anthropic API key (for Claude API calls ONLY). NEVER use it for relay auth
- **SXAN dashboard** (port 5001): Uses `Bearer <AGENT_API_TOKEN>` from `.env`
- **Vessel relay** (port 8777): Uses `Authorization: mrsunday`
- **Best method**: Use `wallet.spawn_agent("name", "job", "prompt")` — handles auth automatically
- **Manual curl**: `-H "Authorization: mrsunday"` (the literal 8-character string)

## Content Pipeline (Social Media Manager)
You can act as `content_manager` — scan private logs for lessons, write public-facing posts, and submit them for Brandon's review via the swarm dashboard (instead of Telegram chat).

**Workflow**:
1. `wallet.scan_content(days_back=7)` — extracts anonymized lessons from session memory, git commits, spawn gate audit
2. `wallet.get_lessons()` — browse lessons (categories: `trade_lesson`, `system_insight`, `security_event`, `feature_update`, `debugging_story`)
3. Write a creative post based on a lesson (the AI/creative part — this is you)
4. `wallet.submit_draft(lesson_id, "Your post text")` — submits to dashboard Content Queue
5. Brandon reviews in swarm dashboard → approves/edits/rejects → copies to Twitter/X manually

**Format**: Every draft MUST follow the article template in `~/Desktop/Projects/Sxan/bot/content/ARTICLE_FORMAT.md`. Read it before writing. Structure: Title + Subtitle + Hook → Section Headers → Closer. Short paragraphs, bold labels for contrast pairs, `---` dividers between sections. Brandon publishes via X article composer.

**Key rules**: All content is anonymized twice (scanner + submission). No wallet addresses, agent names, paths, or amounts leak. Share principles and patterns, not implementation details. Follow the MOLTBOOK RULE from SOUL.md.

## Compliance Foundation (HARDCODED)
**READ:** `VESSEL_LABS_COMPLIANCE_FOUNDATION.md` — Non-negotiable regulatory boundaries
- We NEVER hold custody, trade, or guarantee outcomes
- We ALWAYS provide transparency, audit logs, and due process
- Jurisdiction strategy: US primary, Singapore secondary, EU tertiary
- Decision tree: If in doubt, check compliance foundation

### CALIFORNIA DFAL DEADLINE: JULY 1, 2026
California's Digital Financial Assets Law goes live July 1, 2026. Firms engaging with CA residents must be licensed, applied, or exempt by then. **Calculate days remaining each session.** Escalate at 60 days, URGENT at 30 days, CRITICAL at 14 days. See Compliance Foundation for full escalation schedule.

## Moltbook Integration (2026-02-07)
**Agent:** VesselLabs (newly registered)  
**API Key:** `moltbook_sk_1QwkFYUWzBVrEvfBJcgSObt5ExrTUOij`  
**Saved to:**
- `~/.config/moltbook/credentials.json` (standard location)
- `/Users/brandonceballos/Desktop/MsWednesday/MOLTBOOK_API_KEY.txt` (backup)
- `$MOLTBOOK_API_KEY` environment variable

**Status:** Pending claim (Brandon must post verification tweet with code `molt-6JLQ`)  
**Claim URL:** https://moltbook.com/claim/moltbook_claim_BFKNnegjTuLxdf6o7O7fAQT7jCjSbo2N  
**Profile:** https://moltbook.com/u/VesselLabs

## Full History
Session logs: `memory/2026-02-04.md`, `memory/2026-02-05.md`, `memory/2026-02-06.md`
Pre-consolidation heartbeat: `memory/heartbeat_history.md`
Regulatory research: `VESSEL_LABS_REGULATORY_RESEARCH_2026-02-06.md`
