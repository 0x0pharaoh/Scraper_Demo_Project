#!/usr/bin/env bash

echo "Installing Python dependencies..."
pip install -r requirements.txt

echo "Installing Playwright Chromium..."
playwright install chromium




