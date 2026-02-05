#!/usr/bin/env python3
"""
VesselProject — Unified Phone Display
Runs natively in Termux. Fetches position data from relay server over HTTP.
NO SSH, NO stored credentials beyond the vessel read token.

Security model:
  - READ-ONLY: Fetches from GET /position-state on relay server
  - Cannot send commands, submit tasks, or modify any state
  - Auth token is the same VESSEL_SECRET already on phone for listener
  - Display runs as separate process from listener (no shared state)

Usage (Termux):
    python3 vessel_display.py                    # Default
    python3 vessel_display.py --refresh 2        # Slower refresh
    python3 vessel_display.py --server 10.0.0.1  # Custom server IP

Tmux:
    tmux new -s display 'python3 vessel_display.py'
"""

import os
import sys
import time
import json
import math
import random
import argparse
from datetime import datetime, timezone, timedelta

# Try to load config, fall back to env vars / defaults
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from config import VESSEL_SECRET, SERVER_PORT
except ImportError:
    VESSEL_SECRET = os.getenv('VESSEL_SECRET', '')
    SERVER_PORT = os.getenv('VESSEL_SERVER_PORT', '8777')

# Try urllib (stdlib, always available) before requests
try:
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError
    USE_URLLIB = True
except ImportError:
    USE_URLLIB = False

try:
    import requests as req_lib
    USE_REQUESTS = True
except ImportError:
    USE_REQUESTS = False

if not USE_URLLIB and not USE_REQUESTS:
    print("ERROR: No HTTP library available")
    sys.exit(1)


# --- Terminal Control ---

class Term:
    CLEAR = '\033[2J\033[H'
    ERASE_EOL = '\033[K'
    HIDE_CURSOR = '\033[?25l'
    SHOW_CURSOR = '\033[?25h'
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'

    # Colors
    GREEN = '\033[0;32m'
    BRIGHT_GREEN = '\033[1;32m'
    RED = '\033[0;31m'
    BRIGHT_RED = '\033[1;31m'
    CYAN = '\033[0;36m'
    BRIGHT_CYAN = '\033[1;36m'
    MAGENTA = '\033[0;35m'
    YELLOW = '\033[0;33m'
    WHITE = '\033[0;37m'

    @staticmethod
    def pos(row, col):
        """Move cursor to row, col (1-indexed)."""
        return f'\033[{row};{col}H'

    @staticmethod
    def get_size():
        """Get terminal dimensions."""
        try:
            rows, cols = os.popen('stty size', 'r').read().split()
            return int(rows), int(cols)
        except Exception:
            return 24, 80


# --- Bouncing Agent ---

class Agent:
    def __init__(self, name, symbol, color, max_w, max_h):
        self.name = name
        self.symbol = symbol
        self.color = color
        self.x = random.randint(4, max(5, max_w - 10))
        self.y = random.randint(4, max(5, max_h - 1))
        self.vx = random.uniform(-1.5, 1.5)
        self.vy = random.uniform(-0.8, 0.8)
        if abs(self.vx) < 0.3:
            self.vx = 0.5
        if abs(self.vy) < 0.2:
            self.vy = 0.3
        self.max_w = max_w
        self.max_h = max_h
        self.glow = 0
        self.active = False
        self.last_seen = 0  # timestamp of last detected activity

    def update(self):
        self.x += self.vx
        self.y += self.vy

        if self.x <= 2 or self.x >= self.max_w - len(self.name) - 3:
            self.vx *= -1
        if self.y <= 4 or self.y >= self.max_h - 1:
            self.vy *= -1

        self.x = max(2, min(self.max_w - len(self.name) - 3, self.x))
        self.y = max(4, min(self.max_h - 1, self.y))
        self.glow = max(0, self.glow - 1)

    def render(self):
        style = Term.BOLD if (self.glow > 0 or self.active) else Term.DIM
        marker = ' *' if self.active else ''
        return f"{Term.pos(int(self.y), int(self.x))}{style}{self.color}{self.symbol} {self.name}{marker}{Term.RESET}"


# --- Data Fetcher ---

def fetch_position_state(server_url, secret):
    """Fetch position state from relay server. READ-ONLY."""
    url = f"{server_url}/position-state"

    try:
        if USE_URLLIB:
            req = Request(url, headers={'Authorization': secret})
            resp = urlopen(req, timeout=5)
            if resp.status == 200:
                return json.loads(resp.read().decode())
            return None
        elif USE_REQUESTS:
            resp = req_lib.get(url, headers={'Authorization': secret}, timeout=5)
            if resp.status_code == 200:
                return resp.json()
            return None
    except Exception:
        return None


def fetch_activity(server_url, secret, limit=5):
    """Fetch recent agent activity from relay audit log. READ-ONLY."""
    url = f"{server_url}/activity?limit={limit}"

    try:
        if USE_URLLIB:
            req = Request(url, headers={'Authorization': secret})
            resp = urlopen(req, timeout=5)
            if resp.status == 200:
                return json.loads(resp.read().decode())
            return []
        elif USE_REQUESTS:
            resp = req_lib.get(url, headers={'Authorization': secret}, timeout=5)
            if resp.status_code == 200:
                return resp.json()
            return []
    except Exception:
        return []


# --- Renderer ---

def pnl_color(value):
    if value > 0:
        return Term.BRIGHT_GREEN
    elif value < 0:
        return Term.BRIGHT_RED
    return Term.DIM


def activity_color(action):
    """Color-code activity entries by type."""
    action = action.upper()
    if 'BUY' in action:
        return Term.BRIGHT_GREEN
    if 'SELL' in action:
        return Term.YELLOW
    if 'TRANSFER' in action:
        return Term.MAGENTA
    if 'ASSIGN' in action or 'RELEASE' in action or 'CHECKIN' in action:
        return Term.CYAN
    if 'TRADE_MANAGER' in action:
        return Term.BRIGHT_CYAN
    if 'NOTIFY' in action:
        return Term.BRIGHT_CYAN
    if 'ERROR' in action or 'REJECTED' in action or 'TIMEOUT' in action:
        return Term.BRIGHT_RED
    if 'WALLET_STATUS' in action or 'TRANSACTIONS' in action or 'POSITIONS' in action:
        return Term.DIM
    # feeds and everything else
    return Term.DIM


# Friendly labels for audit log actions
ACTION_LABELS = {
    'BUY_REQUESTED': 'BUY',
    'BUY_RESULT': None,        # skip
    'BUY_ERROR': 'BUY ERR',
    'BUY_REJECTED': 'BUY DENIED',
    'SELL_REQUESTED': 'SELL',
    'SELL_RESULT': None,       # skip
    'SELL_ERROR': 'SELL ERR',
    'SELL_REJECTED': 'SELL DENIED',
    'TRANSFER_REQUESTED': 'TRANSFER',
    'TRANSFER_RESULT': None,   # skip
    'TRANSFER_ERROR': 'XFER ERR',
    'TRANSFER_REJECTED': 'XFER DENIED',
    'TRADE_MANAGER_CHANGED': 'MGR CHANGE',
    'SET_TRADE_MANAGER_REJECTED': None,
    'SET_TRADE_MANAGER_ERROR': None,
    'WALLET_STATUS': 'STATUS',
    'WALLET_STATUS_ERROR': None,
    'WALLET_STATUS_REJECTED': None,
    'TRANSACTIONS': 'TX HIST',
    'TRANSACTIONS_ERROR': None,
    'TRANSACTIONS_REJECTED': None,
    'POSITIONS': 'POSITIONS',
    'POSITIONS_ERROR': None,
    'POSITIONS_REJECTED': None,
    'NOTIFY_REQUESTED': 'NOTIFY',
    'NOTIFY_RESULT': None,     # skip
    'NOTIFY_ERROR': 'NOTIFY ERR',
    'NOTIFY_REJECTED': None,
    'FEED_TELEGRAM': 'SCAN TG',
    'FEED_TELEGRAM_ERROR': None,
    'FEED_GRADUATING': 'SCAN GRAD',
    'FEED_GRADUATING_ERROR': None,
    'FEED_LAUNCHES': 'SCAN LAUNCH',
    'FEED_LAUNCHES_ERROR': None,
    'FEED_CATALYSTS': 'SCAN CAT',
    'FEED_CATALYSTS_ERROR': None,
    # Agent availability (isolation model)
    'AGENT_ASSIGNED': 'ASSIGNED',
    'ASSIGN_REJECTED': 'ASSIGN DENIED',
    'AGENT_RELEASED': 'RELEASED',
    'MANAGER_CHECKIN': 'CHECKIN',
    'MANAGER_TIMEOUT': 'MGR TIMEOUT',
    'TRANSFER_SOL_REQUESTED': 'SOL XFER',
    'TRANSFER_SOL_RESULT': None,
    'TRANSFER_SOL_ERROR': 'SOL XFER ERR',
    'TRANSFER_SOL_REJECTED': 'SOL DENIED',
}


def render_activity_panel(activity, rows, cols):
    """Render the agent activity ticker section."""
    lines = []
    col_start = 2
    row = 4  # Right below header (3 lines of header)

    lines.append(f"{Term.pos(row, col_start)}{Term.BOLD}{Term.CYAN}AGENT ACTIVITY{Term.RESET}")
    row += 1

    if not activity:
        lines.append(f"{Term.pos(row, col_start)}{Term.DIM}No recent activity{Term.RESET}")
        return ''.join(lines)

    # Filter out _RESULT noise, keep meaningful actions
    filtered = []
    for entry in activity:
        action = entry.get('action', '')
        label = ACTION_LABELS.get(action, action[:16])
        if label is None:
            continue  # skip result/error follow-ups
        filtered.append((entry, label))

    if not filtered:
        lines.append(f"{Term.pos(row, col_start)}{Term.DIM}No recent activity{Term.RESET}")
        return ''.join(lines)

    for entry, label in filtered[-5:]:
        ts = entry.get('timestamp', '')
        action = entry.get('action', '?')
        # Parse time — convert UTC to PST, show HH:MM
        PST = timezone(timedelta(hours=-8))
        time_str = ''
        try:
            if 'T' in ts:
                utc_str = ts.replace('Z', '+00:00')
                utc_dt = datetime.fromisoformat(utc_str)
                pst_dt = utc_dt.astimezone(PST)
                time_str = pst_dt.strftime('%I:%M%p').lstrip('0').lower()
        except Exception:
            time_str = '??:??'

        # Build detail snippet based on action type
        # Use 'requester' for WHO did it, 'agent_name'/'agent' for WHICH wallet
        who = entry.get('requester') or entry.get('agent_name') or entry.get('agent') or entry.get('from_agent', '')
        detail = ''
        if 'agent' in entry and action in ('AGENT_ASSIGNED', 'ASSIGN_REJECTED', 'AGENT_RELEASED', 'MANAGER_CHECKIN', 'MANAGER_TIMEOUT'):
            # Isolation model actions
            ag = entry.get('agent', '?')
            atype = entry.get('type', entry.get('old_type', ''))
            pos = entry.get('position') or entry.get('old_position', '')
            reason = entry.get('reason', '')
            if action == 'AGENT_ASSIGNED':
                pos_short = f" {pos[:6]}..{pos[-3:]}" if pos and len(pos) > 9 else (f" {pos}" if pos else '')
                detail = f"{ag} ({atype}){pos_short}"
            elif action == 'ASSIGN_REJECTED':
                detail = f"{ag} ({reason})" if reason else ag
            elif action == 'AGENT_RELEASED':
                pos_short = f" from {pos[:6]}..{pos[-3:]}" if pos and len(pos) > 9 else ''
                detail = f"{ag}{pos_short}"
            elif action == 'MANAGER_CHECKIN':
                detail = ag
            elif action == 'MANAGER_TIMEOUT':
                hrs = entry.get('elapsed_hours', '?')
                detail = f"{ag} ({hrs}h)"
            else:
                detail = ag
        elif 'old_manager' in entry:
            # Trade manager change
            old = entry.get('old_manager', '?')
            new = entry.get('new_manager', '?')
            by = entry.get('requester', '')
            by_tag = f" by {by}" if by else ''
            detail = f"{old} -> {new}{by_tag}"
        elif 'from_agent' in entry and 'to_agent' in entry:
            # Transfer
            mint = entry.get('token_mint', entry.get('mint', ''))
            mint_short = f" {mint[:6]}..{mint[-3:]}" if mint else ''
            detail = f"{entry['from_agent']} -> {entry['to_agent']}{mint_short}"
        elif 'from_agent' in entry:
            # Transfer rejection
            reason = entry.get('reason', '')
            detail = f"{entry['from_agent']} ({reason})" if reason else entry['from_agent']
        elif 'title' in entry:
            req_tag = f"[{who}] " if who else ''
            detail = f'{req_tag}"{entry["title"][:25]}"'
        elif 'agent_name' in entry:
            mint = entry.get('token_mint', '')
            detail = f"{who}"
            if mint:
                detail += f" {mint[:6]}..{mint[-3:]}"
        elif 'limit' in entry:
            req_tag = f"[{who}] " if who else ''
            detail = f"{req_tag}({entry['limit']} tokens)"

        ac = activity_color(action)
        label_fmt = label[:12].ljust(12)
        line = f"{Term.pos(row, col_start)} {Term.DIM}{time_str}{Term.RESET}  {ac}{label_fmt}{Term.RESET} {Term.DIM}{detail}{Term.RESET}{Term.ERASE_EOL}"
        lines.append(line)
        row += 1

    return ''.join(lines)


def render_data_panel(state, rows, cols):
    """Render the position data panel at the bottom of the screen."""
    lines = []
    panel_start = max(rows - 18, rows // 2 + 2)
    col_start = 2

    # Separator
    sep = f"{Term.CYAN}{'=' * (cols - 2)}{Term.RESET}"
    lines.append(f"{Term.pos(panel_start, 1)}{sep}")

    if not state:
        lines.append(f"{Term.pos(panel_start + 1, col_start)}{Term.DIM}Connecting to relay server...{Term.RESET}")
        return '\n'.join(lines)

    row = panel_start + 1

    # SOL Home Base
    sol = state.get('sol_balance', 0)
    sol_price = state.get('sol_price_usd', 0)
    sol_val = state.get('sol_value_usd', 0)
    lines.append(f"{Term.pos(row, col_start)}{Term.BOLD}SOL (HOME BASE){Term.RESET}  "
                 f"{sol:.6f} SOL  {Term.DIM}(${sol_val:.2f} @ ${sol_price:.2f}){Term.RESET}")
    row += 1

    # Positions
    positions = state.get('positions', [])
    if not positions:
        lines.append(f"{Term.pos(row, col_start)}{Term.DIM}No active positions{Term.RESET}")
        row += 1
    else:
        for pos in positions:
            symbol = pos.get('symbol', '???')
            agent = pos.get('agent', '---')
            pnl_pct = pos.get('pnl_percent', 0)
            pnl_usd = pos.get('pnl_usd', 0)
            value = pos.get('current_value_usd', 0)
            entry = pos.get('entry_sol', 0)
            cur_sol = pos.get('current_value_sol', 0)
            price = pos.get('current_price', 0)
            mcap = pos.get('mcap', 0)
            tp = pos.get('tp_target', 0)
            sl = pos.get('sl_target', 0)
            dist_tp = pos.get('distance_to_tp', 0)
            dist_sl = pos.get('distance_to_sl', 0)
            buys = pos.get('buys', 0)
            sells = pos.get('sells', 0)

            ac = Term.CYAN if agent == 'CP9' else Term.YELLOW if agent == 'msSunday' else Term.GREEN
            pc = pnl_color(pnl_pct)

            lines.append(f"{Term.pos(row, col_start)}")
            row += 1
            lines.append(f"{Term.pos(row, col_start)}{ac}{agent}{Term.RESET} - {Term.BOLD}${symbol}{Term.RESET}"
                         f"  {Term.DIM}MCap ${mcap:,.0f}{Term.RESET}")
            row += 1
            lines.append(f"{Term.pos(row, col_start)}  Entry: {entry:.6f} SOL  "
                         f"Value: ${value:.2f} ({cur_sol:.6f} SOL)")
            row += 1
            lines.append(f"{Term.pos(row, col_start)}  Price: ${price:.10f}")
            row += 1
            lines.append(f"{Term.pos(row, col_start)}  P&L:   {pc}{pnl_pct:+.2f}%{Term.RESET}  "
                         f"({pc}${pnl_usd:+.2f}{Term.RESET})")
            row += 1

            tp_warn = f'{Term.BRIGHT_RED}>>>{Term.RESET}' if 0 < dist_tp <= 10 else '   '
            sl_warn = f'{Term.BRIGHT_RED}>>>{Term.RESET}' if 0 < dist_sl <= 10 else '   '
            lines.append(f"{Term.pos(row, col_start)}  TP:{tp_warn} {tp:+.0f}% (dist:{dist_tp:+.1f}%)  "
                         f"SL:{sl_warn} {sl:+.0f}% (dist:{dist_sl:+.1f}%)")
            row += 1
            lines.append(f"{Term.pos(row, col_start)}  Trades: {buys}B / {sells}S")
            row += 1

    # Summary
    total = state.get('total_value_usd', 0)
    realized = state.get('realized_sol', 0)
    lines.append(f"{Term.pos(row, 1)}{sep}")
    row += 1
    # Combine TOTAL + Realized + Freshness on one line (24-row terminal is tight)
    total_line = f"{Term.pos(row, col_start)}{Term.BOLD}TOTAL:{Term.RESET} ${total:.2f}"
    if realized != 0:
        r_usd = realized * sol_price
        rc = pnl_color(r_usd)
        total_line += f"  {Term.BOLD}R:{Term.RESET}{rc}${r_usd:+.2f}{Term.RESET}"

    source_ts = state.get('source_timestamp', '')
    calc = state.get('calculator_used', False)
    if source_ts:
        try:
            src_time = datetime.fromisoformat(str(source_ts))
            age = (datetime.now() - src_time).total_seconds()
            if age > 120:
                total_line += f"  {Term.RED}{int(age)}s STALE{Term.RESET}"
            else:
                total_line += f"  {Term.DIM}{int(age)}s [{'calc' if calc else 'raw'}]{Term.RESET}"
        except (ValueError, TypeError):
            pass

    total_line += Term.ERASE_EOL
    lines.append(total_line)

    return ''.join(lines)


# --- Main Display Loop ---

def run_display(server_ip, refresh):
    server_url = f"http://{server_ip}:{SERVER_PORT}"

    rows, cols = Term.get_size()

    # Create bouncing agents
    agents = {
        'CP0': Agent('CP0', '\u25c6', Term.GREEN, cols, rows),
        'CP1': Agent('CP1', '\u25b2', Term.MAGENTA, cols, rows),
        'CP9': Agent('CP9', '\u25a0', Term.CYAN, cols, rows),
        'msSunday': Agent('msSunday', '\u25c9', Term.YELLOW, cols, rows),
    }

    state = None
    activity = []
    last_fetch = 0
    fetch_interval = max(3.0, refresh)  # Don't hammer the server
    frame = 0
    fetch_errors = 0

    sys.stdout.write(Term.HIDE_CURSOR)
    sys.stdout.flush()

    try:
        while True:
            # Fetch data periodically
            now = time.time()
            if now - last_fetch >= fetch_interval:
                new_state = fetch_position_state(server_url, VESSEL_SECRET)
                active_agents = set()

                if new_state:
                    state = new_state
                    fetch_errors = 0

                    # Mark active agents from positions
                    for pos in state.get('positions', []):
                        a = pos.get('agent', '')
                        if a and a != 'unassigned':
                            active_agents.add(a)

                    # Mark spawned agents (process-based detection from monitor)
                    for a in state.get('spawned_agents', []):
                        active_agents.add(a)
                else:
                    fetch_errors += 1

                # Fetch activity on same interval (no extra request spam)
                activity = fetch_activity(server_url, VESSEL_SECRET, limit=15)

                # Also mark agents active from recent relay activity
                # Use 'requester' field (who made the call) over 'agent_name' (which wallet)
                agent_names = set(agents.keys())
                for entry in activity:
                    # Prefer requester (who made the request) for attribution
                    a = entry.get('requester') or entry.get('agent_name', '')
                    if a in agent_names:
                        active_agents.add(a)
                    # Check transfer, trade manager, and availability fields
                    for field in ('from_agent', 'to_agent', 'new_manager', 'old_manager', 'agent'):
                        val = entry.get(field, '')
                        if val in agent_names:
                            active_agents.add(val)
                    # Check title field for agent names (notifications)
                    title = entry.get('title', '')
                    for name in agent_names:
                        if name in title:
                            active_agents.add(name)

                for name, agent in agents.items():
                    if name in active_agents:
                        agent.last_seen = now
                        agent.glow = 5
                    # Stay active for 90s after last seen (covers gaps between monitoring cycles)
                    agent.active = (now - agent.last_seen) < 90

                last_fetch = now

            # Update agent positions
            for agent in agents.values():
                agent.update()

            # Render
            output = Term.CLEAR

            # Header
            connected = state is not None and fetch_errors < 3
            status_str = f"{Term.GREEN}CONNECTED{Term.RESET}" if connected else f"{Term.RED}DISCONNECTED{Term.RESET}"
            output += f"{Term.BOLD}{Term.WHITE}{'=' * cols}{Term.RESET}\n"
            output += f"  {Term.BOLD}{Term.CYAN}VESSELPROJECT{Term.RESET}"
            output += f"  {Term.DIM}|{Term.RESET}  {status_str}"
            output += f"  {Term.DIM}|{Term.RESET}  {datetime.now().strftime('%H:%M:%S')}\n"
            output += f"{Term.BOLD}{Term.WHITE}{'=' * cols}{Term.RESET}"

            # Activity section (below header, above data)
            output += render_activity_panel(activity, rows, cols)

            # Agents (bouncing across full screen)
            for agent in agents.values():
                output += agent.render()

            # Data panel (bottom area)
            output += render_data_panel(state, rows, cols)

            sys.stdout.write(output)
            sys.stdout.flush()

            frame += 1
            time.sleep(refresh)

    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(Term.SHOW_CURSOR)
        sys.stdout.write(Term.CLEAR)
        sys.stdout.flush()
        print(f"{Term.GREEN}Display stopped{Term.RESET}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='VesselProject Phone Display')
    parser.add_argument('--server', default=os.getenv('VESSEL_SERVER_IP', '100.78.3.119'),
                        help='Relay server IP (default: 192.168.1.146)')
    parser.add_argument('--refresh', type=float, default=0.5,
                        help='Display refresh interval in seconds (default: 0.5)')
    args = parser.parse_args()
    run_display(args.server, args.refresh)
