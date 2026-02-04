"""
VesselProject - Shared Configuration
Communication layer between MsWednesday and phone vessel agents.
"""

import os

# Server config
SERVER_HOST = os.getenv("VESSEL_SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("VESSEL_SERVER_PORT", "8777"))

# Auth - shared secret between server and vessel
VESSEL_SECRET = os.getenv("VESSEL_SECRET", "change-me-before-deploy")

# Vessel identification
VESSEL_ID = os.getenv("VESSEL_ID", "phone-01")

# Anthropic API (for sub-agent on the phone)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

# Task limits
MAX_TASK_OUTPUT = 10000  # chars
TASK_TIMEOUT = 300  # seconds
