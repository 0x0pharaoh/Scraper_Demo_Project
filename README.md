# Modular Web Scraper

![Plugin-Based Architecture](https://img.shields.io/badge/Architecture-Plugin--Based-blueviolet?style=for-the-badge&logo=code)
![Plugin Support](https://img.shields.io/badge/Plugins-Supported-Yes-28a745?style=for-the-badge&logo=plug)
![Python](https://img.shields.io/badge/Built%20With-Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
[![Live Demo](https://img.shields.io/badge/Live-Demo-blue?style=for-the-badge&logo=render)](https://scraper-demo-project.onrender.com)

## Overview

This is a modular web scraping application built using Python, Flask, and Playwright.  
It supports multiple scraping targets using a plugin-based architecture, allowing scrapers to be added, tested, and maintained independently.

The project includes:

- A web-based user interface (via Flask)
- A CLI runner for scripting and automation
- A dynamic plugin loader
- Output in CSV format
- Auto-scroll support for JavaScript-heavy sites
- Visual debug via screenshots

## Features

- Plugin-based scraper support
- Live deployment on Render
- Playwright-based browser automation
- Flask UI with form-based input
- CLI tool for power users
- Auto-scroll and data extraction
- Screenshot logging for failed scrapes
- Output saved as downloadable CSV

## Live URL

[https://scraper-demo-project.onrender.com](https://scraper-demo-project.onrender.com)

## Available Plugins

| Plugin       | Description                                                 |
|--------------|-------------------------------------------------------------|
| indiamart    | Scrape supplier contact data from IndiaMART B2B marketplace |
| google_maps  | Scrape names, ratings, addresses from Google Maps listings  |

Each plugin implements a `run_scraper(query, output_file, limit)` interface and can be validated using `validate_plugins.py`.

## Getting Started

### Prerequisites

- Python 3.8+
- [Playwright](https://playwright.dev/python/)
- Chromium installed by Playwright

### Installation

```bash
git clone https://github.com/yourusername/scraper_project.git
cd scraper_project
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
playwright install chromium

### Running Locally (Web UI)

python app.py
Visit http://localhost:5000 to use the web interface.

Running from CLI
python runner.py --site indiamart --query "tiles"

### Optional arguments:

--limit 10 to limit the number of results

--output static/myfile.csv to specify a custom CSV path

## Plugin Development

### To add a new scraper:

1. Create a new file in the plugins/ folder, e.g., justdial.py
2. Implement the following interface:

description = "Scraper for JustDial"

def run_scraper(query, output_file, limit=None):
    # your scraping logic
4. It will automatically be available in the UI and CLI.

## Plugin Validation
Use the provided utility to validate all plugin modules:

python validate_plugins.py

## Checks that each plugin:

- Imports without error
- Contains a run_scraper() method
- Accepts the required arguments
- Has a description for display

## Deployment (Render or similar)
Ensure render.yaml includes:

build:
  command: |
    pip install -r requirements.txt
    playwright install chromium
    python validate_plugins.py

start: python app.py
Static files (CSV, screenshots) are accessible via /static/filename.csv.

Logging and Debugging
Screenshots from Playwright are saved to /static/indiamart_debug.png

Logs are printed to console and can be redirected if needed

Failed extractions log useful warnings for debugging

## Folder Structure

scraper_project/
├── app.py
├── runner.py
├── validate_plugins.py
├── plugins/
│   ├── indiamart.py
│   ├── google_maps.py
│   └── ...
├── templates/
│   └── index.html
├── static/
│   └── *.csv, *.png
├── utils/
│   └── logger.py
├── requirements.txt
├── render.yaml


License
MIT License (or your chosen license)

---


