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
from collections import deque
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
    def color256(n):
        """256-color foreground ANSI escape."""
        return f'\033[38;5;{n}m'

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


# --- Sprite Definitions ---

AGENT_SPRITES = {
    'CP0': {
        'small': [
            ' ◆ ',
            '◆◆◆',
            ' ◆ ',
        ],
        'large': [
            '  ◆  ',
            ' ◆◆◆ ',
            '◆◆◆◆◆',
            ' ◆◆◆ ',
            '  ◆  ',
        ],
        'gradient': [44, 51, 87, 123, 87],
        'fallback_color': Term.CYAN,
    },
    'CP1': {
        'small': [
            ' ▲ ',
            '▲▲▲',
            ' ▲ ',
        ],
        'large': [
            '  ▲  ',
            ' ▲▲▲ ',
            '▲▲▲▲▲',
            ' ▲▲▲ ',
            '  ▲  ',
        ],
        'gradient': [127, 163, 201, 219, 201],
        'fallback_color': Term.MAGENTA,
    },
    'CP9': {
        'small': [
            '+-+',
            '|■|',
            '+-+',
        ],
        'large': [
            '+---+',
            '|■■■|',
            '|■■■|',
            '|■■■|',
            '+---+',
        ],
        'gradient': [184, 220, 226, 228, 226],
        'fallback_color': Term.CYAN,
    },
    'msSunday': {
        'small': [
            ' ● ',
            '●●●',
            ' ● ',
        ],
        'large': [
            '  ●  ',
            ' ●●● ',
            '●●●●●',
            ' ●●● ',
            '  ●  ',
        ],
        'gradient': [34, 46, 82, 156, 82],
        'fallback_color': Term.YELLOW,
    },
}


# --- Bouncing Agent ---

class Agent:
    def __init__(self, name, sprite_small, sprite_large, color_gradient, fallback_color, max_w, max_h):
        self.name = name
        self.sprite_small = sprite_small
        self.sprite_large = sprite_large
        self.color_gradient = color_gradient
        self.fallback_color = fallback_color
        self.x = float(random.randint(6, max(7, max_w - 12)))
        self.y = float(random.randint(6, max(7, max_h - 4)))
        self.vx = random.uniform(-1.5, 1.5)
        self.vy = random.uniform(-0.8, 0.8)
        if abs(self.vx) < 0.3:
            self.vx = 0.5
        if abs(self.vy) < 0.2:
            self.vy = 0.3
        self.max_w = max_w
        self.max_h = max_h
        self.glow = 0
        self.glow_phase = 0
        self.active = False
        self.last_seen = 0
        self.prev_x = self.x
        self.prev_y = self.y
        self.trail = deque(maxlen=10)

    def current_sprite(self):
        if self.active or self.glow > 0:
            return self.sprite_large
        return self.sprite_small

    def sprite_dims(self):
        """Return (height, width) of current sprite."""
        sp = self.current_sprite()
        h = len(sp)
        w = max(len(row) for row in sp) if sp else 0
        return h, w

    def update(self):
        self.prev_x = self.x
        self.prev_y = self.y

        self.x += self.vx
        self.y += self.vy

        h, w = self.sprite_dims()
        # label extends right of sprite: sprite_width + 1 + name_len
        total_w = w + 1 + len(self.name)

        min_x = 2
        max_x = self.max_w - total_w - 1
        min_y = 4
        max_y = self.max_h - h

        if self.x <= min_x or self.x >= max_x:
            self.vx *= -1
        if self.y <= min_y or self.y >= max_y:
            self.vy *= -1

        self.x = max(min_x, min(max_x, self.x))
        self.y = max(min_y, min(max_y, self.y))
        self.glow = max(0, self.glow - 1)

        # Advance glow phase when active
        if self.active or self.glow > 0:
            self.glow_phase = (self.glow_phase + 1) % len(self.color_gradient)

        # Add trail particle if we moved
        if abs(self.x - self.prev_x) > 0.5 or abs(self.y - self.prev_y) > 0.5:
            self.trail.append((int(self.prev_x), int(self.prev_y), 0))

        # Age all trail entries
        aged = deque(maxlen=10)
        for tx, ty, age in self.trail:
            if age < 10:
                aged.append((tx, ty, age + 1))
        self.trail = aged


# --- Background Particles ---

class Particle:
    def __init__(self, x, y, char, color_code):
        self.x = x
        self.y = y
        self.char = char
        self.color_code = color_code
        self.drift_col_timer = random.randint(2, 3)
        self.drift_row_timer = random.randint(5, 10)
        self.twinkle = False

    def update(self, cols, min_y, max_y):
        self.drift_col_timer -= 1
        if self.drift_col_timer <= 0:
            self.x += random.choice([-1, 0, 1])
            self.x = max(1, min(cols - 1, self.x))
            self.drift_col_timer = random.randint(2, 3)

        self.drift_row_timer -= 1
        if self.drift_row_timer <= 0:
            self.y += random.choice([-1, 0, 1])
            self.y = max(min_y, min(max_y, self.y))
            self.drift_row_timer = random.randint(5, 10)

        self.twinkle = random.random() < 0.15


def init_particles(cols, min_y, max_y, density=0.035):
    """Scatter background particles at ~3.5% density."""
    particles = []
    area = cols * (max_y - min_y + 1)
    count = int(area * density)
    chars = ['.', '.', '.', '+']
    colors = [37, 38, 44, 45]
    for _ in range(count):
        x = random.randint(1, cols - 1)
        y = random.randint(min_y, max_y)
        particles.append(Particle(x, y, random.choice(chars), random.choice(colors)))
    return particles


def update_particles(particles, cols, min_y, max_y):
    for p in particles:
        p.update(cols, min_y, max_y)


# --- Connection Lines ---

def should_connect(a1, a2, max_dist=30):
    """Only draw connection if agents are within max_dist cells."""
    dx = a1.x - a2.x
    dy = a1.y - a2.y
    return (dx * dx + dy * dy) <= max_dist * max_dist


def draw_line_between(buf, x0, y0, x1, y1, cols, rows, min_y):
    """Bresenham's line algorithm — dotted, directional chars, dark gray."""
    style = Term.color256(39)
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    step = 0

    while True:
        # Draw every cell (solid line)
        if True:
            if min_y <= y0 < rows and 1 <= x0 < cols:
                if dx > dy * 2:
                    ch = '-'
                elif dy > dx * 2:
                    ch = '|'
                else:
                    ch = '.'
                buf_set(buf, y0, x0, ch, style)

        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x0 += sx
        if e2 < dx:
            err += dx
            y0 += sy
        step += 1
        if step > 200:
            break


# --- Frame Buffer System ---

def buf_set(buf, row, col, char, style):
    """Write to sparse frame buffer dict."""
    buf[(int(row), int(col))] = (char, style)


def render_agent_layer(agents, particles, frame, rows, cols, min_y, max_y):
    """Composite all visual layers into a frame buffer and emit ANSI output."""
    buf = {}

    # Layer 1: Background particles
    for p in particles:
        if p.twinkle:
            pstyle = Term.BOLD + Term.color256(p.color_code)
        else:
            pstyle = Term.color256(p.color_code)
        buf_set(buf, p.y, p.x, p.char, pstyle)

    # Layer 2: Connection lines (disabled — too noisy on small screen)

    # Layer 3: Agent trails (fading chars in agent color)
    for agent in agents.values():
        for tx, ty, age in agent.trail:
            if min_y <= ty < rows and 1 <= tx < cols:
                fade_colors = agent.color_gradient
                cidx = min(age, len(fade_colors) - 1)
                # Bright chars for fresh trail, dimmer for old
                if age < 3:
                    tch = 'o'
                    tstyle = Term.BOLD + Term.color256(fade_colors[cidx])
                elif age < 6:
                    tch = '+'
                    tstyle = Term.color256(fade_colors[cidx])
                else:
                    tch = '.'
                    tstyle = Term.color256(fade_colors[0])
                buf_set(buf, ty, tx, tch, tstyle)

    # Layer 4: Agent sprites
    for agent in agents.values():
        sprite = agent.current_sprite()
        h, w = agent.sprite_dims()
        ax = int(agent.x)
        ay = int(agent.y)

        if agent.active or agent.glow > 0:
            cidx = agent.glow_phase % len(agent.color_gradient)
            color = Term.color256(agent.color_gradient[cidx])
            style = Term.BOLD + color
        else:
            style = Term.color256(agent.color_gradient[0])

        for row_idx, row_str in enumerate(sprite):
            for col_idx, ch in enumerate(row_str):
                if ch == ' ':
                    continue
                r = ay + row_idx
                c = ax + col_idx
                if min_y <= r < rows and 1 <= c < cols:
                    buf_set(buf, r, c, ch, style)

        # Layer 5: Agent name label (right of sprite, vertically centered)
        label_row = ay + h // 2
        label_col = ax + w + 1
        if agent.active:
            lbl_style = Term.BOLD + Term.color256(agent.color_gradient[2])
            label_text = agent.name + ' *'
        else:
            lbl_style = Term.color256(agent.color_gradient[0])
            label_text = agent.name

        for i, ch in enumerate(label_text):
            c = label_col + i
            if min_y <= label_row < rows and 1 <= c < cols:
                buf_set(buf, label_row, c, ch, lbl_style)

    # Emit buffer as sorted ANSI writes
    output = []
    for (r, c) in sorted(buf.keys()):
        ch, style = buf[(r, c)]
        output.append(f"{Term.pos(r, c)}{style}{ch}{Term.RESET}")
    return ''.join(output)


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


def fetch_agent_availability(server_url, secret):
    """Fetch agent availability from relay server. READ-ONLY."""
    url = f"{server_url}/agents/availability"

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


# Map activity actions to vessel tool types
ACTION_TO_TOOL = {
    'BUY_REQUESTED': 'trading', 'BUY_RESULT': 'trading', 'BUY_ERROR': 'trading',
    'SELL_REQUESTED': 'trading', 'SELL_RESULT': 'trading', 'SELL_ERROR': 'trading',
    'FEED_TELEGRAM': 'scanning', 'FEED_GRADUATING': 'scanning',
    'FEED_LAUNCHES': 'scanning', 'FEED_CATALYSTS': 'scanning',
    'POSITIONS': 'pos_mgmt', 'MANAGER_CHECKIN': 'pos_mgmt',
    'WALLET_STATUS': 'health', 'TRANSACTIONS': 'health',
    'TRANSFER_REQUESTED': 'transfers', 'TRANSFER_SOL_REQUESTED': 'transfers',
    'TRANSFER_RESULT': 'transfers', 'TRANSFER_SOL_RESULT': 'transfers',
    'CONTENT_SCAN': 'content', 'CONTENT_SUBMIT': 'content',
    'CONTENT_LESSONS': 'content', 'CONTENT_QUEUE': 'content',
    'NOTIFY_REQUESTED': 'notify',
}

# Map formal assignment types to tool types
ASSIGN_TYPE_TO_TOOL = {
    'trader': 'trading',
    'scanner': 'scanning',
    'manager': 'pos_mgmt',
    'health': 'health',
    'content_manager': 'content',
}


def render_agent_status_panel(agent_avail, activity, rows, cols):
    """Render vessel tools panel — detects jobs from both assignments and recent activity."""
    lines = []
    col_start = 2

    # Find where activity panel ends
    filtered_count = 0
    if activity:
        for entry in activity:
            action = entry.get('action', '')
            label = ACTION_LABELS.get(action, action[:16])
            if label is not None:
                filtered_count += 1
        filtered_count = min(filtered_count, 13)

    row = 5 + filtered_count + 1

    lines.append(f"{Term.pos(row, col_start)}{Term.BOLD}{Term.YELLOW}VESSEL TOOLS{Term.RESET}")
    row += 1

    # tool_agents: {tool_type: [(agent_name, detail_str), ...]}
    tool_agents = {}
    seen_agents = set()

    # Source 1: Formal assignments from /agents/availability
    if agent_avail and 'agents' in agent_avail:
        for agent_name, info in agent_avail.get('agents', {}).items():
            if info.get('status') == 'busy' and info.get('type'):
                tool = ASSIGN_TYPE_TO_TOOL.get(info['type'], info['type'])
                pos = info.get('position')
                detail = f"{pos[:6]}..{pos[-4:]}" if pos else ''
                tool_agents.setdefault(tool, []).append((agent_name, detail))
                seen_agents.add(agent_name)

    # Source 2: Infer jobs from recent activity (last 90s)
    if activity:
        for entry in activity:
            action = entry.get('action', '')
            tool = ACTION_TO_TOOL.get(action)
            if not tool:
                continue

            # Check age — only count recent entries
            ts = entry.get('timestamp', '')
            try:
                utc_str = ts.replace('Z', '+00:00')
                entry_time = datetime.fromisoformat(utc_str)
                age = (datetime.now(timezone.utc) - entry_time).total_seconds()
                if age > 90:
                    continue
            except (ValueError, TypeError):
                continue

            # Find which agent did this
            agent = entry.get('requester') or entry.get('agent_name') or entry.get('agent', '')
            if not agent or agent in seen_agents or agent == 'MsWednesday':
                continue

            # Add to tool mapping
            detail_label = ACTION_LABELS.get(action, action[:12])
            if detail_label is None:
                detail_label = ''
            tool_agents.setdefault(tool, []).append((agent, detail_label))
            seen_agents.add(agent)

    # Vessel tool list
    tools = [
        ('Trading',   'trading',   Term.BRIGHT_GREEN),
        ('Scanning',  'scanning',  Term.BRIGHT_CYAN),
        ('Pos Mgmt',  'pos_mgmt',  Term.CYAN),
        ('Health',    'health',    Term.YELLOW),
        ('Transfers', 'transfers', Term.MAGENTA),
        ('Content',   'content',   Term.WHITE),
    ]

    for tool_name, tool_type, tool_color in tools:
        agents_on_tool = tool_agents.get(tool_type, [])

        tool_fmt = tool_name.ljust(10)
        line = f"{Term.pos(row, col_start)}{tool_color}{tool_fmt}{Term.RESET} "

        if agents_on_tool:
            parts = []
            for agent_name, detail in agents_on_tool:
                sprite_def = AGENT_SPRITES.get(agent_name, {})
                ac = sprite_def.get('fallback_color', Term.WHITE)
                part = f"{ac}{agent_name}{Term.RESET}"
                if detail:
                    part += f" {Term.DIM}{detail}{Term.RESET}"
                parts.append(part)
            line += ', '.join(parts)
        else:
            line += f"{Term.DIM}--{Term.RESET}"

        line += Term.ERASE_EOL
        lines.append(line)
        row += 1

    # Show idle agents
    all_agents = ['CP0', 'CP1', 'CP9', 'msSunday']
    idle_agents = [a for a in all_agents if a not in seen_agents]

    if idle_agents:
        idle_str = ', '.join(idle_agents)
        line = f"{Term.pos(row, col_start)}{Term.DIM}Idle: {idle_str}{Term.RESET}{Term.ERASE_EOL}"
        lines.append(line)

    return ''.join(lines)


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

    for entry, label in filtered[-13:]:
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
        elif 'agent_name' in entry or ('agent' in entry and not detail):
            mint = entry.get('token_mint', entry.get('mint', ''))
            reason = entry.get('reason', '')
            detail = f"{who}"
            if reason:
                detail += f" ({reason})"
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

    # Create bouncing agents with sprites
    agents = {}
    for name, sdef in AGENT_SPRITES.items():
        agents[name] = Agent(
            name=name,
            sprite_small=sdef['small'],
            sprite_large=sdef['large'],
            color_gradient=sdef['gradient'],
            fallback_color=sdef['fallback_color'],
            max_w=cols,
            max_h=rows,
        )

    # Init background particles
    agent_min_y = 4
    agent_max_y = rows - 1
    particles = init_particles(cols, agent_min_y, agent_max_y)

    state = None
    activity = []
    agent_avail = None
    last_fetch = 0
    fetch_interval = max(3.0, refresh)  # Don't hammer the server
    frame = 0
    fetch_errors = 0

    sys.stdout.write(Term.HIDE_CURSOR)
    sys.stdout.flush()

    try:
        while True:
            # Terminal resize detection every 10 frames
            if frame % 10 == 0:
                new_rows, new_cols = Term.get_size()
                if new_rows != rows or new_cols != cols:
                    rows, cols = new_rows, new_cols
                    agent_min_y = 4
                    agent_max_y = rows - 1
                    particles = init_particles(cols, agent_min_y, agent_max_y)
                    for agent in agents.values():
                        agent.max_w = cols
                        agent.max_h = rows

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

                # Fetch activity and agent availability on same interval
                activity = fetch_activity(server_url, VESSEL_SECRET, limit=30)
                agent_avail = fetch_agent_availability(server_url, VESSEL_SECRET)

                # Also mark agents active from recent relay activity
                # Only count entries from the last 90s — old entries shouldn't light up agents
                agent_names = set(agents.keys())
                for entry in activity:
                    # Skip old entries — parse timestamp and check age
                    ts = entry.get('timestamp', '')
                    try:
                        utc_str = ts.replace('Z', '+00:00')
                        entry_time = datetime.fromisoformat(utc_str)
                        entry_age = (datetime.now(timezone.utc) - entry_time).total_seconds()
                        if entry_age > 90:
                            continue  # Too old to count as "active"
                    except (ValueError, TypeError):
                        continue

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
                    agent.active = name in active_agents
                    if agent.active:
                        agent.glow = 3

                last_fetch = now

            # Update agent positions and particles
            for agent in agents.values():
                agent.update()
            update_particles(particles, cols, agent_min_y, agent_max_y)

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

            # Agent status section (below activity)
            output += render_agent_status_panel(agent_avail, activity, rows, cols)

            # Data panel (bottom area)
            output += render_data_panel(state, rows, cols)

            # Agent layer last — sprites float on top of panels
            output += render_agent_layer(agents, particles, frame, rows, cols, agent_min_y, agent_max_y)

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
