import os

SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8777
VESSEL_SECRET = "mrsunday"
VESSEL_ID = "phone-01"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
MAX_TASK_OUTPUT = 10000
TASK_TIMEOUT = 300

# Load API key from secrets file (not tracked by git)
ANTHROPIC_API_KEY = ""
try:
    with open(os.path.join(os.path.dirname(__file__), "secrets.txt")) as f:
        for line in f:
            if line.startswith("ANTHROPIC_API_KEY="):
                ANTHROPIC_API_KEY = line.split("=", 1)[1].strip()
except FileNotFoundError:
    pass
