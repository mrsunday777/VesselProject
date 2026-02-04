"""
VesselProject - Task Executor
Runs on the phone inside Termux. Executes tasks in a sandboxed workspace.
"""

import asyncio
import os
import sys
import traceback
import tempfile
import time

sys.path.insert(0, "..")
from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, MAX_TASK_OUTPUT, TASK_TIMEOUT


WORKSPACE = os.path.expanduser("~/vessel_workspace")
os.makedirs(WORKSPACE, exist_ok=True)


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
    """Run a Claude-powered sub-agent that can reason and act."""
    prompt = payload.get("prompt", "")
    if not prompt:
        return {"status": "error", "error": "No prompt provided"}

    if not ANTHROPIC_API_KEY:
        return {"status": "error", "error": "ANTHROPIC_API_KEY not set"}

    try:
        import anthropic
    except ImportError:
        return {"status": "error", "error": "anthropic package not installed. Run: pip install anthropic"}

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system_prompt = (
        "You are a sub-agent running on a phone (Android/Termux). "
        "You are spawned by MsWednesday via the Vessel system. "
        f"Your workspace is: {WORKSPACE}\n"
        "Execute the task you're given. Be concise in your response. "
        "Report what you did and the result."
    )

    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )

        return {
            "status": "completed",
            "response": response.content[0].text[:MAX_TASK_OUTPUT],
            "model": ANTHROPIC_MODEL,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
