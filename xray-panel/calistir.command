#!/bin/sh
# Xray Test Kosum Paneli - macOS/Linux baslatici
cd "$(dirname "$0")" || exit 1

command -v python3 >/dev/null 2>&1 || { echo "Python 3 gerekli (https://www.python.org/downloads/)"; exit 1; }

echo "Panel baslatiliyor... Jira token'i arayuzde istenecek (her acilista)."
python3 server.py
