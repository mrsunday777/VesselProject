"""
Vessel Agent Tools — Claude API tool definitions + execution dispatcher.

Defines every tool an agent can use as a Claude API tool schema,
and routes tool calls through VesselTools to the relay server.

Agents ONLY get access to these tools. No shell, no filesystem, no direct API calls.
"""

import asyncio
import json
import os
import time
from datetime import datetime

# VesselTools handles all HTTP calls to the relay
from vessel_tools import VesselTools


# --- Tool Definitions (Claude API format) ---

VESSEL_TOOL_DEFINITIONS = [
    {
        "name": "get_state",
        "description": "Get live position state — SOL balance, all positions, P&L, total value. Use this to understand current portfolio state.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "my_positions",
        "description": "Get only YOUR positions (filtered by your agent name). Shows tokens you manage, their P&L, and current value.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "wallet_status",
        "description": "Get your wallet status — SOL balance, token holdings, enabled status. Use to check if you have funds.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "Agent wallet to check. Defaults to your own wallet.",
                },
            },
        },
    },
    {
        "name": "buy",
        "description": "Buy a token. Spends SOL from your wallet to purchase tokens on Solana DEX.",
        "input_schema": {
            "type": "object",
            "properties": {
                "token_mint": {
                    "type": "string",
                    "description": "Solana token mint address (base58, 32-44 chars)",
                },
                "amount_sol": {
                    "type": "number",
                    "description": "Amount of SOL to spend (max 1.0)",
                },
                "slippage_bps": {
                    "type": "integer",
                    "description": "Slippage tolerance in basis points (default 75)",
                    "default": 75,
                },
            },
            "required": ["token_mint", "amount_sol"],
        },
    },
    {
        "name": "sell",
        "description": "Sell a token position. Exits some or all of a position back to SOL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "token_mint": {
                    "type": "string",
                    "description": "Solana token mint address to sell",
                },
                "percent": {
                    "type": "number",
                    "description": "Percentage of position to sell (1-100, default 100)",
                    "default": 100,
                },
                "slippage_bps": {
                    "type": "integer",
                    "description": "Slippage tolerance in basis points (default 75)",
                    "default": 75,
                },
            },
            "required": ["token_mint"],
        },
    },
    {
        "name": "transfer",
        "description": "Transfer SPL tokens from your wallet to another agent's wallet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "token_mint": {
                    "type": "string",
                    "description": "Token mint address to transfer",
                },
                "to_agent": {
                    "type": "string",
                    "description": "Destination agent name (e.g. 'CP0', 'CP1', 'MsWednesday')",
                },
                "percent": {
                    "type": "integer",
                    "description": "Percentage of balance to transfer (1-100, default 100)",
                    "default": 100,
                },
            },
            "required": ["token_mint", "to_agent"],
        },
    },
    {
        "name": "transfer_sol",
        "description": "Transfer native SOL between agent wallets. Used for returning capital.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to_agent": {
                    "type": "string",
                    "description": "Destination agent name",
                },
                "amount_sol": {
                    "type": "number",
                    "description": "SOL to transfer. Omit to transfer all minus buffer.",
                },
            },
            "required": ["to_agent"],
        },
    },
    {
        "name": "notify",
        "description": "Send a Telegram notification to Brandon. Use for important status updates, alerts, or completed tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Alert title (max 100 chars)",
                },
                "details": {
                    "type": "string",
                    "description": "Alert body with details (max 500 chars)",
                },
                "tx_hash": {
                    "type": "string",
                    "description": "Optional transaction hash to include",
                },
            },
            "required": ["title", "details"],
        },
    },
    {
        "name": "telegram_feed",
        "description": "Get tokens from monitored Telegram chats. Shows what alpha channels are posting.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max tokens to return (1-200, default 50)",
                    "default": 50,
                },
            },
        },
    },
    {
        "name": "graduating_tokens",
        "description": "Get tokens approaching pump.fun graduation. Shows progress percentage toward DEX listing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max tokens to return (1-100, default 30)",
                    "default": 30,
                },
            },
        },
    },
    {
        "name": "new_launches",
        "description": "Get recently launched pump.fun tokens.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max tokens to return (1-100, default 30)",
                    "default": 30,
                },
            },
        },
    },
    {
        "name": "catalysts",
        "description": "Get trending catalyst events — Google Trends, News RSS, Reddit. Scored and keyword-tagged for trading relevance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max events to return (1-50, default 20)",
                    "default": 20,
                },
                "min_score": {
                    "type": "number",
                    "description": "Minimum trend score filter (0-100, default 0)",
                    "default": 0,
                },
            },
        },
    },
    {
        "name": "transactions",
        "description": "Get recent trade history for your wallet. Shows buys, sells, transfers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max transactions to return (1-100, default 20)",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "agents_available",
        "description": "Get agent availability — who is idle vs busy, what positions they hold, what job type they're assigned.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    # assign_agent and release_agent REMOVED — agent busy/idle lifecycle
    # is handled by spawn sessions (auto-release on end/kill/timeout).
    # Emergency release: POST /agents/release (server endpoint, not agent tool).
    {
        "name": "agent_checkin",
        "description": "Manager heartbeat — resets the 5h timeout clock. Call periodically when running as a manager.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "wait",
        "description": "Sleep for a specified number of seconds. Use for polling loops (e.g., check state every 60s).",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "integer",
                    "description": "Number of seconds to sleep (max 300)",
                },
            },
            "required": ["seconds"],
        },
    },
    # --- Compliance Tools (msCounsel) ---
    {
        "name": "compliance_check",
        "description": "Log a compliance ruling. Use this to record every decision with structured audit data. Decision must be COMPLIANT, NOT_COMPLIANT, or GRAY_ZONE.",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The compliance question being evaluated (e.g. 'Can we hold SOL in Vessel Labs wallet?')",
                },
                "decision": {
                    "type": "string",
                    "description": "Ruling: COMPLIANT, NOT_COMPLIANT, or GRAY_ZONE",
                    "enum": ["COMPLIANT", "NOT_COMPLIANT", "GRAY_ZONE"],
                },
                "reasoning": {
                    "type": "string",
                    "description": "Specific legal basis from compliance foundation or regulatory docs",
                },
                "jurisdiction": {
                    "type": "string",
                    "description": "Primary jurisdiction (US, EU, Singapore, Multi)",
                    "default": "US",
                },
                "reference": {
                    "type": "string",
                    "description": "Which section of compliance foundation or regulation applies",
                },
                "human_review_required": {
                    "type": "boolean",
                    "description": "Whether Brandon/legal team should review before proceeding",
                    "default": False,
                },
                "requested_by": {
                    "type": "string",
                    "description": "Who asked the question (agent name or 'Brandon')",
                },
                "next_action": {
                    "type": "string",
                    "description": "Recommended next step",
                },
            },
            "required": ["question", "decision", "reasoning"],
        },
    },
    {
        "name": "compliance_log",
        "description": "Read past compliance audit entries. Use to check precedent, review history, or verify consistency.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max entries to return (default 50)",
                    "default": 50,
                },
                "decision_filter": {
                    "type": "string",
                    "description": "Filter by decision type: COMPLIANT, NOT_COMPLIANT, or GRAY_ZONE",
                },
            },
        },
    },
    {
        "name": "compliance_report",
        "description": "Generate a compliance summary report with statistics. Shows all-time and last-7-days counts by decision type.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


# --- Per-Job-Type Tool Access Matrix ---
# Maps job_type -> set of allowed tool names.
# "general" is permissive (all tools) as a safe fallback.
# Defense-in-depth: enforced at both get_tool_definitions() and execute_tool().

TOOL_ACCESS_MATRIX = {
    "scanner": {
        "get_state", "my_positions", "wallet_status", "notify",
        "telegram_feed", "graduating_tokens", "new_launches", "catalysts",
        "transactions", "agents_available", "agent_checkin", "wait",
    },
    "trader": {
        "get_state", "my_positions", "wallet_status", "buy", "sell",
        "transfer", "transfer_sol", "notify", "telegram_feed",
        "graduating_tokens", "new_launches", "catalysts", "transactions",
        "agents_available", "agent_checkin", "wait",
    },
    "manager": {
        "get_state", "my_positions", "wallet_status", "buy", "sell",
        "transfer", "transfer_sol", "notify", "telegram_feed",
        "graduating_tokens", "new_launches", "catalysts", "transactions",
        "agents_available", "agent_checkin", "wait",
    },
    "health": {
        "get_state", "my_positions", "wallet_status", "notify",
        "transactions", "agents_available", "agent_checkin", "wait",
    },
    "health_monitor": {
        "get_state", "my_positions", "wallet_status", "notify",
        "transactions", "agents_available", "agent_checkin", "wait",
    },
    "compliance_counsel": {
        "get_state", "my_positions", "wallet_status", "notify",
        "transactions", "agents_available", "wait",
        "compliance_check", "compliance_log", "compliance_report",
    },
    "compliance": {
        "get_state", "my_positions", "wallet_status", "notify",
        "transactions", "agents_available", "wait",
        "compliance_check", "compliance_log", "compliance_report",
    },
    "content_manager": {
        "get_state", "my_positions", "wallet_status", "notify",
        "telegram_feed", "graduating_tokens", "new_launches", "catalysts",
        "transactions", "agents_available", "wait",
    },
    "news_reporter": {
        "get_state", "my_positions", "wallet_status", "notify",
        "telegram_feed", "graduating_tokens", "new_launches", "catalysts",
        "transactions", "agents_available", "wait",
    },
    "scout": {
        "get_state", "my_positions", "wallet_status", "notify",
        "telegram_feed", "graduating_tokens", "new_launches", "catalysts",
        "transactions", "agents_available", "wait",
    },
    # "general" is the permissive fallback — all tools allowed
    "general": {t["name"] for t in VESSEL_TOOL_DEFINITIONS},
}


def get_tool_definitions(job_type: str = "general"):
    """Return the Claude API tool definitions list, filtered by job_type."""
    allowed = TOOL_ACCESS_MATRIX.get(job_type, TOOL_ACCESS_MATRIX["general"])
    return [t for t in VESSEL_TOOL_DEFINITIONS if t["name"] in allowed]


async def execute_tool(tool_name: str, tool_input: dict, agent_name: str, job_type: str = "general") -> dict:
    """
    Execute a vessel tool call and return the result.

    Args:
        tool_name: Name of the tool to execute
        tool_input: Tool input parameters from Claude
        agent_name: Identity of the calling agent (for attribution)
        job_type: Agent's job type for tool access control

    Returns:
        Dict with tool execution result
    """
    # Defense-in-depth: enforce tool access matrix at execution time
    allowed = TOOL_ACCESS_MATRIX.get(job_type, TOOL_ACCESS_MATRIX["general"])
    if tool_name not in allowed:
        return {
            "error": f"Tool '{tool_name}' not allowed for job_type '{job_type}'",
            "allowed_tools": sorted(allowed),
        }

    tools = VesselTools(name=agent_name)

    try:
        if tool_name == "get_state":
            result = tools.state()
            return result or {"error": "No state available"}

        elif tool_name == "my_positions":
            return tools.my_positions(agent_name)

        elif tool_name == "wallet_status":
            target = tool_input.get("agent_name", agent_name)
            return tools.wallet_status(target)

        elif tool_name == "buy":
            return tools.buy(
                token_mint=tool_input["token_mint"],
                amount_sol=tool_input["amount_sol"],
                slippage_bps=tool_input.get("slippage_bps", 75),
                agent_name=agent_name,
            )

        elif tool_name == "sell":
            return tools.sell(
                token_mint=tool_input["token_mint"],
                percent=tool_input.get("percent", 100),
                slippage_bps=tool_input.get("slippage_bps", 75),
                agent_name=agent_name,
            )

        elif tool_name == "transfer":
            return tools.transfer(
                token_mint=tool_input["token_mint"],
                to_agent=tool_input["to_agent"],
                percent=tool_input.get("percent", 100),
                from_agent=agent_name,
            )

        elif tool_name == "transfer_sol":
            return tools.transfer_sol(
                from_agent=agent_name,
                to_agent=tool_input["to_agent"],
                amount_sol=tool_input.get("amount_sol"),
            )

        elif tool_name == "notify":
            return tools.notify(
                title=tool_input["title"],
                details=tool_input["details"],
                tx_hash=tool_input.get("tx_hash"),
            )

        elif tool_name == "telegram_feed":
            return tools.telegram_feed(limit=tool_input.get("limit", 50))

        elif tool_name == "graduating_tokens":
            return tools.almost_graduated(limit=tool_input.get("limit", 30))

        elif tool_name == "new_launches":
            return tools.new_launches(limit=tool_input.get("limit", 30))

        elif tool_name == "catalysts":
            return tools.catalysts(
                limit=tool_input.get("limit", 20),
                min_score=tool_input.get("min_score", 0),
            )

        elif tool_name == "transactions":
            return tools.transactions(
                agent_name=agent_name,
                limit=tool_input.get("limit", 20),
            )

        elif tool_name == "agents_available":
            result = tools.agents_available()
            return result or {"error": "Could not reach relay"}

        elif tool_name in ("assign_agent", "release_agent"):
            return {"error": "DEPRECATED: assign_agent/release_agent removed. Agent lifecycle is managed by spawn sessions."}

        elif tool_name == "agent_checkin":
            return tools.agent_checkin(agent_name)

        elif tool_name == "wait":
            seconds = min(tool_input.get("seconds", 60), 300)
            await asyncio.sleep(seconds)
            return {"waited": seconds, "status": "ok"}

        # --- Compliance Tools ---
        elif tool_name == "compliance_check":
            return tools._request('POST', '/compliance/log', {
                'question': tool_input['question'],
                'decision': tool_input['decision'],
                'reasoning': tool_input['reasoning'],
                'jurisdiction': tool_input.get('jurisdiction', 'US'),
                'reference': tool_input.get('reference', ''),
                'human_review_required': tool_input.get('human_review_required', False),
                'requested_by': tool_input.get('requested_by', ''),
                'next_action': tool_input.get('next_action', ''),
            })

        elif tool_name == "compliance_log":
            params = f"limit={tool_input.get('limit', 50)}"
            decision_filter = tool_input.get('decision_filter')
            if decision_filter:
                params += f"&decision={decision_filter}"
            return tools._request('GET', f'/compliance/log?{params}')

        elif tool_name == "compliance_report":
            return tools._request('GET', '/compliance/report')

        else:
            return {"error": f"Unknown tool: {tool_name}"}

    except Exception as e:
        return {"error": f"Tool execution failed: {str(e)}"}


async def execute_tool_calls(content_blocks: list, agent_name: str, job_type: str = "general") -> list:
    """
    Process all tool_use blocks from a Claude response.

    Args:
        content_blocks: The 'content' array from Claude's response
        agent_name: Identity of the calling agent
        job_type: Agent's job type for tool access control

    Returns:
        List of tool_result content blocks for the next message
    """
    results = []
    for block in content_blocks:
        if block.get("type") != "tool_use":
            continue

        tool_name = block["name"]
        tool_input = block.get("input", {})
        tool_id = block["id"]

        result = await execute_tool(tool_name, tool_input, agent_name, job_type)

        results.append({
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": json.dumps(result) if isinstance(result, dict) else str(result),
        })

    return results
