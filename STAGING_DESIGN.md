# Staging Environment Architecture Design

## Purpose
A staging environment that mirrors production for safe testing of security changes,
new agent capabilities, and system updates before they reach live trading wallets.

## Architecture

### Separate Relay Server
- **Port:** 8778 (production: 8777)
- **Config toggle:** `ENVIRONMENT = "staging" | "production"` in `config.py`
- **DB:** `vessel_tasks_staging.db` (separate SQLite file)
- **Audit log:** `relay_audit_staging.log`

### Network Isolation
- Staging relay MUST NOT connect to production SXAN API (`localhost:5001`)
- Staging uses a mock SXAN API or devnet-connected instance
- Config: `SXAN_API_BASE = "http://localhost:5002"` for staging

### Devnet Wallets
- All agent wallets in staging use Solana devnet
- Separate keypairs generated for staging (never share with production)
- Airdrop script for funding staging wallets with devnet SOL

### Agent Isolation
- Staging spawn gates stored in separate directory: `~/.staging_spawn_gates/`
- Staging agent contexts: `VesselProject/agent_contexts_staging/`
- Staging spawn secret: `~/.staging_spawn_secret`

### Phone/Vessel
- Staging vessel connects to port 8778
- `VESSEL_SERVER_PORT = "8778"` in staging config
- Same phone can run staging and production (different Termux sessions)

## Config Toggle Implementation

```python
# config.py additions
ENVIRONMENT = os.getenv("VESSEL_ENVIRONMENT", "production")

if ENVIRONMENT == "staging":
    SERVER_PORT = "8778"
    DB_SUFFIX = "_staging"
    SXAN_API_BASE = "http://localhost:5002"
    SPAWN_SECRET_PATH = Path.home() / '.staging_spawn_secret'
    AGENT_CONTEXTS_DIR = Path(PROJECT_ROOT) / 'agent_contexts_staging'
else:
    # Current production defaults
    pass
```

## Safety Rules
1. Staging config MUST error on startup if `SXAN_API_BASE` points to production port
2. Staging wallet keypairs MUST be in a separate directory from production
3. Staging audit logs MUST be clearly labeled to prevent confusion
4. No cross-environment spawn gates (staging gates cannot authorize production)

## Implementation Priority
- Phase 2 deliverable (after Phase 1 security hardening is verified)
- Requires simultaneous Mac + phone config update
- Test with one agent before full staging deployment
