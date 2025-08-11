#!/usr/bin/env bash

echo "Installing dependencies..."
pip install -r requirements.txt

echo "Installing Playwright browsers..."
playwright install --with-deps



