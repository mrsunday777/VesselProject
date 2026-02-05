#!/usr/bin/env python3
"""
Vessel Agent Toolkit — Unified tools for phone-side agents.

ALL communication with the Mac goes through the relay server.
Agents never call the Mac's internal APIs directly.

Tools:
    state()             — Get live position state (read-only)
    buy()               — Buy token via relay → SXAN API (multi-agent)
    sell()              — Exit position via relay → SXAN API (multi-agent)
    wallet_status()     — Get wallet balance, holdings, enabled status
    transactions()      — Get recent trade history for an agent
    my_positions()      — Get only this agent's positions from state
    notify()            — Send Telegram alert to Brandon via relay
    telegram_feed()     — Tokens from monitored Telegram chats
    almost_graduated()  — Tokens approaching graduation
    new_launches()      — New pump.fun token launches
    catalysts()         — Trending events (Google Trends, News, Reddit)
    check_trigger()     — Check TP/SL against live state
    exit_if_triggered() — Check + sell in one call

Usage:
    from vessel_tools import VesselTools
    tools = VesselTools()

    # Read position
    state = tools.state()

    # Check and exit on trigger
    result = tools.exit_if_triggered(tp_pct=50.0, sl_pct=-30.0)

    # Notify Brandon
    tools.notify("TP Hit", "Exited AGENT at +50%", tx_hash="abc123")
"""

import os
import sys
import json
import time
from datetime import datetime

# Try to load config from parent dir
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from config import VESSEL_SECRET, SERVER_PORT
except ImportError:
    VESSEL_SECRET = os.getenv('VESSEL_SECRET', '')
    SERVER_PORT = os.getenv('VESSEL_SERVER_PORT', '8777')

# HTTP client (stdlib — no pip install needed in Termux)
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# Default relay server (Mac IP on home network)
DEFAULT_SERVER_IP = os.getenv('VESSEL_SERVER_IP', '192.168.1.146')


class VesselTools:
    """Agent toolkit — all operations routed through relay server."""

    def __init__(self, server_ip=None, name=None):
        self.relay_url = f"http://{server_ip or DEFAULT_SERVER_IP}:{SERVER_PORT}"
        self.secret = VESSEL_SECRET
        self.name = name  # Agent identity — sent as X-Requester for audit attribution
        self.log_file = os.path.expanduser('~/vessel_agent.log')

    def _request(self, method, path, body=None):
        """Make HTTP request to relay server."""
        url = f"{self.relay_url}{path}"
        headers = {'Authorization': self.secret}
        if self.name:
            headers['X-Requester'] = self.name

        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        else:
            data = None

        req = Request(url, data=data, headers=headers, method=method)

        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            error_body = e.read().decode() if e.fp else str(e)
            self._log('REQUEST_ERROR', {'url': path, 'status': e.code, 'error': error_body})
            return {'status': 'error', 'error': error_body, 'http_status': e.code}
        except (URLError, Exception) as e:
            self._log('REQUEST_ERROR', {'url': path, 'error': str(e)})
            return {'status': 'error', 'error': str(e)}

    def state(self):
        """
        Get live position state from relay server.

        Returns dict with:
            sol_balance, positions[], total_value_usd, pnl_percent, etc.
            Returns None if relay is unreachable.
        """
        result = self._request('GET', '/position-state')
        if 'error' in result:
            return None
        return result

    def sell(self, token_mint, percent=100, slippage_bps=75, agent_name="MsWednesday"):
        """
        Exit position via relay → SXAN wallet API.

        Args:
            token_mint: Solana token mint address
            percent: Percentage to sell (1-100)
            slippage_bps: Slippage in basis points (default 75)
            agent_name: Agent wallet to sell from (default MsWednesday)

        Returns:
            {'signature': '...', 'status': 'success'} on success
            {'status': 'error', 'error': '...'} on failure
        """
        self._log('SELL_INITIATED', {
            'token_mint': token_mint,
            'percent': percent,
            'slippage_bps': slippage_bps,
            'agent_name': agent_name,
        })

        result = self._request('POST', '/execute/sell', {
            'token_mint': token_mint,
            'percent': percent,
            'slippage_bps': slippage_bps,
            'agent_name': agent_name,
        })

        self._log('SELL_RESULT', result)
        return result

    def buy(self, token_mint, amount_sol, slippage_bps=75, agent_name="MsWednesday"):
        """
        Buy token via relay → SXAN wallet API.

        Args:
            token_mint: Solana token mint address
            amount_sol: Amount of SOL to spend (max 1.0)
            slippage_bps: Slippage in basis points (default 75)
            agent_name: Agent wallet to buy from (default MsWednesday)

        Returns:
            {'signature': '...', 'status': 'success'} on success
            {'status': 'error', 'error': '...'} on failure
        """
        self._log('BUY_INITIATED', {
            'token_mint': token_mint,
            'amount_sol': amount_sol,
            'slippage_bps': slippage_bps,
            'agent_name': agent_name,
        })

        result = self._request('POST', '/execute/buy', {
            'token_mint': token_mint,
            'amount_sol': amount_sol,
            'slippage_bps': slippage_bps,
            'agent_name': agent_name,
        })

        self._log('BUY_RESULT', result)
        return result

    def wallet_status(self, agent_name="MsWednesday"):
        """
        Get wallet status (balance, holdings, enabled) via relay.

        Args:
            agent_name: Agent wallet to check (default MsWednesday)

        Returns:
            Dict with pubkey, sol_balance, tokens, enabled, token_count
            or {'status': 'error', ...} on failure.
        """
        self._log('WALLET_STATUS', {'agent_name': agent_name})
        return self._request('GET', f'/wallet-status/{agent_name}')

    def transactions(self, agent_name="MsWednesday", limit=20):
        """
        Get recent trade history for an agent via relay.

        Args:
            agent_name: Agent wallet to query (default MsWednesday)
            limit: Max transactions to return (1-100, default 20)

        Returns:
            List of transaction dicts or error dict.
        """
        self._log('TRANSACTIONS', {'agent_name': agent_name, 'limit': limit})
        return self._request('GET', f'/transactions/{agent_name}?limit={limit}')

    def my_positions(self, agent_name="MsWednesday"):
        """
        Get only this agent's positions from position state via relay.

        Args:
            agent_name: Agent to filter positions for (default MsWednesday)

        Returns:
            Dict with positions[], total, sol_balance, timestamp
            or {'status': 'error', ...} on failure.
        """
        self._log('POSITIONS', {'agent_name': agent_name})
        return self._request('GET', f'/positions/{agent_name}')

    def notify(self, title, details, tx_hash=None):
        """
        Send Telegram notification to Brandon via relay.

        Args:
            title: Alert title
            details: Alert body
            tx_hash: Optional transaction hash
        """
        self._log('NOTIFY', {'title': title})

        return self._request('POST', '/notify', {
            'title': title,
            'details': details,
            'tx_hash': tx_hash,
        })

    def telegram_feed(self, limit=50):
        """
        Get tokens from monitored Telegram chats via relay.

        Args:
            limit: Max tokens to return (1-200, default 50)

        Returns:
            List of token dicts or {'status': 'error', ...} on failure.
        """
        self._log('FEED_TELEGRAM', {'limit': limit})
        return self._request('GET', f'/feeds/telegram?limit={limit}')

    def almost_graduated(self, limit=30):
        """
        Get tokens approaching graduation via relay.

        Args:
            limit: Max tokens to return (1-100, default 30)

        Returns:
            List of token dicts with graduation progress or error dict.
        """
        self._log('FEED_GRADUATING', {'limit': limit})
        return self._request('GET', f'/feeds/graduating?limit={limit}')

    def new_launches(self, limit=30):
        """
        Get recently launched pump.fun tokens via relay.

        Args:
            limit: Max tokens to return (1-100, default 30)

        Returns:
            List of token dicts or error dict.
        """
        self._log('FEED_LAUNCHES', {'limit': limit})
        return self._request('GET', f'/feeds/launches?limit={limit}')

    def catalysts(self, limit=20, min_score=0):
        """
        Get trending catalyst events via relay.
        Google Trends, News RSS, Reddit — scored and keyword-tagged.

        Args:
            limit: Max events to return (1-50, default 20)
            min_score: Minimum trend score filter (0-100, default 0)

        Returns:
            Dict with 'events' list, 'total', 'timestamp', 'status'
            Each event: {source, category, title, trend_score, keywords, description, url}
        """
        self._log('FEED_CATALYSTS', {'limit': limit, 'min_score': min_score})
        params = f'limit={limit}'
        if min_score > 0:
            params += f'&min_score={min_score}'
        return self._request('GET', f'/feeds/catalysts?{params}')

    def check_trigger(self, tp_pct=None, sl_pct=None):
        """
        Check if TP or SL condition is met based on live state.
        DISABLED — TP/SL auto-sell temporarily removed. Returns state only.
        """
        state = self.state()
        if not state:
            return {'triggered': False, 'type': None, 'error': 'No state available'}

        pnl_pct = state.get('pnl_percent', 0)
        current_value = state.get('current_value_usd', 0)

        self._log('CHECK_TRIGGER_DISABLED', {'pnl_percent': pnl_pct, 'tp_pct': tp_pct, 'sl_pct': sl_pct})

        return {
            'triggered': False,
            'type': None,
            'pnl_percent': pnl_pct,
            'current_value_usd': current_value,
        }

    def exit_if_triggered(self, tp_pct=None, sl_pct=None):
        """
        DISABLED — TP/SL auto-sell temporarily removed. Will not execute sells.
        """
        state = self.state()
        pnl_pct = 0
        if state:
            pnl_pct = state.get('pnl_percent', 0)

        self._log('EXIT_TRIGGER_DISABLED', {'pnl_percent': pnl_pct, 'tp_pct': tp_pct, 'sl_pct': sl_pct})

        return {'executed': False, 'trigger_type': None, 'pnl_percent': pnl_pct}

    def _log(self, action, details):
        """Local agent audit log."""
        entry = {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'action': action,
            'details': details,
        }
        try:
            with open(self.log_file, 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except IOError:
            pass


# --- Convenience: module-level singleton ---

_tools = None

def get_tools(server_ip=None, name=None):
    """Get or create VesselTools singleton."""
    global _tools
    if _tools is None:
        _tools = VesselTools(server_ip=server_ip, name=name)
    return _tools


if __name__ == '__main__':
    # Quick test
    tools = VesselTools()

    print("Vessel Tools — Quick Test")
    print(f"  Relay: {tools.relay_url}")
    print()

    state = tools.state()
    if state:
        print(f"  Status: {state.get('status')}")
        print(f"  Total:  ${state.get('total_value_usd', 0):.2f}")
        print(f"  P&L:    {state.get('pnl_percent', 0):+.2f}%")
        for pos in state.get('positions', []):
            print(f"  {pos.get('agent', '???')} - ${pos.get('symbol', '???')}: "
                  f"{pos.get('pnl_percent', 0):+.2f}%")

        print()
        print("  Trigger check (TP: +50%, SL: -30%):")
        trigger = tools.check_trigger(tp_pct=50.0, sl_pct=-30.0)
        print(f"    Triggered: {trigger['triggered']}")
        print(f"    Current P&L: {trigger.get('pnl_percent', 0):+.2f}%")
    else:
        print("  Could not reach relay server")
