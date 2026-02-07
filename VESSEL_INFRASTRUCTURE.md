# VesselProject Infrastructure — Agent-Agnostic Framework

**Date:** 2026-02-04  
**Architecture:** Vessel Office Model  
**Status:** BUILDING

---

## Philosophy

The vessel is not an agent workspace — it's an **office**.

Any agent spawned into the vessel inherits a complete execution environment with:
- Position monitoring framework
- Price polling infrastructure  
- State management
- Execution hooks
- Notification system

Agents don't build tools. They enter the office, sit down, and use the already-built tools.

---

## Vessel-Level Infrastructure (Pre-built)

### 1. Position Monitor Service
**Location:** `~/vessel_monitor_service.py`

Runs continuously. Tracks ANY token position in the workspace.

```python
monitor = PositionMonitor(token_mint, entry_value_sol, entry_price)

# Service writes live state to:
# ~/position_state.json (readable by ANY agent in vessel)
# ~/monitor.log (transaction + debug log)

# Any agent can query:
state = monitor.get_current_state()  # Returns live metrics
state = monitor.get_history()        # Returns polling history
```

**Outputs (Updated every 30s):**
- `current_price` (DexScreener API)
- `current_value_sol` (calculated)
- `current_value_usd` (calculated)
- `pnl_percent` (calculated)
- `pnl_usd` (calculated)
- `timestamp`
- `status` (MONITORING, TP_HIT, SL_HIT, MANUAL_EXIT)

### 2. Position State File (`~/position_state.json`)
**Universal interface.** ANY agent reads here for current metrics.

```json
{
  "token_mint": "CcYZTCuu...",
  "entry_value_sol": 0.1565,
  "entry_price": 0.001408,
  "current_price": 0.00009555,
  "current_value_sol": 0.0133,
  "current_value_usd": 17.90,
  "pnl_percent": -83.58,
  "pnl_usd": -91.10,
  "tp_target_usd": null,
  "sl_target_usd": null,
  "timestamp": "2026-02-04T11:30:00Z",
  "status": "MONITORING",
  "agent_controlling": "CP9"
}
```

### 3. Execution Framework (`~/vessel_executor.py`)
**Agent-agnostic execution hooks.**

```python
executor = VesselExecutor()

# Any agent can call:
executor.exit_position(percent=100)           # Sell N% of position
executor.exit_if_triggered(tp_value, sl_value)  # Check + execute trigger
executor.notify_owner(message, tx_hash)       # Alert Brandon
executor.log_action(action, details)          # Write to audit trail
```

### 4. Price Polling Service (`~/price_poller.py`)
**DexScreener + CoinGecko integration.**

Runs in background, fetches:
- Token price (DexScreener API)
- SOL/USD rate (CoinGecko)
- Updates state file every 30s

Any agent can read latest prices from state file without making API calls.

### 5. Notification System (`~/vessel_notifier.py`)
**Telegram alerts to Brandon.**

```python
notifier = VesselNotifier()
notifier.alert(title, details, tx_hash)  # Sends to @VinnDiesell
```

---

## Agent Workflow (Any Agent)

### Entering the Vessel
```
1. MsWednesday spawns agent with: token_mint, entry_value_sol
2. Agent reads ~/position_state.json
3. Agent inherits: monitor service + executor + notifier
4. Agent operates autonomously within vessel bounds
```

### Working in the Vessel
```python
# Agent doesn't write its own monitoring — it uses vessel service
state = read_position_state()  # Get live metrics

# If condition met (TP/SL/other):
executor.exit_position(percent=100)      # Vessel handles execution
notifier.alert("Exit triggered", details) # Vessel handles notification

# Agent can work on anything — monitoring runs in background
```

### Leaving the Vessel
```
1. Agent completes task (or timeout)
2. Position state remains accessible to next agent
3. Monitoring continues (agent-agnostic)
4. MsWednesday can spawn new agent to inherit same position
```

---

## Migration Path

### Current State (CP9-specific)
- CP9 writes its own monitoring loop
- CP9 maintains own state files
- CP9 handles own execution logic
- Phone display reads CP9's files

### New State (Vessel-level)
- Vessel monitoring service runs independently
- CP9 just reads state + calls executor hooks
- ANY agent spawned into vessel uses same tools
- Phone display reads vessel state (agent-agnostic)

---

## Building Sequence

1. **Phase 1:** Extract monitoring → `vessel_monitor_service.py`
2. **Phase 2:** Extract execution → `vessel_executor.py`
3. **Phase 3:** Extract notifications → `vessel_notifier.py`
4. **Phase 4:** Integrate price poller → `price_poller.py`
5. **Phase 5:** Migrate CP9 to use framework (no more internal loops)
6. **Phase 6:** Test with new agent spawn

---

## Long-term Benefits

**Today:** CP9 custom monitoring  
**Tomorrow:** Any agent can monitor any position  
**Next:** Multiple agents working different positions in vessel simultaneously  
**Future:** Vessel is a fully-featured autonomous trading office

---

## Security Model

- **Vessel isolation:** No access to main laptop
- **State file:** Read by any agent, written only by vessel service
- **Executor:** Validates calls, logs all actions
- **Notifications:** Only vessel → Brandon (agents can't bypass)
- **Agent governance:** MsWednesday controls spawn + revocation

---

**Status:** DESIGN COMPLETE — Ready to build Phase 1
