#!/usr/bin/env python3
"""
Vessel Execution Framework — Agent-Agnostic Execution Hooks
Allows ANY agent in the vessel to execute trades, exits, notifications
"""

import os
import json
import sys
import requests
from datetime import datetime
from pathlib import Path

class VesselExecutor:
    """Agent-agnostic execution framework for the vessel."""
    
    def __init__(self):
        self.state_file = Path.home() / 'position_state.json'
        self.log_file = Path.home() / 'executor.log'
        self.laptop_relay_url = "http://192.168.1.146:8777"
        self.vessel_secret = os.getenv('VESSEL_SECRET', 'mrsunday')
        
    def _log(self, action, details):
        """Log all executor actions."""
        timestamp = datetime.utcnow().isoformat()
        log_entry = {
            'timestamp': timestamp,
            'action': action,
            'details': details,
        }
        with open(self.log_file, 'a') as f:
            f.write(json.dumps(log_entry) + '\n')
    
    def _update_state(self, status):
        """Update position state (status only)."""
        try:
            with open(self.state_file) as f:
                state = json.load(f)
            state['status'] = status
            state['timestamp'] = datetime.utcnow().isoformat() + 'Z'
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except:
            pass
    
    def exit_position(self, percent=100):
        """
        Exit N% of position via laptop wallet API.
        
        Called by: Any agent in vessel
        Returns: {'status': 'success'|'error', 'tx_hash': '...', 'details': '...'}
        """
        try:
            state = self._get_state()
            if not state:
                return {'status': 'error', 'details': 'No position state found'}
            
            token_mint = state.get('token_mint')
            if not token_mint:
                return {'status': 'error', 'details': 'No token mint in state'}
            
            # Call laptop wallet API to execute exit
            payload = {
                'token_mint': token_mint,
                'percent': percent,
                'slippage_bps': 75,
            }
            
            resp = requests.post(
                'http://localhost:5001/api/agent-wallet/sell/MsWednesday',
                json=payload,
                headers={'Authorization': f'Bearer {os.getenv("AGENT_API_TOKEN")}'},
                timeout=30
            )
            
            if resp.status_code == 200:
                result = resp.json()
                tx_hash = result.get('signature', 'unknown')
                self._update_state('POSITION_EXITED')
                self._log('EXIT_EXECUTED', {'percent': percent, 'tx_hash': tx_hash})
                return {
                    'status': 'success',
                    'tx_hash': tx_hash,
                    'exit_value_usd': state.get('current_value_usd'),
                    'pnl_usd': state.get('pnl_usd'),
                }
            else:
                error = resp.text
                self._log('EXIT_FAILED', {'error': error})
                return {'status': 'error', 'details': error}
        except Exception as e:
            self._log('EXIT_ERROR', {'error': str(e)})
            return {'status': 'error', 'details': str(e)}
    
    def check_trigger(self, tp_value=None, sl_value=None):
        """
        Check if TP or SL condition is met.
        
        Returns: {'triggered': True|False, 'type': 'TP'|'SL'|None}
        """
        state = self._get_state()
        if not state:
            return {'triggered': False, 'type': None}
        
        current_value = state.get('current_value_usd', 0)
        
        if tp_value and current_value >= tp_value:
            return {'triggered': True, 'type': 'TP', 'value': current_value}
        
        if sl_value and current_value <= sl_value:
            return {'triggered': True, 'type': 'SL', 'value': current_value}
        
        return {'triggered': False, 'type': None}
    
    def exit_if_triggered(self, tp_value=None, sl_value=None):
        """
        Check conditions and exit if triggered.
        
        Returns: {'executed': True|False, 'trigger_type': 'TP'|'SL'|None, ...}
        """
        trigger = self.check_trigger(tp_value, sl_value)
        
        if trigger['triggered']:
            result = self.exit_position(percent=100)
            return {
                'executed': True,
                'trigger_type': trigger['type'],
                'exit_result': result,
            }
        
        return {'executed': False, 'trigger_type': None}
    
    def notify_owner(self, title, details, tx_hash=None):
        """
        Send Telegram notification to Brandon.
        
        Called by: Any agent in vessel
        """
        try:
            message = f"**{title}**\n\n{details}"
            if tx_hash:
                message += f"\n\nTX: {tx_hash}"
            
            requests.post(
                'http://localhost:5001/api/notify',
                json={'user_id': '6265463172', 'message': message},
                timeout=10
            )
            self._log('NOTIFICATION_SENT', {'title': title})
        except Exception as e:
            self._log('NOTIFICATION_ERROR', {'error': str(e)})
    
    def _get_state(self):
        """Read current position state."""
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except:
                return None
        return None


def agent_exit_example():
    """Example: How an agent would use the executor."""
    executor = VesselExecutor()
    
    print("Example Agent Workflow:")
    print("1. Read current state")
    state = executor._get_state()
    print(f"   Current value: ${state.get('current_value_usd', 0):.2f}")
    
    print("2. Check if TP hit (TP = $163.50)")
    trigger = executor.check_trigger(tp_value=163.50)
    if trigger['triggered']:
        print(f"   ✅ TP triggered at ${trigger['value']}")
        result = executor.exit_position(percent=100)
        print(f"   Exit TX: {result.get('tx_hash')}")
        executor.notify_owner("TP Hit", f"Exited at ${state.get('current_value_usd')}")
    else:
        print(f"   ❌ TP not yet hit (current: ${state.get('current_value_usd')})")


if __name__ == '__main__':
    agent_exit_example()
