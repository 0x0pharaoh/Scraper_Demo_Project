# app.py

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import subprocess
import os
import csv
import time
from datetime import datetime

app = Flask(__name__)
app.secret_key = "your-secret-key"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

def get_available_plugins():
    plugin_dir = os.path.join(BASE_DIR, "plugins")
    return [
        f[:-3] for f in os.listdir(plugin_dir)
        if f.endswith(".py") and f != "__init__.py"
    ]

@app.route("/", methods=["GET", "POST"])
def index():
    available_plugins = get_available_plugins()

    if request.method == "POST":
        site = request.form.get("site")
        query = request.form.get("query")
        limit = request.form.get("limit")

        if site and query:
            filename_safe = query.lower().replace(" ", "_")
            date_str = datetime.now().strftime("%d%m%y_%H%M%S")
            filename = f"{filename_safe}_{site}_{date_str}.csv"
            output_rel_path = os.path.join("static", filename)
            output_abs_path = os.path.join(STATIC_DIR, filename)

            command = ["python", "runner.py", "--mode", "modular", "--site", site, "--query", query, "--output", output_abs_path]
            if limit:
                command.extend(["--limit", limit])

            try:
                result = subprocess.run(command, check=True, capture_output=True, text=True)
                session["message"] = f"Scraping completed. Output saved to static/{filename}"

                if os.path.exists(output_abs_path):
                    with open(output_abs_path, newline='', encoding='utf-8') as f:
                        reader = csv.reader(f)
                        rows = list(reader)
                        record_count = len(rows) - 1

                        if record_count > 0:
                            # Store only headers and filename, NOT all data rows
                            session["headers"] = rows[0]
                            session["output_file"] = filename
                            if limit:
                                try:
                                    int_limit = int(limit)
                                    if record_count < int_limit:
                                        session["message"] += f"<br>Only {record_count} records found out of requested {int_limit}."
                                except ValueError:
                                    session["message"] += "<br>⚠️ Invalid limit value."
                        else:
                            session["message"] += "<br>⚠️ Output file is empty."
                    return redirect(url_for("index"))
                else:
                    session["message"] += "<br>⚠️ Output file not found."

            except subprocess.CalledProcessError as e:
                session["message"] = f"❌ Scraper failed. Error: {e.stderr or e.stdout or 'Check logs.'}"
            return redirect(url_for("index"))

    message = session.pop("message", None)
    output_file = session.pop("output_file", None)
    headers = session.pop("headers", None)
    # No more table_data in session

    return render_template(
        "index.html",
        message=message,
        output_file=output_file,
        headers=headers,
        table_data=None,  # Removed
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





