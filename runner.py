# runner.py

import importlib
import os
from datetime import datetime
import traceback
import subprocess
import sys
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def generate_filename(query, site):
    filename_safe = query.lower().replace(" ", "_")
    date_str = datetime.now().strftime("%d%m%y_%H%M%S")
    return os.path.join(BASE_DIR, "static", f"{filename_safe}_{site}_{date_str}.csv")

def run_scraper(site, query, output_file, limit=None):
    # Ensure Playwright Chromium is installed
    try:
        subprocess.run(
            ["python", "-m", "playwright", "install", "chromium"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError as e:
        print(json.dumps({"success": False, "error": f"Failed to install Playwright browsers: {e}"}))
        sys.exit(1)

    try:
        scraper_module = importlib.import_module(f"plugins.{site}")
    except ModuleNotFoundError:
        print(json.dumps({"success": False, "error": f"Scraper module not found for site: {site}"}))
        sys.exit(1)

    try:
        # Run scraper
        if "base_dir" in scraper_module.run_scraper.__code__.co_varnames:
            count = scraper_module.run_scraper(query, output_file, limit=limit, base_dir=BASE_DIR)
        else:
            count = scraper_module.run_scraper(query, output_file, limit=limit)

        if count == 0:
            print(json.dumps({"success": False, "error": "No data scraped."}))
            sys.exit(0)

        if not os.path.exists(output_file):
            print(json.dumps({"success": False, "error": "Output file not found."}))
            sys.exit(0)

        print(json.dumps({"success": True, "file": output_file, "count": count}))
        sys.exit(0)

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        traceback.print_exc()
        sys.exit(1)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Modular Web Scraper")
    parser.add_argument("--mode", default="modular")
    parser.add_argument("--site", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--output", required=False)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    output_file = args.output or generate_filename(args.query, args.site)
    output_file = os.path.abspath(output_file)

    run_scraper(args.site, args.query, output_file, args.limit)

if __name__ == "__main__":
    main()



















"""

# runner.py

import argparse
import importlib
import sys
import os
from datetime import datetime

def generate_filename(query, site):
    filename_safe = query.lower().replace(" ", "_")
    date_str = datetime.now().strftime("%d%m%y")
    return os.path.join("static", f"{filename_safe}_{site}_{date_str}.csv")

def run_scraper(site, query, output_file):
    try:
        scraper_module = importlib.import_module(f"scrapers.{site}")
    except ModuleNotFoundError:
        print(f"Scraper module not found for site: {site}")
        sys.exit(1)

    try:
        print(f"\n🟢 Running: {site} for '{query}' -> {output_file}")
        scraper_module.run_scraper(query, output_file)
    except Exception as e:
        print(f"❌ Scraper failed: {e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Modular Web Scraper")
    parser.add_argument("--mode", default="modular")
    parser.add_argument("--site", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--output", required=False, help="Output CSV file name")
    args = parser.parse_args()

    output_file = args.output or generate_filename(args.query, args.site)
    run_scraper(args.site, args.query, output_file)

if __name__ == "__main__":
    main()

    
"""