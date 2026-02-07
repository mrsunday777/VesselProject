"""
SXAN Wallet Client for MsWednesday
Simple interface to the agent wallet API.

Usage:
    from sxan_wallet import wallet

    # Check balance
    status = wallet.status()
    print(f"Balance: {status['sol_balance']} SOL")

    # Buy / sell
    wallet.buy("TokenMint...", 0.05)
    wallet.sell("TokenMint...", percent=100)

    # Feed access
    wallet.telegram_feed(50)
    wallet.almost_graduated(30)
    wallet.new_launches(30)

    # Content pipeline (social media)
    wallet.scan_content(days_back=7)
    wallet.get_lessons(category='trade_lesson')
    wallet.submit_draft(lesson_id, "Post text here")
    wallet.get_content_queue()

    # Trading controls
    wallet.stop()
    wallet.resume()
    wallet.is_trading_enabled()
"""

import os
import time
import requests

# Config
SXAN_API_URL = os.getenv('SXAN_API_URL', 'http://localhost:5001')
AGENT_NAME = 'MsWednesday'

# Load AGENT_API_TOKEN: check env first, then read from bot .env
_AGENT_API_TOKEN = os.getenv('AGENT_API_TOKEN')
if not _AGENT_API_TOKEN:
    _bot_env = os.path.expanduser('~/Desktop/Projects/Sxan/bot/.env')
    if os.path.exists(_bot_env):
        with open(_bot_env) as f:
            for line in f:
                line = line.strip()
                if line.startswith('AGENT_API_TOKEN='):
                    _AGENT_API_TOKEN = line.split('=', 1)[1].strip().strip('"').strip("'")
                    break


class AgentWallet:
    """Client for SXAN agent wallet API."""

    def __init__(self, api_url=SXAN_API_URL, agent_name=AGENT_NAME, token=_AGENT_API_TOKEN):
        self.api_url = api_url.rstrip('/')
        self.agent_name = agent_name
        self._token = token
        self._session = requests.Session()
        if self._token:
            self._session.headers['Authorization'] = f'Bearer {self._token}'

    def _get(self, path, params=None):
        """GET request with auth."""
        resp = self._session.get(f'{self.api_url}{path}', params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, json_data=None):
        """POST request with auth."""
        resp = self._session.post(f'{self.api_url}{path}', json=json_data, timeout=90)
        resp.raise_for_status()
        return resp.json()

    # --- Wallet status ---

    def status(self):
        """Get wallet status (balance, enabled, pubkey)."""
        return self._get(f'/api/agent-wallet/status/{self.agent_name}')

    def balance(self):
        """Get SOL balance."""
        data = self.status()
        return data.get('sol_balance', 0)

    def is_enabled(self):
        """Check if wallet is enabled for trading."""
        data = self.status()
        return data.get('enabled', False)

    # --- Trading ---

    def buy(self, token_mint, amount_sol, slippage_bps=75):
        """
        Buy a token.

        Args:
            token_mint: Token mint address
            amount_sol: Amount of SOL to spend
            slippage_bps: Slippage tolerance in basis points (default 75)
        """
        return self._post(f'/api/agent-wallet/buy/{self.agent_name}', {
            'token_mint': token_mint,
            'amount_sol': amount_sol,
            'slippage_bps': slippage_bps,
        })

    def sell(self, token_mint, percent=100, slippage_bps=75):
        """
        Sell a token.

        Args:
            token_mint: Token mint address
            percent: Percentage of balance to sell (1-100)
            slippage_bps: Slippage tolerance in basis points
        """
        return self._post(f'/api/agent-wallet/sell/{self.agent_name}', {
            'token_mint': token_mint,
            'percent': percent,
            'slippage_bps': slippage_bps,
        })

    def instant_sell(self, token_mint, percent=100, slippage_bps=75):
        """
        Instant sell (reactive exit) - MsWednesday tactical liquidation.

        Used for:
        - Quick bounces mid-trade
        - Emergency exits
        - Tactical pivots

        Args:
            token_mint: Token mint address
            percent: Percentage of balance to sell (1-100)
            slippage_bps: Slippage tolerance in basis points
        """
        # Alias to sell() — same API, semantic clarity for MsWednesday
        return self.sell(token_mint, percent=percent, slippage_bps=slippage_bps)

    # --- Token transfers ---

    def transfer(self, token_mint, to_agent, amount=None, percent=100):
        """
        Transfer tokens to another agent's wallet.

        Used for transfer-on-entry model:
        1. MsWednesday buys token
        2. MsWednesday transfers to managing agent (e.g., CP9)
        3. Managing agent owns position and can sell autonomously

        Args:
            token_mint: Token mint address
            to_agent: Destination agent name (e.g., 'CP9', 'msSunday')
            amount: Exact token amount (optional, uses percent if None)
            percent: Percentage of balance to transfer (1-100, default 100)

        Returns:
            {'success': bool, 'signature': str, 'amount': float, ...}
        """
        payload = {
            'to_agent': to_agent,
            'token_mint': token_mint,
            'percent': percent,
        }
        if amount is not None:
            payload['amount'] = amount

        return self._post(f'/api/agent-wallet/transfer/{self.agent_name}', payload)

    def buy_and_transfer(self, token_mint, amount_sol, to_agent, slippage_bps=75):
        """
        Atomic buy + transfer: Entry discipline with immediate ownership transfer.

        Pipeline:
        1. Pre-flight balance check (trade + gas + self reserve + tx fees)
        2. Buy tokens with SOL (MsWednesday entry)
        3. Transfer 100% to managing agent
        4. Send 0.01 SOL gas to agent
        5. Return combined result

        Args:
            token_mint: Token mint address
            amount_sol: SOL to spend on buy
            to_agent: Agent who will manage the position
            slippage_bps: Slippage for buy

        Returns:
            {'success': bool, 'buy': {...}, 'transfer': {...}, 'gas_sent': {...}}
        """
        # Pre-flight balance check
        overhead = self.AGENT_GAS_SOL + self.SELF_RESERVE_SOL + self.TX_FEE_BUFFER
        required = amount_sol + overhead
        current_balance = self.balance()
        if current_balance < required:
            return {
                'success': False,
                'error': (
                    f'Insufficient balance for buy_and_transfer. '
                    f'Have {current_balance:.6f} SOL, need {required:.6f} SOL '
                    f'({amount_sol} trade + {self.AGENT_GAS_SOL} agent gas + '
                    f'{self.SELF_RESERVE_SOL} self reserve + {self.TX_FEE_BUFFER} tx fees)'
                ),
                'buy': None,
                'transfer': None,
                'gas_sent': None,
            }

        # Step 1: Buy
        buy_result = self.buy(token_mint, amount_sol, slippage_bps)
        if not buy_result.get('success'):
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
            try:
                transfer_result = self.transfer(token_mint, to_agent, percent=100)
            except Exception as e:
                err_msg = str(e)
                transfer_result = {'success': False, 'error': err_msg}
                if 'balance' in err_msg.lower() or 'not found' in err_msg.lower() or '400' in err_msg:
                    continue
                break
            if transfer_result.get('success'):
                break
            err = transfer_result.get('error', '')
            if 'balance' not in err.lower() and 'not found' not in err.lower():
                break

        # Step 3: Send gas SOL to agent (0.01 SOL so they can execute sells)
        gas_result = None
        if transfer_result.get('success'):
            gas_result = self.transfer_sol(to_agent, amount_sol=0.01)

        return {
            'success': transfer_result.get('success', False),
            'buy': buy_result,
            'transfer': transfer_result,
            'gas_sent': gas_result,
            'error': transfer_result.get('error') if not transfer_result.get('success') else None,
        }

    def emergency_sell(self, token_mint, agent_name, percent=100, slippage_bps=75):
        """
        Emergency override: Sell from ANY agent's wallet.

        Used when:
        - Managing agent is unresponsive
        - Emergency market conditions
        - Need immediate exit from delegated position

        Args:
            token_mint: Token to sell
            agent_name: Which agent's wallet to sell from
            percent: Percentage to sell (1-100)
            slippage_bps: Slippage tolerance

        Returns:
            Sell result from target agent's wallet
        """
        # Sell directly from the specified agent's wallet
        return self._post(f'/api/agent-wallet/sell/{agent_name}', {
            'token_mint': token_mint,
            'percent': percent,
            'slippage_bps': slippage_bps,
        })

    # --- Agent Availability (Local File) ---

    AVAILABILITY_FILE = os.path.expanduser('~/Desktop/VesselProject/agent_availability.json')

    def _read_availability(self):
        """Read agent availability from local JSON file."""
        import json as _json
        try:
            with open(self.AVAILABILITY_FILE, 'r') as f:
                return _json.load(f)
        except (FileNotFoundError, _json.JSONDecodeError):
            return {'agents': {}}

    def _write_availability(self, data):
        """Write agent availability to local JSON file."""
        import json as _json
        from datetime import datetime, timezone
        data['timestamp'] = datetime.now(timezone.utc).isoformat()
        with open(self.AVAILABILITY_FILE, 'w') as f:
            _json.dump(data, f, indent=2)

    def agents_available(self):
        """
        Get agent availability state. Shows who is idle vs busy.

        Returns:
            Dict with 'agents' map: {agent_name: {status, position, type, ...}}
        """
        return self._read_availability()

    def find_available_agent(self):
        """
        Find first idle agent.

        Returns:
            Agent name string or None if all busy.
        """
        state = self.agents_available()
        if not state or 'agents' not in state:
            return None
        for agent_name, data in state['agents'].items():
            if data.get('status') == 'idle':
                return agent_name
        return None

    def mark_agent_busy(self, agent_name, job_type, token_mint=None):
        """
        Mark an agent as busy in the availability file.

        Args:
            agent_name: Agent to mark busy
            job_type: Job type being assigned
            token_mint: Optional token mint for the position
        """
        from datetime import datetime, timezone
        state = self._read_availability()
        if 'agents' not in state:
            state['agents'] = {}
        state['agents'][agent_name] = {
            'status': 'busy',
            'position': token_mint,
            'assigned_at': datetime.now(timezone.utc).isoformat(),
            'type': job_type,
            'last_checkin': datetime.now(timezone.utc).isoformat(),
        }
        self._write_availability(state)
        return {'success': True, 'agent': agent_name, 'status': 'busy'}

    def mark_agent_idle(self, agent_name):
        """
        Mark an agent as idle in the availability file.

        Args:
            agent_name: Agent to mark idle
        """
        state = self._read_availability()
        if 'agents' not in state:
            state['agents'] = {}
        state['agents'][agent_name] = {
            'status': 'idle',
            'position': None,
            'assigned_at': None,
            'type': None,
            'last_checkin': None,
        }
        self._write_availability(state)
        return {'success': True, 'agent': agent_name, 'status': 'idle'}

    def release_agent(self, agent_name):
        """
        Release agent from assignment (mark idle).

        Args:
            agent_name: Agent to release
        """
        return self.mark_agent_idle(agent_name)

    def transfer_sol(self, to_agent, amount_sol=None, from_agent=None):
        """
        Transfer native SOL between agent wallets.
        Hits dashboard API directly (no relay).

        Args:
            to_agent: Destination agent
            amount_sol: SOL to transfer. None = all minus buffer.
            from_agent: Source agent (default: self.agent_name)
        """
        source = from_agent or self.agent_name
        payload = {
            'to_agent': to_agent,
        }
        if amount_sol is not None:
            payload['amount_sol'] = amount_sol
        return self._post(f'/api/agent-wallet/transfer-sol/{source}', payload)

    # Capital flow constants
    AGENT_GAS_SOL = 0.01     # SOL sent to agent for gas on entry
    SELF_RESERVE_SOL = 0.01  # SOL Wednesday keeps for her own emergency sells
    TX_FEE_BUFFER = 0.005    # Buffer for token transfer + SOL transfer tx fees

    def buy_and_assign(self, token_mint, amount_sol, agent_name=None, slippage_bps=75):
        """
        Orchestrated: find agent → buy → transfer → gas → assign.
        Primary entry method for isolation model.

        Pre-flight checks ensure Wednesday retains enough SOL for:
        - 0.01 agent gas
        - 0.01 self reserve (emergency sells)
        - 0.005 tx fee buffer

        Args:
            token_mint: Token to buy
            amount_sol: SOL to spend
            agent_name: Specific agent (or None for auto-pick)
            slippage_bps: Slippage for buy

        Returns:
            {'success': bool, 'agent': str, 'buy': {...}, 'transfer': {...}, ...}
        """
        # Pre-flight balance check
        overhead = self.AGENT_GAS_SOL + self.SELF_RESERVE_SOL + self.TX_FEE_BUFFER
        required = amount_sol + overhead
        current_balance = self.balance()
        if current_balance < required:
            return {
                'success': False,
                'error': (
                    f'Insufficient balance for buy_and_assign. '
                    f'Have {current_balance:.6f} SOL, need {required:.6f} SOL '
                    f'({amount_sol} trade + {self.AGENT_GAS_SOL} agent gas + '
                    f'{self.SELF_RESERVE_SOL} self reserve + {self.TX_FEE_BUFFER} tx fees)'
                ),
            }

        # Find available agent
        if agent_name is None:
            agent_name = self.find_available_agent()
            if agent_name is None:
                return {'success': False, 'error': 'No available agents — all busy'}

        # Buy tokens
        try:
            buy_result = self.buy(token_mint, amount_sol, slippage_bps)
        except Exception as e:
            return {'success': False, 'error': f'Buy failed: {e}', 'agent': agent_name}

        if not buy_result.get('success'):
            return {
                'success': False,
                'error': f"Buy failed: {buy_result.get('error', 'Unknown')}",
                'agent': agent_name,
                'buy': buy_result,
            }

        # Transfer tokens to agent (retry with delay — RPC needs time to index new balance)
        transfer_result = None
        for attempt in range(4):
            if attempt > 0:
                time.sleep(3)
            try:
                transfer_result = self.transfer(token_mint, agent_name, percent=100)
            except Exception as e:
                err_msg = str(e)
                transfer_result = {'success': False, 'error': err_msg}
                # Retry on balance/not-found errors (HTTP 400 from stale RPC)
                if 'balance' in err_msg.lower() or 'not found' in err_msg.lower() or '400' in err_msg:
                    continue
                break  # Non-balance error, don't retry
            if transfer_result.get('success'):
                break
            err = transfer_result.get('error', '')
            if 'balance' not in err.lower() and 'not found' not in err.lower():
                break  # Non-balance error, don't retry

        if not transfer_result or not transfer_result.get('success'):
            return {
                'success': False,
                'error': f"Transfer failed: {(transfer_result or {}).get('error', 'Unknown')}",
                'agent': agent_name,
                'buy': buy_result,
                'transfer': transfer_result,
            }

        # Send gas SOL to agent (0.01 SOL so they can execute sells)
        gas_result = self.transfer_sol(agent_name, amount_sol=0.01)

        # NOTE: Agent busy/idle marking is handled by the spawn session lifecycle.
        # No assign_agent() call needed — spawn marks busy, session end marks idle.

        return {
            'success': True,
            'agent': agent_name,
            'buy': buy_result,
            'transfer': transfer_result,
            'gas_sent': gas_result,
        }

    def sell_and_return(self, agent_name, token_mint, percent=100, slippage_bps=75):
        """
        Orchestrated exit: sell → return SOL to MsWednesday → release agent.

        Args:
            agent_name: Agent selling their position
            token_mint: Token to sell
            percent: Percentage to sell (1-100)
            slippage_bps: Slippage

        Returns:
            {'success': bool, 'sell': {...}, 'sol_return': {...}, 'release': {...}}
        """
        # Sell from agent's wallet
        try:
            sell_result = self.emergency_sell(token_mint, agent_name, percent, slippage_bps)
        except Exception as e:
            return {'success': False, 'error': f'Sell failed: {e}'}

        if not sell_result.get('success'):
            return {
                'success': False,
                'error': f"Sell failed: {sell_result.get('error', 'Unknown')}",
                'sell': sell_result,
            }

        # Return SOL to MsWednesday
        sol_return = self.transfer_sol(self.agent_name, from_agent=agent_name)

        # Release agent
        release = self.release_agent(agent_name)

        return {
            'success': True,
            'sell': sell_result,
            'sol_return': sol_return,
            'release': release,
        }

    # --- Transaction history ---

    def transactions(self, limit=20):
        """Get recent transaction history."""
        data = self._get(f'/api/agent-wallet/transactions/{self.agent_name}', {'limit': limit})
        return data.get('transactions', [])

    # --- Feed access ---

    def telegram_feed(self, limit=50):
        """
        Get tokens from Telegram feed (monitored chats).

        Returns:
            List of tokens with symbol, address, time, chat info
        """
        data = self._get('/api/telegram/feed', {'wallet': 'J5G2Z5yTgprEiwKEr3NLpKLghAVksez8twitJJwfiYsh'})
        tokens = data.get('tokens', [])
        return tokens[:limit]

    def almost_graduated(self, limit=30):
        """
        Get tokens approaching graduation ($20K-$69K mcap).

        Returns:
            List of tokens with address, symbol, name, mcap, progress (%), logo
        """
        data = self._get('/api/swarm/graduating')
        tokens = data.get('tokens', [])
        return tokens[:limit]

    def new_launches(self, limit=30):
        """
        Get recently launched tokens from pump.fun.

        Returns:
            List of tokens with address, symbol, name, mcap, launch_time, logo
        """
        data = self._get('/api/swarm/launches')
        launches = data.get('launches', [])
        return launches[:limit]

    # --- Catalyst events ---

    def catalysts(self, limit=20, min_score=0):
        """
        Get trending catalyst events (Google Trends, News, Reddit).

        Args:
            limit: Max events to return (1-50)
            min_score: Minimum trend score filter (0-100)

        Returns:
            List of events with source, title, trend_score, keywords, url
        """
        params = {'limit': limit}
        if min_score > 0:
            params['min_score'] = min_score
        data = self._get('/api/swarm/catalysts', params)
        return data.get('events', [])

    # --- Content Pipeline (Social Media Manager) ---

    def scan_content(self, days_back=7):
        """
        Scan private logs for publishable lessons.
        Extracts from session memory, git commits, spawn gate audit.
        All output is anonymized (addresses, agent names, paths stripped).

        Args:
            days_back: How many days of history to scan (default 7)

        Returns:
            {'new_lessons': int, 'total_lessons': int}
        """
        return self._post('/api/content/scan', {'days_back': days_back})

    def get_lessons(self, category=None, limit=50):
        """
        Get extracted lessons (raw material for posts).

        Args:
            category: Filter by category (trade_lesson, system_insight,
                      security_event, feature_update, debugging_story)
            limit: Max results (default 50)

        Returns:
            {'lessons': [...], 'total': int}
        """
        params = {'limit': limit}
        if category:
            params['category'] = category
        return self._get('/api/content/lessons', params)

    def submit_draft(self, lesson_id, content, platform='twitter'):
        """
        Submit a social media post draft for Brandon's review.
        Content is re-anonymized before storage (defense in depth).

        IMPORTANT: Format every draft per ~/Desktop/Projects/Sxan/bot/content/ARTICLE_FORMAT.md
        Structure: Title + Subtitle + Hook → Section Headers → Closer.
        Brandon publishes via X article composer.

        Args:
            lesson_id: ID of the lesson this post is based on
            content: The post text (will be anonymized). Must follow ARTICLE_FORMAT.md
            platform: Target platform (default 'twitter')

        Returns:
            {'draft': {...}, 'id': str}
        """
        return self._post('/api/content/drafts', {
            'lesson_id': lesson_id,
            'content': content,
            'platform': platform,
            'author_agent': self.agent_name,
        })

    def get_content_queue(self):
        """
        Get full content queue (pending + approved + published + rejected).
        Check what Brandon has reviewed and what's still waiting.

        Returns:
            {'drafts': [...], 'total': int}
        """
        return self._get('/api/content/queue')

    # --- Trading controls ---

    def stop(self):
        """Disable wallet — halts all trading immediately."""
        return self._post(f'/api/agent-wallet/disable/{self.agent_name}')

    def resume(self):
        """Enable wallet — resume trading."""
        return self._post(f'/api/agent-wallet/enable/{self.agent_name}')

    def is_trading_enabled(self):
        """Check if trading is allowed."""
        return self.is_enabled()


# Singleton instance — import as: from sxan_wallet import wallet
wallet = AgentWallet()

if __name__ == '__main__':
    print("Checking wallet status...")
    try:
        s = wallet.status()
        print(f"  Public Key: {s.get('pubkey', 'N/A')}")
        print(f"  Balance:    {s.get('sol_balance', 'N/A')} SOL")
        print(f"  Enabled:    {s.get('enabled', 'N/A')}")
        print(f"  Auth:       {'Bearer token' if wallet._token else 'NONE (will fail)'}")
    except Exception as e:
        print(f"  Error: {e}")
