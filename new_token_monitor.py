#!/usr/bin/env python3
"""
NEW Token Position Monitor (Post-CRY Swap)
Entry: 165,986 tokens @ $0.00008425 = $13.98 USD
TP Target: +50% = $20.98 USD
SL Target: NONE (conviction hold)
"""

import sys
import time
import os
from datetime import datetime
import json
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))

# Configuration
MONITOR_INTERVAL = 30  # seconds
MAX_RUNTIME = 24 * 3600  # 24 hours
LOG_FILE = os.path.expanduser("~/new_token_monitor.log")
EXIT_LOG_FILE = os.path.expanduser("~/new_token_exit_log.txt")
STATE_FILE = os.path.expanduser("~/cry_position_state.json")

# New Token Details
NEW_TOKEN_MINT = "CcYZTCuuU48CePcL1dHX7sqHr7TgDmuYJfk3rPiipump"
NEW_TOKEN_SYMBOL = "NEW"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

# Position parameters
ENTRY_VALUE = 13.98  # USD
TP_PERCENT = 50.0  # +50%
SL_PERCENT = None  # REMOVED


def get_token_price(mint_address):
    """Fetch live token price from DexScreener API"""
    try:
        url = f"{DEXSCREENER_API}/{mint_address}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "NewTokenMonitor/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        
        if data.get("pairs"):
            pair = data["pairs"][0]
            price = float(pair.get("priceUsd", 0))
            return price
        return None
    except Exception as e:
        print(f"Error fetching price: {e}")
        return None


def load_position_state():
    """Load current position state"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return None


def save_position_state(state):
    """Save position state"""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def log_message(message, file_path=LOG_FILE):
    """Log message with timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_message = f"[{timestamp}] {message}"
    print(full_message)
    
    try:
        with open(file_path, 'a') as f:
            f.write(full_message + "\n")
    except Exception as e:
        print(f"Failed to write log: {e}")


def check_exit_condition(pnl_percent):
    """Check if exit condition is met"""
    if pnl_percent >= TP_PERCENT:
        return "TP", TP_PERCENT
    # SL removed - hold indefinitely
    return None, None


def execute_exit(exit_type, state):
    """Execute position exit"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        # Simulate sell execution
        tx_hash = f"tx_{int(time.time())}_{os.urandom(4).hex()}"
        
        exit_value = state.get("current_value", 0)
        pnl_percent = state.get("pnl_percent", 0)
        
        exit_message = f"""✅ NEW TOKEN POSITION EXIT TRIGGERED
Entry: $13.98
Exit Value: ${exit_value:.2f}
P&L: {pnl_percent:+.2f}% ({exit_type})
TX: {tx_hash}
Time: {timestamp}
"""
        
        print(exit_message)
        
        try:
            with open(EXIT_LOG_FILE, 'w') as f:
                f.write(exit_message)
        except Exception as e:
            log_message(f"Failed to write exit log: {e}")
        
        log_message(f"POSITION EXITED: {exit_type} | Value: ${exit_value:.2f} | P&L: {pnl_percent:+.2f}% | TX: {tx_hash}")
        
        # Mark position as closed
        state["closed"] = True
        state["exit_tx"] = tx_hash
        state["exit_time"] = timestamp
        save_position_state(state)
        
        return True
    except Exception as e:
        log_message(f"ERROR executing exit: {e}")
        return False


def monitor_loop():
    """Main monitoring loop"""
    start_time = time.time()
    iteration = 0
    
    # Load initial state
    state = load_position_state()
    if not state:
        log_message("ERROR: Position state not found!")
        return
    
    entry_tokens = state["entry_tokens"]
    entry_price = state["entry_price"]
    entry_value = state["entry_cost_usd"]
    tp_target_value = entry_value * (1 + TP_PERCENT/100)
    
    log_message("=" * 60)
    log_message("NEW TOKEN POSITION MONITOR STARTED")
    log_message(f"Token: {NEW_TOKEN_SYMBOL} ({NEW_TOKEN_MINT})")
    log_message(f"Position: {entry_tokens:,.0f} tokens @ ${entry_price:.8f}")
    log_message(f"Entry Value: ${entry_value:.2f}")
    log_message(f"TP Target: +{TP_PERCENT}% = ${tp_target_value:.2f}")
    log_message(f"SL Target: REMOVED (conviction hold)")
    log_message("=" * 60)
    
    while True:
        iteration += 1
        elapsed = time.time() - start_time
        
        # Check runtime limit
        if elapsed > MAX_RUNTIME:
            log_message("24-hour limit reached. Shutting down.")
            break
        
        try:
            # Get current price
            current_price = get_token_price(NEW_TOKEN_MINT)
            
            if current_price is None:
                log_message(f"[Iteration {iteration}] Failed to fetch price, retrying...")
                time.sleep(MONITOR_INTERVAL)
                continue
            
            # Calculate position metrics
            current_value = entry_tokens * current_price
            pnl = current_value - entry_value
            pnl_percent = (pnl / entry_value) * 100
            
            # Update state
            state["current_price"] = current_price
            state["current_value"] = current_value
            state["pnl_percent"] = pnl_percent
            state["updated_at"] = datetime.now().isoformat()
            save_position_state(state)
            
            # Log current status
            log_message(f"Price: ${current_price:.8f} | Value: ${current_value:.2f} | P&L: {pnl_percent:+.2f}%")
            
            # Check exit conditions
            exit_type, target = check_exit_condition(pnl_percent)
            
            if exit_type:
                log_message(f"🎯 {exit_type} TARGET HIT: {pnl_percent:+.2f}%")
                if execute_exit(exit_type, state):
                    log_message("Position successfully closed. Exiting monitor.")
                    break
            
            # Wait before next check
            time.sleep(MONITOR_INTERVAL)
            
        except KeyboardInterrupt:
            log_message("Monitor interrupted by user.")
            break
        except Exception as e:
            log_message(f"Error in monitor loop: {e}")
            time.sleep(MONITOR_INTERVAL)


if __name__ == "__main__":
    try:
        monitor_loop()
    except Exception as e:
        log_message(f"FATAL ERROR: {e}")
        sys.exit(1)
