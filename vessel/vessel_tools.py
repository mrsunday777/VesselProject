#!/usr/bin/env python3
"""
Vessel Agent Toolkit — Unified tools for phone-side agents.

ALL communication with the Mac goes through the relay server.
Agents never call the Mac's internal APIs directly.

Tools:
    state()                      — Get live position state (read-only)
    buy()                        — Buy token via relay → SXAN API (multi-agent)
    sell()                       — Exit position via relay → SXAN API (multi-agent)
    transfer()                   — Transfer tokens between agent wallets
    buy_and_transfer()           — Atomic buy + transfer to specific agent
    get_trade_manager()          — Get current trade manager assignment
    set_trade_manager()          — Set who receives new positions
    transfer_to_manager()        — Transfer to current trade manager (dynamic routing)
    buy_and_transfer_to_manager() — Atomic buy + transfer to current manager
    wallet_status()              — Get wallet balance, holdings, enabled status
    transactions()               — Get recent trade history for an agent
    my_positions()               — Get only this agent's positions from state
    notify()                     — Send Telegram alert to Brandon via relay
    telegram_feed()              — Tokens from monitored Telegram chats
    almost_graduated()           — Tokens approaching graduation
    new_launches()               — New pump.fun token launches
    catalysts()                  — Trending events (Google Trends, News, Reddit)
    agents_available()            — Get agent availability state
    find_available_agent()       — Find first idle agent
    assign_agent()               — Assign agent to position (mark busy)
    release_agent()              — Release agent from assignment (mark idle)
    agent_checkin()              — Manager heartbeat (resets timeout)
    transfer_sol()               — Transfer native SOL between wallets
    buy_and_assign()             — Orchestrated: buy → transfer → assign agent
    sell_and_return()            — Orchestrated: sell → return SOL → release agent
    check_trigger()              — Check TP/SL against live state
    exit_if_triggered()          — Check + sell in one call

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
DEFAULT_SERVER_IP = os.getenv('VESSEL_SERVER_IP', '100.78.3.119')


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

    def transfer(self, token_mint, to_agent, amount=None, percent=100, from_agent="MsWednesday"):
        """
        Transfer tokens from one agent wallet to another via relay.

        Args:
            token_mint: Token mint address
            to_agent: Destination agent name
            amount: Exact token amount (optional)
            percent: Percentage of balance to transfer (1-100, default 100)
            from_agent: Source agent name (default MsWednesday)

        Returns:
            {'success': bool, 'signature': str, ...} on success
            {'status': 'error', 'error': '...'} on failure
        """
        self._log('TRANSFER_INITIATED', {
            'token_mint': token_mint,
            'from_agent': from_agent,
            'to_agent': to_agent,
            'percent': percent,
            'amount': amount,
        })

        payload = {
            'token_mint': token_mint,
            'to_agent': to_agent,
            'from_agent': from_agent,
            'percent': percent,
        }
        if amount is not None:
            payload['amount'] = amount

        result = self._request('POST', '/execute/transfer', payload)

        self._log('TRANSFER_RESULT', result)
        return result

    def buy_and_transfer(self, token_mint, amount_sol, to_agent, slippage_bps=75, agent_name="MsWednesday"):
        """
        Atomic buy + transfer: Entry followed by immediate ownership transfer.

        Args:
            token_mint: Token mint address
            amount_sol: SOL to spend on buy
            to_agent: Agent who will manage the position
            slippage_bps: Slippage for buy
            agent_name: Agent buying the tokens (default MsWednesday)

        Returns:
            {'success': bool, 'buy': {...}, 'transfer': {...}}
        """
        self._log('BUY_AND_TRANSFER_INITIATED', {
            'token_mint': token_mint,
            'amount_sol': amount_sol,
            'to_agent': to_agent,
        })

        # Step 1: Buy
        buy_result = self.buy(token_mint, amount_sol, slippage_bps, agent_name)
        if buy_result.get('status') == 'error' or not buy_result.get('success'):
            return {
                'success': False,
                'error': f"Buy failed: {buy_result.get('error', 'Unknown')}",
                'buy': buy_result,
                'transfer': None,
            }

        # Step 2: Transfer 100% to managing agent (retry — RPC needs time to index new balance)
        transfer_result = None
        for attempt in range(4):
            if attempt > 0:
                time.sleep(3)
            transfer_result = self.transfer(token_mint, to_agent, percent=100, from_agent=agent_name)
            if transfer_result.get('success'):
                break
            err = str(transfer_result.get('error', ''))
            if 'balance' not in err.lower() and 'not found' not in err.lower():
                break  # Non-balance error, don't retry

        result = {
            'success': transfer_result.get('success', False),
            'buy': buy_result,
            'transfer': transfer_result,
            'error': transfer_result.get('error') if not transfer_result.get('success') else None,
        }

        self._log('BUY_AND_TRANSFER_RESULT', result)
        return result

    # --- Trade Manager (dynamic routing) ---

    def get_trade_manager(self):
        """
        Get current trade manager from vessel state.
        Returns the agent who receives positions after entry.
        """
        result = self._request('GET', '/trade-manager')
        return result.get('trade_manager')

    def set_trade_manager(self, agent_name):
        """
        Set current trade manager.
        All new positions will be transferred to this agent after buy.

        Args:
            agent_name: Agent to assign as trade manager (e.g., 'CP9', 'CP0', 'msSunday')

        Returns:
            {'success': bool, 'trade_manager': str, 'previous': str}
        """
        self._log('SET_TRADE_MANAGER', {'agent_name': agent_name})
        return self._request('POST', '/trade-manager', {'agent_name': agent_name})

    def transfer_to_manager(self, token_mint, amount=None, percent=100, from_agent="MsWednesday"):
        """
        Transfer tokens to the current trade manager.
        Vessel infra handles routing — caller doesn't need to know who's managing.

        Args:
            token_mint: Token mint address
            amount: Exact token amount (optional)
            percent: Percentage of balance to transfer (1-100, default 100)
            from_agent: Source agent name (default MsWednesday)

        Returns:
            {'success': bool, 'signature': str, 'to_agent': str, ...}
        """
        manager = self.get_trade_manager()
        if not manager:
            self._log('TRANSFER_TO_MANAGER_FAILED', {'error': 'No trade manager configured'})
            return {'success': False, 'error': 'No trade manager configured'}

        self._log('TRANSFER_TO_MANAGER', {
            'token_mint': token_mint,
            'from_agent': from_agent,
            'to_manager': manager,
            'percent': percent,
        })

        return self.transfer(token_mint, manager, amount, percent, from_agent)

    def buy_and_transfer_to_manager(self, token_mint, amount_sol, slippage_bps=75, agent_name="MsWednesday"):
        """
        Atomic buy + transfer to current trade manager.
        MsWednesday entry discipline → automatic handoff to whoever is managing.

        Args:
            token_mint: Token mint address
            amount_sol: SOL to spend on buy
            slippage_bps: Slippage for buy
            agent_name: Agent buying the tokens (default MsWednesday)

        Returns:
            {'success': bool, 'buy': {...}, 'transfer': {...}, 'trade_manager': str}
        """
        manager = self.get_trade_manager()
        if not manager:
            self._log('BUY_AND_TRANSFER_TO_MANAGER_FAILED', {'error': 'No trade manager configured'})
            return {'success': False, 'error': 'No trade manager configured', 'buy': None, 'transfer': None}

        self._log('BUY_AND_TRANSFER_TO_MANAGER_INITIATED', {
            'token_mint': token_mint,
            'amount_sol': amount_sol,
            'to_manager': manager,
        })

        result = self.buy_and_transfer(token_mint, amount_sol, manager, slippage_bps, agent_name)
        result['trade_manager'] = manager

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

    # --- Agent Availability (Multi-Position Isolation Model) ---

    def agents_available(self):
        """
        Get agent availability state from relay.

        Returns:
            Dict with 'agents' map showing each agent's status, position, type.
            Or None if relay unreachable.
        """
        result = self._request('GET', '/agents/availability')
        if 'error' in result:
            return None
        return result

    def find_available_agent(self):
        """
        Find first idle agent from availability state.

        Returns:
            Agent name string or None if all busy.
        """
        state = self.agents_available()
        if not state:
            return None
        for agent_name, data in state.get('agents', {}).items():
            if data.get('status') == 'idle':
                return agent_name
        return None

    def assign_agent(self, agent_name, token_mint, agent_type="trader"):
        """
        Assign an agent to a position (marks them as busy).

        Args:
            agent_name: Agent to assign
            token_mint: Token mint they're managing
            agent_type: 'trader' or 'manager'

        Returns:
            {'success': bool, 'agent_name': str, 'status': 'busy', ...}
        """
        self._log('ASSIGN_AGENT', {'agent': agent_name, 'token_mint': token_mint, 'type': agent_type})
        return self._request('POST', '/agents/assign', {
            'agent_name': agent_name,
            'token_mint': token_mint,
            'agent_type': agent_type,
        })

    def release_agent(self, agent_name):
        """
        Release an agent from their assignment (marks them as idle).

        Args:
            agent_name: Agent to release

        Returns:
            {'success': bool, 'agent_name': str, 'status': 'idle', ...}
        """
        self._log('RELEASE_AGENT', {'agent': agent_name})
        return self._request('POST', '/agents/release', {
            'agent_name': agent_name,
        })

    def agent_checkin(self, agent_name):
        """
        Manager heartbeat — resets the 5h timeout clock.

        Args:
            agent_name: Manager agent checking in

        Returns:
            {'success': bool, 'last_checkin': str}
        """
        return self._request('POST', '/agents/checkin', {
            'agent_name': agent_name,
        })

    def transfer_sol(self, from_agent, to_agent, amount_sol=None):
        """
        Transfer native SOL between agent wallets via relay.
        Used for capital return: trader sells → SOL goes back to MsWednesday.

        Args:
            from_agent: Source agent wallet
            to_agent: Destination agent wallet
            amount_sol: SOL to transfer. None = transfer all minus buffer.

        Returns:
            {'success': bool, 'signature': str, 'amount_sol': float, ...}
        """
        self._log('TRANSFER_SOL_INITIATED', {
            'from_agent': from_agent,
            'to_agent': to_agent,
            'amount_sol': amount_sol,
        })

        payload = {'from_agent': from_agent, 'to_agent': to_agent}
        if amount_sol is not None:
            payload['amount_sol'] = amount_sol

        result = self._request('POST', '/execute/transfer-sol', payload)
        self._log('TRANSFER_SOL_RESULT', result)
        return result

    # Capital flow constants
    AGENT_GAS_SOL = 0.01     # SOL sent to agent for gas on entry
    SELF_RESERVE_SOL = 0.01  # SOL buyer keeps for emergency sells
    TX_FEE_BUFFER = 0.005    # Buffer for token transfer + SOL transfer tx fees

    def buy_and_assign(self, token_mint, amount_sol, agent_name=None, slippage_bps=75, buyer="MsWednesday"):
        """
        Orchestrated flow: find agent → buy → transfer tokens → gas → mark busy.
        This is the PRIMARY entry method for the isolation model.

        Pre-flight checks ensure buyer retains enough SOL for:
        - 0.01 agent gas
        - 0.01 self reserve (emergency sells)
        - 0.005 tx fee buffer

        Args:
            token_mint: Token to buy
            amount_sol: SOL to spend
            agent_name: Specific agent to assign (or None for auto-pick)
            slippage_bps: Slippage for buy
            buyer: Who buys (default MsWednesday)

        Returns:
            {'success': bool, 'agent': str, 'buy': {...}, 'transfer': {...}, 'assignment': {...}}
        """
        # Pre-flight balance check
        overhead = self.AGENT_GAS_SOL + self.SELF_RESERVE_SOL + self.TX_FEE_BUFFER
        required = amount_sol + overhead
        status = self.wallet_status(buyer)
        current_balance = status.get('sol_balance', 0) if isinstance(status, dict) else 0
        if current_balance < required:
            return {
                'success': False,
                'error': (
                    f'Insufficient balance for buy_and_assign. '
                    f'{buyer} has {current_balance:.6f} SOL, needs {required:.6f} SOL '
                    f'({amount_sol} trade + {self.AGENT_GAS_SOL} agent gas + '
                    f'{self.SELF_RESERVE_SOL} self reserve + {self.TX_FEE_BUFFER} tx fees)'
                ),
                'buy': None,
                'transfer': None,
                'gas_sent': None,
                'assignment': None,
            }

        self._log('BUY_AND_ASSIGN_INITIATED', {
            'token_mint': token_mint,
            'amount_sol': amount_sol,
            'requested_agent': agent_name,
            'buyer_balance': current_balance,
        })

        # Step 1: Find available agent
        if agent_name is None:
            agent_name = self.find_available_agent()
            if agent_name is None:
                return {
                    'success': False,
                    'error': 'No available agents — all busy',
                    'buy': None,
                    'transfer': None,
                    'assignment': None,
                }

        # Step 2: Buy tokens
        buy_result = self.buy(token_mint, amount_sol, slippage_bps, buyer)
        if buy_result.get('status') == 'error' or not buy_result.get('success'):
            return {
                'success': False,
                'error': f"Buy failed: {buy_result.get('error', 'Unknown')}",
                'agent': agent_name,
                'buy': buy_result,
                'transfer': None,
                'assignment': None,
            }

        # Step 3: Transfer tokens to agent (retry — RPC needs time to index new balance)
        transfer_result = None
        for attempt in range(4):
            if attempt > 0:
                time.sleep(3)
            transfer_result = self.transfer(token_mint, agent_name, percent=100, from_agent=buyer)
            if transfer_result.get('success'):
                break
            err = str(transfer_result.get('error', ''))
            if 'balance' not in err.lower() and 'not found' not in err.lower():
                break  # Non-balance error, don't retry

        if not transfer_result.get('success'):
            return {
                'success': False,
                'error': f"Transfer failed: {transfer_result.get('error', 'Unknown')}",
                'agent': agent_name,
                'buy': buy_result,
                'transfer': transfer_result,
                'assignment': None,
            }

        # Step 4: Send gas SOL to agent (0.01 SOL so they can execute sells)
        gas_result = self.transfer_sol(agent_name, amount_sol=0.01, from_agent=buyer)

        # Step 5: Mark agent as busy
        assign_result = self.assign_agent(agent_name, token_mint, "trader")

        result = {
            'success': assign_result.get('success', False),
            'agent': agent_name,
            'buy': buy_result,
            'transfer': transfer_result,
            'gas_sent': gas_result,
            'assignment': assign_result,
        }

        self._log('BUY_AND_ASSIGN_RESULT', {
            'success': result['success'],
            'agent': agent_name,
            'token_mint': token_mint,
        })

        return result

    def sell_and_return(self, agent_name, token_mint, percent=100, slippage_bps=75, return_to="MsWednesday"):
        """
        Orchestrated exit: sell position → return SOL to apex → release agent.

        Args:
            agent_name: Agent selling their position
            token_mint: Token to sell
            percent: Percentage to sell (1-100)
            slippage_bps: Slippage for sell
            return_to: Who receives the SOL proceeds (default MsWednesday)

        Returns:
            {'success': bool, 'sell': {...}, 'sol_return': {...}, 'release': {...}}
        """
        self._log('SELL_AND_RETURN_INITIATED', {
            'agent': agent_name,
            'token_mint': token_mint,
            'percent': percent,
            'return_to': return_to,
        })

        # Step 1: Sell
        sell_result = self.sell(token_mint, percent, slippage_bps, agent_name)
        if sell_result.get('status') == 'error' or not sell_result.get('success'):
            return {
                'success': False,
                'error': f"Sell failed: {sell_result.get('error', 'Unknown')}",
                'sell': sell_result,
                'sol_return': None,
                'release': None,
            }

        # Step 2: Return SOL to apex (all minus buffer)
        sol_return = self.transfer_sol(agent_name, return_to)

        # Step 3: Release agent (even if SOL return fails — agent is done)
        release = self.release_agent(agent_name)

        result = {
            'success': True,
            'sell': sell_result,
            'sol_return': sol_return,
            'release': release,
        }

        self._log('SELL_AND_RETURN_RESULT', {
            'success': True,
            'agent': agent_name,
            'sol_returned': sol_return.get('amount_sol', 0) if sol_return.get('success') else 0,
        })

        return result

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
