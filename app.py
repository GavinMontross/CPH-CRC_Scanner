#!/usr/bin/env python3
import os
import csv
import threading
import logging
import shutil
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_from_directory
import requests
from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

load_dotenv()

# ----- Config -----
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = Flask(__name__, static_folder="static", template_folder="templates")

SNIPE_URL = os.getenv("SNIPE_URL")
SNIPE_TOKEN = os.getenv("SNIPE_API_TOKEN")
CURRENT_CSV = os.getenv("CURRENT_CSV", "current_scan.csv")
COMPLETED_FOLDER = os.getenv("COMPLETED_FOLDER", "completed_scans")
CSV_HEADERS = os.getenv(
    "CSV_HEADERS", "Equipment Type,Item Description,Serial Number,Temple Tag"
).split(",")

SNIPE_VERIFY_SSL = os.getenv("SNIPE_VERIFY_SSL", "true").lower() == "true"
SNIPE_TIMEOUT = int(os.getenv("SNIPE_TIMEOUT_SECONDS", "5"))

CSV_LOCK = threading.Lock()
SEEN_SERIALS = set()

if not os.path.exists(COMPLETED_FOLDER):
    os.makedirs(COMPLETED_FOLDER)


# ----- Helpers -----
def load_existing_serials():
    """Reads current file to prevent dupes in the 'Serial Number' column."""
    SEEN_SERIALS.clear()
    if os.path.exists(CURRENT_CSV):
        try:
            with open(CURRENT_CSV, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Column 3 is Serial Number
                    if "Serial Number" in row and row["Serial Number"]:
                        SEEN_SERIALS.add(row["Serial Number"].strip().lower())
        except Exception as e:
            logging.error(f"Error loading serials: {e}")


def ensure_csv():
    with CSV_LOCK:
        if not os.path.exists(CURRENT_CSV) or os.path.getsize(CURRENT_CSV) == 0:
            with open(CURRENT_CSV, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(CSV_HEADERS)


def append_row(data):
    ensure_csv()
    # Check duplicate on strict Serial Number
    serial = data.get("Serial Number", "").strip()

    if serial and serial.lower() in SEEN_SERIALS:
        return False, "Duplicate Serial detected in this batch."

    with CSV_LOCK:
        try:
            # Order strictly: [Type, Desc, Serial, Tag]
            row = [
                data.get("Equipment Type", ""),
                data.get("Item Description", ""),
                serial,
                data.get("Temple Tag", "N/A"),
            ]

            with open(CURRENT_CSV, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(row)

            if serial:
                SEEN_SERIALS.add(serial.lower())
            return True, "Saved"
        except Exception as e:
            return False, str(e)


def lookup_snipe(term):
    if not SNIPE_URL or not SNIPE_TOKEN:
        return None
    if not term:
        return None

    base_api = SNIPE_URL.rstrip("/")
    if base_api.endswith("/hardware"):
        base_api = base_api.replace("/hardware", "")

    headers = {"Authorization": f"Bearer {SNIPE_TOKEN}", "Accept": "application/json"}

    def get_data(url, params=None):
        try:
            r = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=SNIPE_TIMEOUT,
                verify=SNIPE_VERIFY_SSL,
            )
            if r.status_code == 200:
                d = r.json()
                if "id" in d:
                    return [d]
                return d.get("rows", [])
            return []
        except:
            return []

    # Try Tag -> Serial -> Search
    rows = get_data(f"{base_api}/hardware/bytag/{term}")
    if not rows:
        rows = get_data(f"{base_api}/hardware/byserial/{term}")
    if not rows:
        rows = get_data(f"{base_api}/hardware", params={"search": term, "limit": 1})

    if rows:
        hw = rows[0]
        # Construct Description: "Dell Optiplex 7050"
        manuf = hw.get("manufacturer", {}).get("name", "")
        model = hw.get("model", {}).get("name", "")
        full_desc = f"{manuf} {model}".strip()

        return {
            "Equipment Type": hw.get("category", {}).get(
                "name", "Computer"
            ),  # Default to Computer if empty
            "Item Description": full_desc,
            "Serial Number": hw.get("serial", ""),
            "Temple Tag": hw.get("asset_tag", ""),
            "found_in_snipe": True,
        }
    return None


# ----- Routes -----
@app.route("/")
def index():
    load_existing_serials()
    return render_template("index.html")


@app.route("/lookup", methods=["POST"])
def api_lookup():
    data = request.json or {}
    term = data.get(
        "serial", ""
    ).strip()  # This is the raw input (could be tag or serial)

    # Snipe Lookup
    res = lookup_snipe(term)

    if res:
        # We found it! Return the CLEAN DATA from Snipe
        # This fixes the issue: If you scanned a Tag, 'res' contains the real Serial.
        # We return that real Serial so the frontend uses it.
        return jsonify(res)

    # Not found in Snipe
    # Heuristic: Is this input likely a Tag or a Serial?
    # Temple Tags usually start with CPH or are just digits.
    # Serials are usually alphanumeric.
    is_likely_tag = term.upper().startswith("CPH") or (term.isdigit() and len(term) < 8)

    return jsonify(
        {
            "Equipment Type": "Computer",  # Default guess
            "Item Description": "",
            "Serial Number": (
                "" if is_likely_tag else term
            ),  # If it looks like a tag, leave serial blank
            "Temple Tag": (
                term if is_likely_tag else ""
            ),  # If it looks like a tag, put in tag field
            "found_in_snipe": False,
        }
    )


@app.route("/add", methods=["POST"])
def api_add():
    data = request.json or {}
    success, msg = append_row(data)
    return jsonify({"ok": success, "error": msg})


@app.route("/recent", methods=["GET"])
def api_recent():
    ensure_csv()
    try:
        with CSV_LOCK:
            with open(CURRENT_CSV, "r", encoding="utf-8") as f:
                reader = list(csv.reader(f))
                if len(reader) > 1:
                    last_rows = reader[1:][-5:]
                    last_rows.reverse()
                    return jsonify({"items": last_rows})
    except:
        pass
    return jsonify({"items": []})


@app.route("/finalize", methods=["POST"])
def api_finalize():
    with CSV_LOCK:
        if not os.path.exists(CURRENT_CSV):
            return jsonify({"error": "No data to finalize"}), 400

        # 1. Generate Filename (YYYYMMDD-cph-crc.xlsx)
        today_str = datetime.now().strftime("%Y%m%d")
        base_name = f"{today_str}-cph-crc"
        filename = f"{base_name}.xlsx"
        dest_path = os.path.join(COMPLETED_FOLDER, filename)

        # Handle duplicate filenames (append counter)
        counter = 1
        while os.path.exists(dest_path):
            filename = f"{base_name}-{counter}.xlsx"
            dest_path = os.path.join(COMPLETED_FOLDER, filename)
            counter += 1

        try:
            # 2. Convert CSV to Excel with Formatting
            wb = Workbook()
            ws = wb.active
            ws.title = "Scan Data"

            with open(CURRENT_CSV, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                for row_idx, row in enumerate(reader, 1):
                    for col_idx, value in enumerate(row, 1):
                        ws.cell(row=row_idx, column=col_idx, value=value)

            # 3. Auto-fit Columns
            for column_cells in ws.columns:
                length = max(len(str(cell.value) or "") for cell in column_cells)
                # Add a little extra padding
                ws.column_dimensions[
                    get_column_letter(column_cells[0].column)
                ].width = (length + 2)

            # 4. Save and Cleanup
            wb.save(dest_path)

            # Reset the current session
            os.remove(CURRENT_CSV)
            SEEN_SERIALS.clear()

            return jsonify({"ok": True, "filename": filename})

        except Exception as e:
            logging.error(f"Finalize Error: {e}")
            return jsonify({"error": str(e)}), 500


@app.route("/completed_files", methods=["GET"])
def api_completed_files():
    files = []
    if os.path.exists(COMPLETED_FOLDER):
        # Look for .xlsx OR .csv (in case you have old ones)
        files = [
            f
            for f in os.listdir(COMPLETED_FOLDER)
            if f.endswith(".xlsx") or f.endswith(".csv")
        ]
    files.sort(reverse=True)
    return jsonify({"files": files})


@app.route("/download/<path:filename>")
def download_file(filename):
    return send_from_directory(COMPLETED_FOLDER, filename, as_attachment=True)


if __name__ == "__main__":
    ensure_csv()
    load_existing_serials()
    app.run(host="0.0.0.0", port=5000, debug=True)
