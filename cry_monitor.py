#!/usr/bin/env python3
"""
CRY Token Position Monitor
Autonomous monitoring loop for CRY token position
Entry: 28,274 tokens at $0.0006007 ($18 USD)
TP: +50% = $27 | SL: -30% = $12.60
"""

import sys
import time
import os
from datetime import datetime
import json

# Add workspace to path
sys.path.insert(0, os.path.dirname(__file__))

import sxan_wallet

# Configuration
MONITOR_INTERVAL = 30  # seconds
MAX_RUNTIME = 24 * 3600  # 24 hours
LOG_FILE = os.path.expanduser("~/cry_monitor.log")
EXIT_LOG_FILE = os.path.expanduser("~/cry_exit_log.txt")
STATE_FILE = os.path.expanduser("~/cry_position_state.json")

# Position targets
TP_PERCENT = 50.0  # +50%
SL_PERCENT = None  # REMOVED - No automatic stop loss


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
    # SL removed - hold position indefinitely
    return None, None


def execute_exit(exit_type, state):
    """Execute position exit"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Execute sell via wallet
    try:
        sell_result = sxan_wallet.sell(percent=100)
        tx_hash = sell_result.get("tx", "UNKNOWN")
        
        exit_value = state.get("current_value", 0)
        pnl_percent = state.get("pnl_percent", 0)
        entry_cost = state.get("entry_cost_usd", 18.0)
        
        exit_message = f"""✅ CRY POSITION EXIT TRIGGERED
Entry: 0.199 SOL ($18)
Exit Value: ${exit_value:.2f}
P&L: {pnl_percent:+.2f}% ({exit_type})
TX: {tx_hash}
Time: {timestamp}
"""
        
        # Log to console
        print(exit_message)
        
        # Write to exit log
        try:
            with open(EXIT_LOG_FILE, 'w') as f:
                f.write(exit_message)
        except Exception as e:
            log_message(f"Failed to write exit log: {e}")
        
        # Also log to main monitor log
        log_message(f"POSITION EXITED: {exit_type} | Value: ${exit_value:.2f} | P&L: {pnl_percent:+.2f}% | TX: {tx_hash}")
        
        return True
    except Exception as e:
        log_message(f"ERROR executing exit: {e}")
        return False


def monitor_loop():
    """Main monitoring loop"""
    start_time = time.time()
    iteration = 0
    
    log_message("=" * 60)
    log_message("CRY TOKEN POSITION MONITOR STARTED")
    log_message(f"Token: 9CaWKwDJPFTrkJuk5dj1Vyc2TBse9CjQFmomVGkrpump")
    log_message(f"Position: 28,274 CRY @ $0.0006007 = $18 USD")
    log_message(f"TP Target: +{TP_PERCENT}% = $27 USD")
    log_message(f"SL Target: REMOVED (no automatic stop loss)")
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
            current_price = sxan_wallet.get_cry_price()
            
            if current_price is None:
                log_message(f"[Iteration {iteration}] Failed to fetch price, retrying...")
                time.sleep(MONITOR_INTERVAL)
                continue
            
            # Update position state
            state = sxan_wallet.update_position(current_price)
            
            current_value = state["current_value"]
            pnl_percent = state["pnl_percent"]
            
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
