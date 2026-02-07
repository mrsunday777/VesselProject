"""
SXAN Wallet - Solana Token Position Manager
Handles balance queries, price fetching, and position execution
"""

import json
import os
import time
from datetime import datetime
import urllib.request
import urllib.error

WALLET_STATE_FILE = os.path.expanduser("~/cry_position_state.json")
CRY_MINT = "9CaWKwDJPFTrkJuk5dj1Vyc2TBse9CjQFmomVGkrpump"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"


def get_cry_price():
    """Fetch live CRY token price from DexScreener API"""
    try:
        url = f"{DEXSCREENER_API}/{CRY_MINT}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "CRY-Monitor/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            
        if data.get("pairs"):
            # Get the first pair (highest volume)
            pair = data["pairs"][0]
            price = float(pair.get("priceUsd", 0))
            return price
        return None
    except Exception as e:
        print(f"Error fetching price: {e}")
        return None


def status():
    """Get current wallet status for CRY token"""
    return {
        "balance": 28274,  # Entry balance
        "token": CRY_MINT,
        "symbol": "CRY",
        "timestamp": datetime.now().isoformat()
    }


def sell(percent=100):
    """
    Sell position (simulated)
    percent: percentage of position to sell (100 = 100%)
    """
    state = load_position_state()
    timestamp = datetime.now().isoformat()
    
    tx_hash = f"tx_{int(time.time())}_{os.urandom(4).hex()}"
    
    result = {
        "status": "success",
        "tx": tx_hash,
        "percent_sold": percent,
        "timestamp": timestamp,
        "exit_price": state.get("current_price", 0),
        "exit_value": state.get("current_value", 0),
        "pnl_percent": state.get("pnl_percent", 0)
    }
    
    # Mark position as closed
    state["closed"] = True
    state["exit_tx"] = tx_hash
    state["exit_time"] = timestamp
    save_position_state(state)
    
    return result


def load_position_state():
    """Load position state from file"""
    if os.path.exists(WALLET_STATE_FILE):
        try:
            with open(WALLET_STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    
    return {
        "entry_tokens": 28274,
        "entry_cost_sol": 0.199,
        "entry_price": 0.0006007,
        "entry_cost_usd": 18.0,
        "current_tokens": 28274,
        "current_price": 0,
        "current_value": 0,
        "pnl_percent": 0,
        "tp_target": 27.0,
        "sl_target": None,  # REMOVED - No automatic stop loss
        "closed": False,
        "created_at": datetime.now().isoformat()
    }


def save_position_state(state):
    """Save position state to file"""
    with open(WALLET_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def update_position(current_price):
    """Update position with current price"""
    state = load_position_state()
    
    current_value = state["current_tokens"] * current_price
    pnl = current_value - state["entry_cost_usd"]
    pnl_percent = (pnl / state["entry_cost_usd"]) * 100
    
    state["current_price"] = current_price
    state["current_value"] = current_value
    state["pnl_percent"] = pnl_percent
    state["updated_at"] = datetime.now().isoformat()
    
    save_position_state(state)
    
    return state
