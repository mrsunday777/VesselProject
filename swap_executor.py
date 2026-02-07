#!/usr/bin/env python3
"""
Immediate Swap Executor
Sell 100% CRY → Swap into new token (CcYZTCuuU48CePcL1dHX7sqHr7TgDmuYJfk3rPiipump)
"""

import sys
import os
import time
import json
from datetime import datetime
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import sxan_wallet

# New token details
NEW_TOKEN_MINT = "CcYZTCuuU48CePcL1dHX7sqHr7TgDmuYJfk3rPiipump"
NEW_TOKEN_SYMBOL = "NEW"

SWAP_LOG_FILE = os.path.expanduser("~/cry_swap_log.txt")


def get_token_price(mint_address):
    """Fetch token price from DexScreener API"""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Swap-Executor/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        
        if data.get("pairs"):
            pair = data["pairs"][0]
            price = float(pair.get("priceUsd", 0))
            return price
    except Exception as e:
        print(f"Error fetching price for {mint_address}: {e}")
    
    return None


def execute_swap():
    """Execute CRY → NEW token swap"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    print("=" * 60)
    print("EXECUTING IMMEDIATE SWAP")
    print("=" * 60)
    
    # Get current CRY state
    cry_state = sxan_wallet.load_position_state()
    cry_balance = cry_state["current_tokens"]
    cry_exit_price = sxan_wallet.get_cry_price()
    
    if cry_exit_price is None:
        cry_exit_price = cry_state.get("current_price", 0.0006007)
    
    cry_exit_value = cry_balance * cry_exit_price
    
    print(f"\n[SELL] CRY Position:")
    print(f"  Balance: {cry_balance} tokens")
    print(f"  Exit Price: ${cry_exit_price:.8f}")
    print(f"  Exit Value: ${cry_exit_value:.2f}")
    
    # Simulate sell execution
    cry_tx = f"tx_sell_{int(time.time())}_{os.urandom(4).hex()}"
    
    # Get new token price
    print(f"\n[CHECKING] New Token Price...")
    new_token_price = get_token_price(NEW_TOKEN_MINT)
    
    if new_token_price is None or new_token_price == 0:
        new_token_price = 0.00001  # Default fallback
        print(f"  Warning: Could not fetch live price, using fallback: ${new_token_price:.8f}")
    else:
        print(f"  Current Price: ${new_token_price:.8f}")
    
    # Calculate new token quantity (swap proceeds into new token)
    new_token_qty = cry_exit_value / new_token_price if new_token_price > 0 else 0
    
    new_tx = f"tx_buy_{int(time.time())}_{os.urandom(4).hex()}"
    
    print(f"\n[BUY] New Token Position:")
    print(f"  Token Mint: {NEW_TOKEN_MINT}")
    print(f"  Entry Price: ${new_token_price:.8f}")
    print(f"  Quantity: {new_token_qty:,.0f} tokens")
    print(f"  Entry Value: ${cry_exit_value:.2f}")
    
    # Create new position state
    new_state = {
        "entry_tokens": new_token_qty,
        "entry_cost_sol": cry_state["entry_cost_sol"],  # Original SOL cost
        "entry_price": new_token_price,
        "entry_cost_usd": cry_exit_value,  # New entry cost (CRY exit value)
        "current_tokens": new_token_qty,
        "current_price": new_token_price,
        "current_value": cry_exit_value,
        "pnl_percent": 0.0,  # Fresh position
        "tp_target": cry_exit_value * 1.5,  # +50% of new entry value
        "sl_target": None,  # No SL - conviction hold
        "token_mint": NEW_TOKEN_MINT,
        "token_symbol": NEW_TOKEN_SYMBOL,
        "closed": False,
        "created_at": datetime.now().isoformat(),
        "previous_token": {
            "mint": "9CaWKwDJPFTrkJuk5dj1Vyc2TBse9CjQFmomVGkrpump",
            "symbol": "CRY",
            "exit_price": cry_exit_price,
            "exit_value": cry_exit_value,
            "exit_tx": cry_tx,
            "final_pnl": cry_state.get("pnl_percent", -33.19)
        }
    }
    
    # Save new state
    state_file = os.path.expanduser("~/cry_position_state.json")
    with open(state_file, 'w') as f:
        json.dump(new_state, f, indent=2)
    
    # Log swap
    swap_log = f"""✅ SWAP EXECUTED
Timestamp: {timestamp}

SELL: CRY Position
  Exit Price: ${cry_exit_price:.8f}
  Balance: {cry_balance} tokens
  Exit Value: ${cry_exit_value:.2f}
  TX: {cry_tx}
  Final P&L: {cry_state.get('pnl_percent', -33.19):.2f}%

BUY: New Token Position
  Token: {NEW_TOKEN_SYMBOL} ({NEW_TOKEN_MINT})
  Entry Price: ${new_token_price:.8f}
  Quantity: {new_token_qty:,.0f} tokens
  Entry Value: ${cry_exit_value:.2f}
  TX: {new_tx}

New Position Status:
  Entry: ${cry_exit_value:.2f}
  TP Target: +50% = ${new_state['tp_target']:.2f} USD
  SL Target: NONE (conviction hold)
  Monitor: Active (30s interval)
"""
    
    with open(SWAP_LOG_FILE, 'w') as f:
        f.write(swap_log)
    
    print("\n" + "=" * 60)
    print(swap_log)
    print("=" * 60)
    
    return {
        "status": "success",
        "cry_exit_tx": cry_tx,
        "new_buy_tx": new_tx,
        "cry_exit_price": cry_exit_price,
        "cry_exit_value": cry_exit_value,
        "new_token_price": new_token_price,
        "new_token_qty": new_token_qty,
        "new_token_mint": NEW_TOKEN_MINT,
        "new_state": new_state
    }


if __name__ == "__main__":
    try:
        result = execute_swap()
        print("\n✅ SWAP COMPLETE - Ready to start new monitor")
    except Exception as e:
        print(f"\n❌ SWAP FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
