#!/usr/bin/env python3
"""
Vessel MCP Server — Exposes vessel_tools as MCP tools for Claude Code.

When Claude Code calls a tool, this server forwards it to the relay server
via HTTP, using the same auth and endpoints that phone-side agents use.

Agent identity is configured via environment variables:
    AGENT_NAME   — Agent identity (e.g. "CP0", "msSunday")
    JOB_TYPE     — Agent role (e.g. "scanner", "trader", "content_manager")
    RELAY_URL    — Relay server URL (default: http://localhost:8777)
    VESSEL_SECRET — Auth token for relay

Usage:
    # Standalone test:
    python3 vessel_mcp_server.py

    # Via Claude Code MCP config:
    claude --mcp-config config.json ...
"""

import json
import os
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from mcp.server.fastmcp import FastMCP

# --- Config from environment ---
AGENT_NAME = os.environ.get("AGENT_NAME", "unknown")
JOB_TYPE = os.environ.get("JOB_TYPE", "general")
RELAY_URL = os.environ.get("RELAY_URL", "http://localhost:8777")
VESSEL_SECRET = os.environ.get("VESSEL_SECRET", "mrsunday")


def _relay_request(method: str, path: str, body: dict = None) -> dict:
    """Make HTTP request to relay server."""
    url = f"{RELAY_URL}{path}"
    headers = {
        "Authorization": VESSEL_SECRET,
        "X-Requester": AGENT_NAME,
    }

    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    else:
        data = None

    req = Request(url, data=data, headers=headers, method=method)

    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        return {"error": error_body, "http_status": e.code}
    except (URLError, Exception) as e:
        return {"error": str(e)}


# --- MCP Server ---

mcp = FastMCP("vessel-tools")


# --- State & Wallet Tools ---

@mcp.tool()
def get_state() -> str:
    """Get live position state — SOL balance, all positions, P&L, total value. Use this to understand current portfolio state."""
    result = _relay_request("GET", "/position-state")
    return json.dumps(result)


@mcp.tool()
def my_positions() -> str:
    """Get only YOUR positions (filtered by your agent name). Shows tokens you manage, their P&L, and current value."""
    result = _relay_request("GET", f"/positions/{AGENT_NAME}")
    return json.dumps(result)


@mcp.tool()
def wallet_status(agent_name: str = "") -> str:
    """Get wallet status — SOL balance, token holdings, enabled status. Use to check if you have funds.

    Args:
        agent_name: Agent wallet to check. Defaults to your own wallet.
    """
    target = agent_name if agent_name else AGENT_NAME
    result = _relay_request("GET", f"/wallet-status/{target}")
    return json.dumps(result)


# --- Trading Tools ---

@mcp.tool()
def buy(token_mint: str, amount_sol: float, slippage_bps: int = 75) -> str:
    """Buy a token. Spends SOL from your wallet to purchase tokens on Solana DEX.

    Args:
        token_mint: Solana token mint address (base58, 32-44 chars)
        amount_sol: Amount of SOL to spend (max 1.0)
        slippage_bps: Slippage tolerance in basis points (default 75)
    """
    result = _relay_request("POST", "/execute/buy", {
        "token_mint": token_mint,
        "amount_sol": amount_sol,
        "slippage_bps": slippage_bps,
        "agent_name": AGENT_NAME,
    })
    return json.dumps(result)


@mcp.tool()
def sell(token_mint: str, percent: float = 100, slippage_bps: int = 75) -> str:
    """Sell a token position. Exits some or all of a position back to SOL.

    Args:
        token_mint: Solana token mint address to sell
        percent: Percentage of position to sell (1-100, default 100)
        slippage_bps: Slippage tolerance in basis points (default 75)
    """
    result = _relay_request("POST", "/execute/sell", {
        "token_mint": token_mint,
        "percent": percent,
        "slippage_bps": slippage_bps,
        "agent_name": AGENT_NAME,
    })
    return json.dumps(result)


@mcp.tool()
def transfer(token_mint: str, to_agent: str, percent: int = 100) -> str:
    """Transfer SPL tokens from your wallet to another agent's wallet.

    Args:
        token_mint: Token mint address to transfer
        to_agent: Destination agent name (e.g. 'CP0', 'CP1', 'MsWednesday')
        percent: Percentage of balance to transfer (1-100, default 100)
    """
    result = _relay_request("POST", "/execute/transfer", {
        "token_mint": token_mint,
        "to_agent": to_agent,
        "from_agent": AGENT_NAME,
        "percent": percent,
    })
    return json.dumps(result)


@mcp.tool()
def transfer_sol(to_agent: str, amount_sol: float = None) -> str:
    """Transfer native SOL between agent wallets. Used for returning capital.

    Args:
        to_agent: Destination agent name
        amount_sol: SOL to transfer. Omit to transfer all minus buffer.
    """
    payload = {"from_agent": AGENT_NAME, "to_agent": to_agent}
    if amount_sol is not None:
        payload["amount_sol"] = amount_sol
    result = _relay_request("POST", "/execute/transfer-sol", payload)
    return json.dumps(result)


# --- Notification ---

@mcp.tool()
def notify(title: str, details: str, tx_hash: str = "") -> str:
    """Send a Telegram notification to Brandon. Use for important status updates, alerts, or completed tasks.

    Args:
        title: Alert title (max 100 chars)
        details: Alert body with details (max 500 chars)
        tx_hash: Optional transaction hash to include
    """
    payload = {"title": title, "details": details}
    if tx_hash:
        payload["tx_hash"] = tx_hash
    result = _relay_request("POST", "/notify", payload)
    return json.dumps(result)


# --- Feed Tools ---

@mcp.tool()
def telegram_feed(limit: int = 50) -> str:
    """Get tokens from monitored Telegram chats. Shows what alpha channels are posting.

    Args:
        limit: Max tokens to return (1-200, default 50)
    """
    result = _relay_request("GET", f"/feeds/telegram?limit={limit}")
    return json.dumps(result)


@mcp.tool()
def graduating_tokens(limit: int = 30) -> str:
    """Get tokens approaching pump.fun graduation. Shows progress percentage toward DEX listing.

    Args:
        limit: Max tokens to return (1-100, default 30)
    """
    result = _relay_request("GET", f"/feeds/graduating?limit={limit}")
    return json.dumps(result)


@mcp.tool()
def new_launches(limit: int = 30) -> str:
    """Get recently launched pump.fun tokens.

    Args:
        limit: Max tokens to return (1-100, default 30)
    """
    result = _relay_request("GET", f"/feeds/launches?limit={limit}")
    return json.dumps(result)


@mcp.tool()
def catalysts(limit: int = 20, min_score: float = 0) -> str:
    """Get trending catalyst events — Google Trends, News RSS, Reddit. Scored and keyword-tagged for trading relevance.

    Args:
        limit: Max events to return (1-50, default 20)
        min_score: Minimum trend score filter (0-100, default 0)
    """
    params = f"limit={limit}"
    if min_score > 0:
        params += f"&min_score={min_score}"
    result = _relay_request("GET", f"/feeds/catalysts?{params}")
    return json.dumps(result)


# --- History & Agent Management ---

@mcp.tool()
def transactions(limit: int = 20) -> str:
    """Get recent trade history for your wallet. Shows buys, sells, transfers.

    Args:
        limit: Max transactions to return (1-100, default 20)
    """
    result = _relay_request("GET", f"/transactions/{AGENT_NAME}?limit={limit}")
    return json.dumps(result)


@mcp.tool()
def agents_available() -> str:
    """Get agent availability — who is idle vs busy, what positions they hold, what job type they're assigned."""
    result = _relay_request("GET", "/agents/availability")
    return json.dumps(result)


@mcp.tool()
def agent_checkin() -> str:
    """Manager heartbeat — resets the 5h timeout clock. Call periodically when running as a manager."""
    result = _relay_request("POST", "/agents/checkin", {"agent_name": AGENT_NAME})
    return json.dumps(result)


@mcp.tool()
def wait(seconds: int) -> str:
    """Sleep for a specified number of seconds. Use for polling loops (e.g., check state every 60s).

    Args:
        seconds: Number of seconds to sleep (max 300)
    """
    seconds = min(seconds, 300)
    time.sleep(seconds)
    return json.dumps({"waited": seconds, "status": "ok"})


# --- Compliance Tools ---

@mcp.tool()
def compliance_check(
    question: str,
    decision: str,
    reasoning: str,
    jurisdiction: str = "US",
    reference: str = "",
    human_review_required: bool = False,
    requested_by: str = "",
    next_action: str = "",
) -> str:
    """Log a compliance ruling. Use this to record every decision with structured audit data. Decision must be COMPLIANT, NOT_COMPLIANT, or GRAY_ZONE.

    Args:
        question: The compliance question being evaluated
        decision: Ruling: COMPLIANT, NOT_COMPLIANT, or GRAY_ZONE
        reasoning: Specific legal basis from compliance foundation or regulatory docs
        jurisdiction: Primary jurisdiction (US, EU, Singapore, Multi)
        reference: Which section of compliance foundation or regulation applies
        human_review_required: Whether Brandon/legal team should review before proceeding
        requested_by: Who asked the question (agent name or 'Brandon')
        next_action: Recommended next step
    """
    result = _relay_request("POST", "/compliance/log", {
        "question": question,
        "decision": decision,
        "reasoning": reasoning,
        "jurisdiction": jurisdiction,
        "reference": reference,
        "human_review_required": human_review_required,
        "requested_by": requested_by,
        "next_action": next_action,
    })
    return json.dumps(result)


@mcp.tool()
def compliance_log(limit: int = 50, decision_filter: str = "") -> str:
    """Read past compliance audit entries. Use to check precedent, review history, or verify consistency.

    Args:
        limit: Max entries to return (default 50)
        decision_filter: Filter by decision type: COMPLIANT, NOT_COMPLIANT, or GRAY_ZONE
    """
    params = f"limit={limit}"
    if decision_filter:
        params += f"&decision={decision_filter}"
    result = _relay_request("GET", f"/compliance/log?{params}")
    return json.dumps(result)


@mcp.tool()
def compliance_report() -> str:
    """Generate a compliance summary report with statistics. Shows all-time and last-7-days counts by decision type."""
    result = _relay_request("GET", "/compliance/report")
    return json.dumps(result)


# --- Content Pipeline Tools ---

@mcp.tool()
def scan_content(days_back: int = 7) -> str:
    """Trigger content scan — extracts lessons from logs, commits, audits.

    Args:
        days_back: How many days back to scan (1-30, default 7)
    """
    result = _relay_request("POST", "/content/scan", {"days_back": days_back})
    return json.dumps(result)


@mcp.tool()
def get_lessons(category: str = "", limit: int = 50) -> str:
    """Get extracted lessons from content store.

    Args:
        category: Filter by category (trade_lesson, system_insight, etc.)
        limit: Max lessons to return (1-200, default 50)
    """
    params = f"limit={limit}"
    if category:
        params += f"&category={category}"
    result = _relay_request("GET", f"/content/lessons?{params}")
    return json.dumps(result)


@mcp.tool()
def submit_draft(lesson_id: str, content: str, platform: str = "twitter") -> str:
    """Submit a draft post for review.

    Args:
        lesson_id: ID of the lesson this draft is based on
        content: The post text (max 2000 chars)
        platform: Target platform (default 'twitter')
    """
    result = _relay_request("POST", "/content/submit", {
        "lesson_id": lesson_id,
        "content": content,
        "platform": platform,
        "author_agent": AGENT_NAME,
    })
    return json.dumps(result)


@mcp.tool()
def get_content_queue() -> str:
    """Get the full content queue (pending, approved, published, rejected)."""
    result = _relay_request("GET", "/content/queue")
    return json.dumps(result)


if __name__ == "__main__":
    mcp.run()
