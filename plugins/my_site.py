# plugins/my_site.py
description = "Scrape data from MySite"

def run_scraper(query, output_file=None, limit=None):
    # 1) do your scraping (honor `limit`)
    rows = [
        {"ColA": "value1", "ColB": "value2"},
        # ...
    ]
    if limit is not None:
        rows = rows[:int(limit)]

    # 2) write CSV to `output_file` (REQUIRED)
    import os, csv
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys() if rows else ["ColA","ColB"])
        w.writeheader()
        w.writerows(rows)

    # 3) return the standard shape
    return {"file": output_file, "data": rows}
