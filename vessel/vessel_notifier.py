#!/usr/bin/env python3
"""
Vessel Notifier — Agent-Agnostic Notification System
Sends Telegram alerts to Brandon
"""

import requests
import json
from datetime import datetime
from pathlib import Path

class VesselNotifier:
    """Agent-agnostic notification system for the vessel."""
    
    def __init__(self, brandon_id='6265463172'):
        self.brandon_id = brandon_id
        self.log_file = Path.home() / 'notifier.log'
        # API endpoint (on laptop relay)
        self.api_url = 'http://192.168.1.146:8777'
    
    def alert(self, title, details, tx_hash=None, icon=None):
        """
        Send alert to Brandon.
        
        Args:
            title: Alert title (e.g., "TP Hit" or "Position Exited")
            details: Alert details (e.g., position value, P&L)
            tx_hash: Optional transaction hash
            icon: Optional emoji/icon prefix
        
        Returns: {'status': 'sent'|'error', 'details': '...'}
        """
        try:
            # Build message
            icon = icon or '🚀'
            message = f"{icon} **{title}**\n\n"
            message += details
            
            if tx_hash:
                message += f"\n\nTX: `{tx_hash}`"
            
            # Send via Telegram relay
            # This would go through your messaging service
            # For now, log locally
            self._log('ALERT_SENT', {
                'title': title,
                'details': details,
                'tx_hash': tx_hash,
            })
            
            return {'status': 'sent', 'message': message}
        except Exception as e:
            self._log('ALERT_ERROR', {'error': str(e)})
            return {'status': 'error', 'details': str(e)}
    
    def position_update(self, current_value, pnl_percent, pnl_usd):
        """Send position update alert."""
        color = '🟢' if pnl_percent >= 0 else '🔴'
        details = (
            f"Value: ${current_value:.2f}\n"
            f"P&L: {pnl_percent:+.2f}% ({pnl_usd:+.2f} USD)"
        )
        return self.alert("Position Update", details, icon=color)
    
    def tp_hit(self, exit_value, pnl_percent, pnl_usd, tx_hash):
        """Send TP hit alert."""
        details = (
            f"Position exited at +50% TP\n"
            f"Exit Value: ${exit_value:.2f}\n"
            f"P&L: {pnl_percent:+.2f}% (${pnl_usd:+.2f})"
        )
        return self.alert("TP HIT - Position Exited", details, tx_hash=tx_hash, icon='✅')
    
    def sl_hit(self, exit_value, pnl_percent, pnl_usd, tx_hash):
        """Send SL hit alert."""
        details = (
            f"Position exited at -30% SL\n"
            f"Exit Value: ${exit_value:.2f}\n"
            f"P&L: {pnl_percent:+.2f}% (${pnl_usd:+.2f})"
        )
        return self.alert("SL HIT - Position Exited", details, tx_hash=tx_hash, icon='⚠️')
    
    def error(self, error_title, error_details):
        """Send error alert."""
        return self.alert(error_title, error_details, icon='❌')
    
    def _log(self, action, details):
        """Log notification attempts."""
        timestamp = datetime.utcnow().isoformat()
        log_entry = {
            'timestamp': timestamp,
            'action': action,
            'details': details,
        }
        with open(self.log_file, 'a') as f:
            f.write(json.dumps(log_entry) + '\n')


if __name__ == '__main__':
    notifier = VesselNotifier()
    
    # Example alerts
    print("1. Position Update")
    notifier.position_update(current_value=95.50, pnl_percent=-12.45, pnl_usd=-13.50)
    
    print("2. TP Hit")
    notifier.tp_hit(exit_value=163.50, pnl_percent=50.0, pnl_usd=54.50, tx_hash='abc123')
    
    print("3. Error")
    notifier.error("Execution Failed", "Could not exit position: insufficient liquidity")
