"""
VesselProject - Task Executor
Runs on the phone inside Termux. Executes tasks in a sandboxed workspace.
"""

import asyncio
import os
import sys
import traceback
import time
import json
from datetime import datetime

sys.path.insert(0, "..")
from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, MAX_TASK_OUTPUT, TASK_TIMEOUT, AGENT_MAX_TURNS

from agent_tools import get_tool_definitions, execute_tool_calls


WORKSPACE = os.path.expanduser("~/vessel_workspace")
os.makedirs(WORKSPACE, exist_ok=True)

# Session logs directory
SESSION_LOG_DIR = os.path.join(WORKSPACE, "agent_sessions")
os.makedirs(SESSION_LOG_DIR, exist_ok=True)

# Agent identity docs directory
AGENT_DOCS_DIR = os.path.join(WORKSPACE, "agents")

# Active agent sessions — tracked for cancellation
# session_key -> {"cancelled": bool}
_active_sessions = {}


def cancel_session(session_key: str):
    """Signal an active agent session to stop."""
    if session_key in _active_sessions:
        _active_sessions[session_key]["cancelled"] = True
        return True
    return False


async def execute_task(task: dict) -> dict:
    """Route task to the right executor based on task_type."""
    task_type = task.get("task_type", "execute")
    payload = task.get("payload", {})
    timeout = task.get("timeout", TASK_TIMEOUT)

    try:
        if task_type == "shell":
            return await _run_shell(payload, timeout)
        elif task_type == "python":
            return await _run_python(payload, timeout)
        elif task_type == "agent":
            return await _run_agent(payload, timeout)
        elif task_type == "execute":
            # Auto-detect based on payload
            if "command" in payload:
                return await _run_shell(payload, timeout)
            elif "code" in payload:
                return await _run_python(payload, timeout)
            elif "prompt" in payload:
                return await _run_agent(payload, timeout)
            else:
                return {"status": "error", "error": "Unknown payload format"}
        else:
            return {"status": "error", "error": f"Unknown task_type: {task_type}"}

    except asyncio.TimeoutError:
        return {"status": "timeout", "error": f"Task exceeded {timeout}s timeout"}
    except Exception as e:
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


async def _run_shell(payload: dict, timeout: int) -> dict:
    """Execute a shell command in the workspace."""
    command = payload.get("command", "")
    if not command:
        return {"status": "error", "error": "No command provided"}

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=WORKSPACE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"status": "timeout", "error": f"Command exceeded {timeout}s"}

    return {
        "status": "completed",
        "exit_code": proc.returncode,
        "stdout": stdout.decode()[:MAX_TASK_OUTPUT],
        "stderr": stderr.decode()[:MAX_TASK_OUTPUT],
    }


async def _run_python(payload: dict, timeout: int) -> dict:
    """Execute Python code in the workspace."""
    code = payload.get("code", "")
    if not code:
        return {"status": "error", "error": "No code provided"}

    # Write to temp file and execute
    script_path = os.path.join(WORKSPACE, f"_task_{int(time.time())}.py")
    try:
        with open(script_path, "w") as f:
            f.write(code)

        proc = await asyncio.create_subprocess_exec(
            sys.executable, script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKSPACE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"status": "timeout", "error": f"Script exceeded {timeout}s"}

        return {
            "status": "completed",
            "exit_code": proc.returncode,
            "stdout": stdout.decode()[:MAX_TASK_OUTPUT],
            "stderr": stderr.decode()[:MAX_TASK_OUTPUT],
        }
    finally:
        if os.path.exists(script_path):
            os.remove(script_path)


async def _run_agent(payload: dict, timeout: int) -> dict:
    """
    Run a Claude-powered agent with vessel_tools access.
    Multi-turn agentic loop — agent can call tools and reason across turns.
    """
    import urllib.request
    import json as _json

    prompt = payload.get("prompt", "")
    if not prompt:
        return {"status": "error", "error": "No prompt provided"}

    if not ANTHROPIC_API_KEY:
        return {"status": "error", "error": "ANTHROPIC_API_KEY not set"}

    agent_name = payload.get("agent_name", "unknown")
    max_turns = payload.get("max_turns", AGENT_MAX_TURNS)
    identity = payload.get("identity", "")
    job_type = payload.get("job_type", "general")
    session_id = payload.get("session_id", f"{agent_name}_{int(time.time())}")
    model = payload.get("model", ANTHROPIC_MODEL)

    # Register session for cancellation tracking
    session_key = session_id
    _active_sessions[session_key] = {"cancelled": False}

    # Build system prompt
    system_prompt = _build_agent_system(agent_name, identity, job_type)

    # Get tool definitions
    tools = get_tool_definitions()

    messages = [{"role": "user", "content": prompt}]

    # Session log
    log_path = os.path.join(SESSION_LOG_DIR, f"{agent_name}_{int(time.time())}.jsonl")
    session_start = time.time()
    total_input_tokens = 0
    total_output_tokens = 0

    _session_log(log_path, "session_start", {
        "agent_name": agent_name,
        "job_type": job_type,
        "session_id": session_id,
        "model": model,
        "max_turns": max_turns,
        "prompt_preview": prompt[:500],
    })

    last_response = None

    try:
        for turn in range(max_turns):
            # Check cancellation
            if _active_sessions.get(session_key, {}).get("cancelled"):
                _session_log(log_path, "session_cancelled", {"turn": turn + 1})
                return {
                    "status": "cancelled",
                    "response": _extract_text(last_response) if last_response else "Cancelled before first response",
                    "turns": turn,
                    "session_id": session_id,
                }

            # Call Claude API
            try:
                response = await asyncio.wait_for(
                    _claude_api_call(system_prompt, messages, tools, model),
                    timeout=min(timeout, 120),
                )
            except asyncio.TimeoutError:
                _session_log(log_path, "api_timeout", {"turn": turn + 1})
                return {
                    "status": "timeout",
                    "error": f"Claude API call timed out on turn {turn + 1}",
                    "turns": turn,
                    "session_id": session_id,
                }

            last_response = response

            # Track token usage
            usage = response.get("usage", {})
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)

            stop_reason = response.get("stop_reason", "")
            content = response.get("content", [])

            _session_log(log_path, "api_response", {
                "turn": turn + 1,
                "stop_reason": stop_reason,
                "content_blocks": len(content),
                "usage": usage,
            })

            if stop_reason == "end_turn" or stop_reason == "stop_sequence":
                # Agent is done
                final_text = _extract_text(response)
                elapsed = round(time.time() - session_start, 2)

                _session_log(log_path, "session_complete", {
                    "turns": turn + 1,
                    "elapsed_seconds": elapsed,
                    "total_input_tokens": total_input_tokens,
                    "total_output_tokens": total_output_tokens,
                })

                return {
                    "status": "completed",
                    "response": final_text[:MAX_TASK_OUTPUT],
                    "turns": turn + 1,
                    "session_id": session_id,
                    "model": model,
                    "usage": {
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                    },
                    "elapsed_seconds": elapsed,
                }

            if stop_reason == "tool_use":
                # Execute tool calls
                _session_log(log_path, "tool_calls", {
                    "turn": turn + 1,
                    "tools": [b["name"] for b in content if b.get("type") == "tool_use"],
                })

                tool_results = await execute_tool_calls(content, agent_name)

                _session_log(log_path, "tool_results", {
                    "turn": turn + 1,
                    "results_count": len(tool_results),
                })

                # Append assistant response and tool results to conversation
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": tool_results})
            else:
                # Unexpected stop reason — treat as done
                final_text = _extract_text(response)
                _session_log(log_path, "unexpected_stop", {
                    "turn": turn + 1,
                    "stop_reason": stop_reason,
                })
                return {
                    "status": "completed",
                    "response": final_text[:MAX_TASK_OUTPUT],
                    "turns": turn + 1,
                    "session_id": session_id,
                    "stop_reason": stop_reason,
                }

        # Hit max turns
        final_text = _extract_text(last_response) if last_response else "No response generated"
        elapsed = round(time.time() - session_start, 2)

        _session_log(log_path, "session_hit_limit", {
            "max_turns": max_turns,
            "elapsed_seconds": elapsed,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
        })

        return {
            "status": "completed",
            "response": final_text[:MAX_TASK_OUTPUT],
            "turns": max_turns,
            "hit_limit": True,
            "session_id": session_id,
            "model": model,
            "usage": {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            },
            "elapsed_seconds": elapsed,
        }

    except Exception as e:
        _session_log(log_path, "session_error", {
            "error": str(e),
            "traceback": traceback.format_exc(),
        })
        return {"status": "error", "error": str(e), "session_id": session_id}

    finally:
        _active_sessions.pop(session_key, None)


def _build_agent_system(agent_name: str, identity: str, job_type: str) -> str:
    """Build the system prompt for an agent session."""
    parts = []

    parts.append(
        f"You are {agent_name}, a sub-agent in the SXAN trading system. "
        f"You run on the phone vessel in a sandboxed environment. "
        f"You can ONLY interact with the system through the tools provided to you. "
        f"You have NO shell access, NO filesystem access, and NO direct API calls.\n"
    )

    parts.append(
        f"Your current job type is: {job_type}\n"
    )

    if identity:
        parts.append("--- YOUR IDENTITY & RULES ---\n")
        parts.append(identity)
        parts.append("\n--- END IDENTITY ---\n")

    parts.append(
        "IMPORTANT CONSTRAINTS:\n"
        "- You can ONLY use the tools provided. No other actions are possible.\n"
        "- Every action you take is audit-logged. Act responsibly.\n"
        "- If you're unsure, use notify() to alert Brandon rather than guessing.\n"
        "- Be concise in your reasoning. Report what you did and the results.\n"
        "- When your task is complete, end your turn with a summary of what happened.\n"
    )

    return "\n".join(parts)


async def _claude_api_call(system: str, messages: list, tools: list, model: str) -> dict:
    """Make a single Claude API call with tool definitions."""
    import urllib.request
    import json as _json

    body = _json.dumps({
        "model": model,
        "max_tokens": 4096,
        "system": system,
        "messages": messages,
        "tools": tools,
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )

    loop = asyncio.get_event_loop()
    resp_data = await loop.run_in_executor(None, _sync_urlopen, req)
    return resp_data


def _sync_urlopen(req) -> dict:
    """Synchronous URL open for use with run_in_executor."""
    import urllib.request
    import json as _json

    with urllib.request.urlopen(req, timeout=120) as resp:
        return _json.loads(resp.read().decode())


def _extract_text(response: dict) -> str:
    """Extract text content from a Claude API response."""
    if not response or "content" not in response:
        return ""
    text_parts = []
    for block in response.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block["text"])
    return "\n".join(text_parts)


def _session_log(log_path: str, event: str, data: dict):
    """Append an event to the session log file."""
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "event": event,
        **data,
    }
    try:
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except IOError:
        pass
