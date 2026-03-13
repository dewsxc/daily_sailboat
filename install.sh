#!/bin/bash

# Exit on error
set -e

echo "Starting installation for Daily Sailboat Bot..."

# 1. Update system and install dependencies
echo "Updating system packages..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip sqlite3 tzdata

# 2. Set Timezone to Asia/Taipei (UTC+8) to ensure cron runs at the right time
echo "Setting system timezone to Asia/Taipei..."
sudo ln -sf /usr/share/zoneinfo/Asia/Taipei /etc/localtime
sudo dpkg-reconfigure -f noninteractive tzdata

# 3. Create Virtualenv
echo "Creating virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

# 4. Install Python requirements
echo "Installing requirements..."
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 5. Create secret.yml if it doesn't exist (copy from example)
if [ ! -f "secret.yml" ]; then
    echo "Creating secret.yml from example. PLEASE EDIT THIS FILE WITH YOUR API KEYS."
    cp secret.yml.example secret.yml
else
    echo "secret.yml already exists. Skipping..."
fi

# 6. Setup Cronjob (Daily at 01:00)
echo "Setting up cronjob..."
# Use absolute path for the script based on where install.sh is located
BASE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SCRIPT_PATH="${BASE_DIR}/bot.py"
VENV_PYTHON="${BASE_DIR}/venv/bin/python3"

# Remove existing cronjob for this script to avoid duplicates
crontab -l 2>/dev/null | grep -v "$SCRIPT_PATH" | crontab - || true

# Add new cronjob: 01:00 every day (UTC+8)
# Pipe output directly to system log (syslog) via logger
# GCP will automatically ingest this to Cloud Logging
(crontab -l 2>/dev/null; echo "0 1 * * * cd ${BASE_DIR} && ${VENV_PYTHON} ${SCRIPT_PATH} 2>&1 | logger -t daily_sailboat_bot") | crontab -

echo "--------------------------------------------------"
echo "Installation completed successfully!"
echo "IMPORTANT: Please edit 'secret.yml' and fill in your API keys before the first run."
echo "You can manually test the bot by running: ./venv/bin/python3 bot.py"
echo "--------------------------------------------------"
