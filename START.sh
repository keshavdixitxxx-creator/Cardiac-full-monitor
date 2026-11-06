#!/usr/bin/env bash
cd "$(dirname "$0")"
echo "Starting Cardiac Monitor on localhost..."
python3 agent.py || python agent.py
