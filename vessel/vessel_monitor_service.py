#!/usr/bin/env python3
"""
Vessel Position Monitor Service — Agent-Agnostic Framework
Runs independently. ANY agent in the vessel can read live position metrics.
"""

import os
import json
import time
import requests
from datetime import datetime
from pathlib import Path

class PositionMonitor:
    """Agent-agnostic position monitoring for the vessel."""
    
    def __init__(self, token_mint, entry_value_sol, entry_price=None):
        self.token_mint = token_mint
        self.entry_value_sol = entry_value_sol
        self.entry_price = entry_price
        self.state_file = Path.home() / 'position_state.json'
        self.log_file = Path.home() / 'monitor.log'
        
    def get_current_state(self):
        """Read live position state (called by agents)."""
        if self.state_file.exists():
            with open(self.state_file) as f:
                return json.load(f)
        return {}
    
    def fetch_price(self):
        """Fetch token price from DexScreener."""
        try:
            resp = requests.get(
                f'https://api.dexscreener.com/latest/dex/tokens/{self.token_mint}',
                timeout=5
            )
            data = resp.json()
            if data.get('pairs'):
                return float(data['pairs'][0].get('priceUsd', 0))
        except Exception as e:
            self._log(f"Price fetch error: {e}")
        return 0
    
    def fetch_sol_price(self):
        """Fetch SOL/USD rate from CoinGecko."""
        try:
            resp = requests.get(
                'https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd',
                timeout=5
            )
            return resp.json()['solana']['usd']
        except Exception as e:
            self._log(f"SOL price fetch error: {e}")
            return 70  # Fallback
    
    def get_token_balance(self):
        """Read token balance from wallet state file."""
        wallet_state = Path.home() / 'cry_position_state.json'
        if wallet_state.exists():
            try:
                with open(wallet_state) as f:
                    data = json.load(f)
                    return data.get('token_balance', 0)
            except:
                pass
        return 0
    
    def calculate_metrics(self):
        """Calculate live P&L metrics."""
        price = self.fetch_price()
        sol_price = self.fetch_sol_price()
        balance = self.get_token_balance()
        
        if balance == 0 or price == 0:
            return None
        
        # Calculate current value
        current_value_usd = balance * price
        current_value_sol = current_value_usd / sol_price if sol_price > 0 else 0
        
        # Calculate P&L
        entry_value_usd = self.entry_value_sol * sol_price
        pnl_usd = current_value_usd - entry_value_usd
        pnl_percent = (pnl_usd / entry_value_usd * 100) if entry_value_usd > 0 else 0
        
        return {
            'current_price': price,
            'current_value_sol': current_value_sol,
            'current_value_usd': current_value_usd,
            'token_balance': balance,
            'pnl_percent': pnl_percent,
            'pnl_usd': pnl_usd,
            'entry_value_usd': entry_value_usd,
            'sol_price': sol_price,
        }
    
    def update_state(self, metrics, agent_controlling=None, tp_target=None, sl_target=None):
        """Write live state (called by monitoring loop)."""
        if not metrics:
            return
        
        state = {
            'token_mint': self.token_mint,
            'entry_value_sol': self.entry_value_sol,
            'entry_price': self.entry_price,
            'current_price': metrics['current_price'],
            'current_value_sol': metrics['current_value_sol'],
            'current_value_usd': metrics['current_value_usd'],
            'token_balance': metrics['token_balance'],
            'pnl_percent': metrics['pnl_percent'],
            'pnl_usd': metrics['pnl_usd'],
            'tp_target_usd': tp_target,
            'sl_target_usd': sl_target,
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'status': 'MONITORING',
            'agent_controlling': agent_controlling or 'vessel-service',
        }
        
        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=2)
    
    def _log(self, msg):
        """Log to monitor.log."""
        timestamp = datetime.utcnow().isoformat()
        with open(self.log_file, 'a') as f:
            f.write(f"{timestamp} | {msg}\n")


def run_monitor_service(token_mint, entry_value_sol, entry_price=None, poll_interval=30):
    """Run the vessel monitor service (background loop)."""
    monitor = PositionMonitor(token_mint, entry_value_sol, entry_price)
    
    print(f"🚀 Vessel Monitor Service Started")
    print(f"   Token: {token_mint}")
    print(f"   Entry: {entry_value_sol} SOL")
    print(f"   Poll Interval: {poll_interval}s")
    print(f"   State File: ~/position_state.json")
    print(f"   Log File: ~/monitor.log\n")
    
    poll_count = 0
    while True:
        try:
            metrics = monitor.calculate_metrics()
            if metrics:
                monitor.update_state(metrics)
                poll_count += 1
                print(f"Poll #{poll_count}: ${metrics['current_value_usd']:.2f} | P&L: {metrics['pnl_percent']:+.2f}%")
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\n✅ Monitor service stopped")
            break
        except Exception as e:
            print(f"Error in monitor loop: {e}")
            time.sleep(poll_interval)


if __name__ == '__main__':
    # Example: Run monitor for the current token
    run_monitor_service(
        token_mint='CcYZTCuuU48CePcL1dHX7sqHr7TgDmuYJfk3rPiipump',
        entry_value_sol=0.1565,
        poll_interval=30
    )
