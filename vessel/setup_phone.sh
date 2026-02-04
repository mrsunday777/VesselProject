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

# Install tmux for background running
pkg install -y tmux

# Create workspace
mkdir -p "$HOME/vessel_workspace"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "NEXT STEPS:"
echo "  1. Edit ~/.vessel_env with your actual server IP, secret, and API key"
echo "  2. Run: source ~/.vessel_env"
echo "  3. Start the listener: python ~/VesselProject/vessel/listener.py"
echo ""
echo "TIP: Use tmux to keep it running when screen is off:"
echo "  tmux new -s vessel"
echo "  python ~/VesselProject/vessel/listener.py"
echo "  (then Ctrl+B, D to detach)"
echo ""
echo "To prevent Android from killing Termux:"
echo "  termux-wake-lock"
