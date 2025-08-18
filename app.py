# app.py

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import subprocess
import os
import csv
import time
import json
from datetime import datetime
import importlib
from flask_cors import CORS
from urllib.parse import urljoin
from utils.logger import log_buffer   # import log_buffer
import base64   # needed for encoding

app = Flask(__name__)
app.secret_key = "your-secret-key"
CORS(app, resources={r"/api/*": {"origins": "*"}})  # Allow extension to call API

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

def get_available_plugins():
    plugin_dir = os.path.join(BASE_DIR, "plugins")
    return [
        f[:-3] for f in os.listdir(plugin_dir)
        if f.endswith(".py") and f != "__init__.py"
    ]

def load_table_data(filename, max_rows=100):
    """Load up to max_rows from CSV file (excluding header)."""
    file_path = os.path.join(STATIC_DIR, filename)
    if not os.path.exists(file_path):
        return None, None
    with open(file_path, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        return None, None
    headers = rows[0]
    data = rows[1:max_rows + 1] if max_rows else rows[1:]
    return headers, data

def abs_url(path):
    base = request.host_url
    return urljoin(base, path.lstrip("/"))

def try_run_plugin_direct(site, query, output_abs_path, limit):
    """Attempt to run a scraper plugin directly via plugins.<site>.run_scraper."""
    try:
        module = importlib.import_module(f"plugins.{site}")
    except ModuleNotFoundError:
        return {"success": False, "error": f"Plugin not found for site: {site}"}
    except Exception as e:
        return {"success": False, "error": f"Failed to import plugin {site}: {e}"}

    if not hasattr(module, "run_scraper"):
        return {"success": False, "error": f"Plugin {site} has no run_scraper()"}

    try:
        result = module.run_scraper(query, output_file=output_abs_path, limit=limit)
        count = 0
        if isinstance(result, dict):
            if "data" in result and isinstance(result["data"], list):
                count = len(result["data"])
            elif "count" in result and isinstance(result["count"], int):
                count = result["count"]
        else:
            try:
                count = int(result)
            except Exception:
                count = 0

        if not os.path.exists(output_abs_path):
            return {"success": False, "error": "Output file not found after plugin run."}

        return {"success": True, "file": output_abs_path, "count": count}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.after_request
def add_logs_to_response(response):
    """Store logs in session so they survive redirects."""
    if log_buffer:
        logs_text = " || ".join(log_buffer[-50:])
        session["last_logs"] = logs_text
        log_buffer.clear()
    return response

@app.route("/debug-logs")
def debug_logs():
    """Expose last logs via JSON so browser can print them."""
    logs_text = session.pop("last_logs", "")
    if not logs_text:
        return jsonify({"logs_b64": ""})
    logs_b64 = base64.b64encode(logs_text.encode("utf-8")).decode("ascii")
    return jsonify({"logs_b64": logs_b64})

@app.route("/", methods=["GET", "POST"])
def index():
    available_plugins = get_available_plugins()

    if request.method == "POST":
        site = request.form.get("site")
        query = request.form.get("query")
        limit_raw = request.form.get("limit")

        try:
            limit = int(limit_raw) if limit_raw else None
        except ValueError:
            limit = None

        if site and query:
            filename_safe = query.lower().replace(" ", "_")
            date_str = datetime.now().strftime("%d%m%y_%H%M%S")
            filename = f"{filename_safe}_{site}_{date_str}.csv"
            output_abs_path = os.path.join(STATIC_DIR, filename)

            direct_result = try_run_plugin_direct(site, query, output_abs_path, limit)

            if not direct_result.get("success"):
                command = [
                    "python", "runner.py", "--mode", "modular",
                    "--site", site, "--query", query,
                    "--output", output_abs_path
                ]
                if limit is not None:
                    command.extend(["--limit", str(limit)])

                try:
                    result = subprocess.run(command, capture_output=True, text=True)
                    output_text = result.stdout.strip()
                    try:
                        result_json = json.loads(output_text)
                    except json.JSONDecodeError:
                        session["message"] = f"Failed to parse scraper output: {output_text}"
                        return redirect(url_for("index"))

                    if result_json.get("success"):
                        session["message"] = f"Scraping completed. Output saved to static/{filename}"
                        if os.path.exists(output_abs_path):
                            with open(output_abs_path, newline='', encoding='utf-8') as f:
                                reader = csv.reader(f)
                                rows = list(reader)
                                record_count = len(rows) - 1
                                if record_count > 0:
                                    session["total_records"] = record_count
                                    session["output_file"] = filename
                                    if limit is not None and record_count < limit:
                                        session["message"] += f"<br>Only {record_count} records found out of requested {limit}."
                                else:
                                    session["message"] += "<br>Output file is empty."
                        else:
                            session["message"] += "<br>Output file not found."
                    else:
                        session["message"] = f"Scraper failed: {result_json.get('error', 'Unknown error')}"
                except Exception as e:
                    session["message"] = f"Scraper execution failed: {e}"
                return redirect(url_for("index"))
            else:
                session["message"] = f"Scraping completed. Output saved to static/{filename}"
                if os.path.exists(output_abs_path):
                    with open(output_abs_path, newline='', encoding='utf-8') as f:
                        reader = csv.reader(f)
                        rows = list(reader)
                        record_count = len(rows) - 1
                        if record_count > 0:
                            session["total_records"] = record_count
                            session["output_file"] = filename
                            if limit is not None and record_count < limit:
                                session["message"] += f"<br>Only {record_count} records found out of requested {limit}."
                        else:
                            session["message"] += "<br>Output file is empty."
                else:
                    session["message"] += "<br>Output file not found."
                return redirect(url_for("index"))

    message = session.pop("message", None)
    output_file = session.get("output_file")
    total_records = session.get("total_records")
    headers, table_data = (None, None)
    if output_file:
        headers, table_data = load_table_data(output_file, max_rows=100)

    return render_template(
        "index.html",
        message=message,
        output_file=output_file,
        headers=headers,
        table_data=table_data,
        total_records=total_records,
        available_plugins=available_plugins,
        timestamp=int(time.time())
    )

@app.route("/reset")
def reset():
    session.clear()
    return redirect(url_for("index"))

@app.route("/data/<filename>")
def get_data(filename):
    file_path = os.path.join(STATIC_DIR, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404
    with open(file_path, encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
    return jsonify({"headers": rows[0], "rows": rows[1:]})

# API endpoints for Chrome extension
@app.route("/api/plugins", methods=["GET"])
def api_plugins():
    return jsonify({"plugins": get_available_plugins()})

@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    payload = request.get_json(silent=True) or {}
    site = payload.get("site")
    query = payload.get("query")
    limit = payload.get("limit")
    if not site or not query:
        return jsonify({"success": False, "error": "site and query are required"}), 400

    filename_safe = query.lower().replace(" ", "_")
    date_str = datetime.now().strftime("%d%m%y_%H%M%S")
    filename = f"{filename_safe}_{site}_{date_str}.csv"
    output_abs_path = os.path.join(STATIC_DIR, filename)

    direct_result = try_run_plugin_direct(site, query, output_abs_path, limit)
    if not direct_result.get("success"):
        return jsonify({"success": False, "error": direct_result.get("error", "Unknown error")}), 500

    return jsonify({
        "success": True,
        "count": direct_result.get("count", 0),
        "file": f"static/{filename}",
        "file_url": abs_url(f"static/{filename}")
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)


































"""
# app.py

from flask import Flask, render_template, request, redirect, url_for
import subprocess
import os
from datetime import datetime
import csv

app = Flask(__name__)

last_output_file = None
last_table_data = None
last_headers = None
last_message = None

@app.route("/", methods=["GET", "POST"])
def index():
    global last_output_file, last_table_data, last_headers, last_message

    if request.method == "POST":
        site = request.form.get("site")
        query = request.form.get("query")

        if site and query:
            filename_safe = query.lower().replace(" ", "_")
            date_str = datetime.now().strftime("%d%m%y")
            output_file = f"{filename_safe}_{site}_{date_str}.csv"
            output_path = os.path.join("static", output_file)

            command = ["python", "runner.py", "--mode", "modular", "--site", site, "--query", query, "--output", output_path]
            try:
                subprocess.run(command, check=True)
                last_message = f"✅ Scraping completed. Output saved to static/{output_file}"
                if os.path.exists(output_path):
                    with open(output_path, newline='', encoding='utf-8') as f:
                        reader = csv.reader(f)
                        rows = list(reader)
                        if len(rows) > 1:
                            last_headers = rows[0]
                            last_table_data = rows[1:]
                            last_output_file = output_file
                        else:
                            last_message += "<br>⚠️ Output file is empty."
                            last_output_file = None
                            last_table_data = None
                else:
                    last_message += "<br>⚠️ Output file not found."
                    last_output_file = None
                    last_table_data = None
            except subprocess.CalledProcessError:
                last_message = "Scraper failed. Please check logs."
        else:
            last_message = "⚠️ Please fill all fields."

        return redirect(url_for('index'))

    return render_template("index.html",
                           message=last_message,
                           output_file=last_output_file,
                           headers=last_headers,
                           table_data=last_table_data)

if __name__ == "__main__":
    app.run(debug=True)

"""





