#!/bin/bash
# VesselProject - Phone Setup Script
# Run this in Termux on the Android phone

echo "=== VesselProject Phone Setup ==="
echo ""

# Update Termux packages
echo "[1/5] Updating Termux..."
pkg update -y && pkg upgrade -y

# Install Python and Git
echo "[2/5] Installing Python and Git..."
pkg install -y python git

# Clone the project (replace with your actual repo URL)
echo "[3/5] Cloning VesselProject..."
if [ -d "$HOME/VesselProject" ]; then
    echo "  VesselProject already exists, pulling latest..."
    cd "$HOME/VesselProject" && git pull
else
    echo "  MANUAL STEP: Clone your repo or copy files to ~/VesselProject"
    echo "  Example: git clone <your-repo-url> ~/VesselProject"
    mkdir -p "$HOME/VesselProject"
fi

# Install Python dependencies
echo "[4/5] Installing Python dependencies..."
cd "$HOME/VesselProject/vessel"
pip install -r requirements.txt

# Set up environment
echo "[5/5] Setting up environment..."
if [ ! -f "$HOME/.vessel_env" ]; then
    cat > "$HOME/.vessel_env" << 'ENVEOF'
# VesselProject Environment - EDIT THESE VALUES
export VESSEL_SERVER_URL="ws://YOUR_SERVER_IP:8777"
export VESSEL_SECRET="change-me-before-deploy"
export VESSEL_ID="phone-01"
export ANTHROPIC_API_KEY="sk-ant-your-key-here"
ENVEOF
    echo "  Created ~/.vessel_env — EDIT THIS FILE with your actual values!"
else
    echo "  ~/.vessel_env already exists"
fi

# Add to shell profile
if ! grep -q "vessel_env" "$HOME/.bashrc" 2>/dev/null; then
    echo 'source "$HOME/.vessel_env"' >> "$HOME/.bashrc"
    echo "  Added env loader to .bashrc"
fi

# Install tmux for background running (optional, for manual testing)
pkg install -y tmux

# Create workspace
mkdir -p "$HOME/vessel_workspace"

# Install systemd service for auto-start on boot
echo "[6/6] Installing systemd service..."
if command -v systemctl &> /dev/null; then
    # Copy service file to systemd directory
    SERVICE_FILE="/root/VesselProject/vessel/vessel-listener.service"
    SYSTEMD_DIR="/etc/systemd/system"
    
    if [ -f "$SERVICE_FILE" ]; then
        cp "$SERVICE_FILE" "$SYSTEMD_DIR/"
        systemctl daemon-reload
        systemctl enable vessel-listener.service
        echo "  ✓ vessel-listener.service installed and enabled"
        echo "  ✓ Will auto-start on phone reboot"
    else
        echo "  ✗ WARNING: vessel-listener.service not found"
    fi
else
    echo "  NOTE: systemd not available in this Termux setup"
    echo "  Using tmux method instead (see below)"
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "NEXT STEPS:"
echo "  1. Edit ~/.vessel_env with your actual server IP, secret, and API key"
echo "  2. Run: source ~/.vessel_env"
echo ""
echo "LISTENER (receives tasks from MsWednesday):"
echo ""
echo "  OPTION A (Recommended): Systemd Service (auto-starts on reboot)"
echo "    - Service is now installed and enabled"
echo "    - Start it: systemctl start vessel-listener"
echo "    - Check status: systemctl status vessel-listener"
echo "    - View logs: journalctl -u vessel-listener -f"
echo ""
echo "  OPTION B: Manual tmux (for testing)"
echo "    - tmux new -s vessel 'python ~/VesselProject/vessel/listener.py'"
echo "    - (Ctrl+B, D to detach)"
echo ""
echo "DISPLAY (live P&L dashboard — READ-ONLY, no SSH needed):"
echo ""
echo "  tmux new -s display 'python ~/VesselProject/vessel/vessel_display.py'"
echo ""
echo "  Options:"
echo "    --server IP    Server IP (default: 192.168.1.146)"
echo "    --refresh N    Refresh rate in seconds (default: 0.5)"
echo ""
echo "  The display fetches data from the relay server over HTTP."
echo "  It is strictly read-only — cannot send commands to the Mac."
echo ""
echo "PHONE SLEEP SAFETY:"
echo "  Systemd service runs in background (safe to lock screen)"
echo "  Auto-restarts on crash (Restart=on-failure)"
echo "  Auto-starts on phone reboot"
echo ""
echo "TROUBLESHOOTING:"
echo "  - View live logs: journalctl -u vessel-listener -f"
echo "  - Restart service: systemctl restart vessel-listener"
echo "  - Stop service: systemctl stop vessel-listener"
echo "  - Disable auto-start: systemctl disable vessel-listener"
