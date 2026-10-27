#!/usr/bin/env python3
import os
import csv
import json
import base64
import secrets
import urllib.parse
import threading
import logging
import shutil
from datetime import datetime, timedelta
import io
import re
from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    send_from_directory,
    session,
    redirect,
    url_for,
)
import requests
from dotenv import load_dotenv
from openpyxl import load_workbook, Workbook
from openpyxl.utils import get_column_letter

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

try:
    import pytesseract
    from PIL import Image, ImageEnhance, ImageOps, ImageFilter
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

try:
    import zxingcpp
    ZXING_AVAILABLE = True
except ImportError:
    ZXING_AVAILABLE = False

try:
    from pyzbar.pyzbar import decode as decode_barcode
    BARCODE_AVAILABLE = True
except ImportError:
    BARCODE_AVAILABLE = False


load_dotenv()

# ----- Logging & App Setup -----
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = Flask(__name__, static_folder="static", template_folder="templates")

# Flask Session Security
app.secret_key = os.getenv(
    "FLASK_SECRET_KEY", "cph-crc-scanner-session-secret-9b2f8a1c4e7d0"
)
app.config["SESSION_COOKIE_NAME"] = "cph_crc_session"
app.config["SESSION_COOKIE_PATH"] = "/"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Must be True on HTTPS so Safari and iOS devices don't drop the cookie
app.config["SESSION_COOKIE_SECURE"] = (
    os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"
)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

# --- Environment Variables ---
SNIPE_URL = os.getenv("SNIPE_URL")
SNIPE_TOKEN = os.getenv("SNIPE_API_TOKEN")
CURRENT_CSV = os.getenv("CURRENT_CSV", "current_scan.csv")
COMPLETED_FOLDER = os.getenv("COMPLETED_FOLDER", "completed_scans")
CSV_HEADERS = os.getenv(
    "CSV_HEADERS", "Equipment Type,Item Description,Serial Number,Temple Tag"
).split(",")

TEMPLATE_FILE = os.getenv("TEMPLATE_XLSX", "template.xlsx")

SNIPE_VERIFY_SSL = os.getenv("SNIPE_VERIFY_SSL", "true").lower() == "true"
SNIPE_TIMEOUT = int(os.getenv("SNIPE_TIMEOUT_SECONDS", "5"))

CSV_LOCK = threading.Lock()
USER_PHOTOS = {}  # In-memory store for profile photos to keep session cookie under 4KB limit

if not os.path.exists(COMPLETED_FOLDER):
    os.makedirs(COMPLETED_FOLDER)

# --- Microsoft Entra ID / Temple SSO Settings ---
SSO_ENABLED = os.getenv("SSO_ENABLED", "true").lower() == "true"
SSO_TENANT_ID = os.getenv("SSO_TENANT_ID") or os.getenv(
    "AZURE_TENANT_ID", "716e81ef-b522-4473-8e31-10bd02ccf6e5"
)
SSO_CLIENT_ID = os.getenv("SSO_CLIENT_ID") or os.getenv(
    "AZURE_CLIENT_ID", "28e809ea-19ba-4028-89c7-c1d016e4836c"
)
SSO_CLIENT_SECRET = os.getenv("SSO_CLIENT_SECRET") or os.getenv(
    "AZURE_CLIENT_SECRET"
)
SSO_REDIRECT_URI = os.getenv("SSO_REDIRECT_URI") or os.getenv("AZURE_REDIRECT_URI", "")
SSO_SCOPES = "openid profile email User.Read"

# Authorized AD / Entra Group Object IDs
# - 0d1a93f6-3804-421e-a73f-2ba7bf0bb889: CPH-CPHIT-Administrators
# - 78f9833c-b9af-477b-8974-b9a98314505e: CPH-CPHIT-Student Workers
DEFAULT_ALLOWED_GROUPS = [
    "0d1a93f6-3804-421e-a73f-2ba7bf0bb889",
    "78f9833c-b9af-477b-8974-b9a98314505e",
]
env_groups = os.getenv("ALLOWED_ADMIN_GROUPS")
if env_groups:
    ALLOWED_GROUPS = [g.strip() for g in env_groups.split(",") if g.strip()]
else:
    ALLOWED_GROUPS = DEFAULT_ALLOWED_GROUPS


# ----- SSO Helpers -----
def get_redirect_uri():
    """Gets or dynamically builds the OAuth2 redirect URI."""
    if SSO_REDIRECT_URI:
        return SSO_REDIRECT_URI
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    # Microsoft Entra ID strictly requires https:// for all non-localhost/non-127.0.0.1 addresses
    if proto == "http" and not (host.startswith("localhost") or host.startswith("127.0.0.1")):
        proto = "https"
    path = os.getenv("SSO_REDIRECT_PATH", "/auth/callback.php")
    return f"{proto}://{host}{path}"


def exchange_code_for_token(code, redirect_uri):
    """Exchanges authorization code for tokens via Microsoft token endpoint."""
    token_url = f"https://login.microsoftonline.com/{SSO_TENANT_ID}/oauth2/v2.0/token"
    payload = {
        "client_id": SSO_CLIENT_ID,
        "scope": SSO_SCOPES,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "client_secret": SSO_CLIENT_SECRET,
    }
    try:
        resp = requests.post(token_url, data=payload, timeout=10)
        if resp.status_code == 200:
            return resp.json(), None
        return None, f"HTTP {resp.status_code}: {resp.text}"
    except Exception as e:
        return None, str(e)


def decode_jwt(token):
    """Decodes JWT payload without signature verification (signature verified by TLS/token endpoint)."""
    try:
        parts = token.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            return json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
    except Exception as e:
        logging.warning(f"Error decoding JWT: {e}")
    return {}


def check_user_group_membership(access_token, allowed_group_ids, user_email=""):
    """
    Checks if the authenticated user is a member of any of the allowed AD/Entra groups
    via Microsoft Graph checkMemberGroups API.
    """
    if not allowed_group_ids:
        logging.info(f"No allowed groups specified; allowing access for {user_email}.")
        return True

    url = "https://graph.microsoft.com/v1.0/me/checkMemberGroups"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    body = {"groupIds": allowed_group_ids}

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            matching_groups = data.get("value", [])
            logging.info(
                f"Group check for {user_email}: Checked {len(allowed_group_ids)} groups -> Found {len(matching_groups)} matches: {matching_groups}"
            )
            if matching_groups:
                return True

            # If no matches found, query the user's actual groups to help diagnose
            try:
                mg_resp = requests.post(
                    "https://graph.microsoft.com/v1.0/me/getMemberGroups",
                    headers=headers,
                    json={"securityEnabledOnly": False},
                    timeout=5,
                )
                if mg_resp.status_code == 200:
                    actual_groups = mg_resp.json().get("value", [])
                    logging.warning(
                        f"User {user_email} is NOT in allowed groups ({allowed_group_ids}). User's actual groups ({len(actual_groups)}): {actual_groups[:15]}"
                    )
                else:
                    logging.warning(
                        f"Could not retrieve actual groups for {user_email}: HTTP {mg_resp.status_code} - {mg_resp.text}"
                    )
            except Exception as e:
                logging.debug(f"Failed to fetch getMemberGroups: {e}")

            return False
        else:
            logging.error(f"Graph API Group Check Failed: HTTP {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        logging.error(f"Graph API Group Check Exception: {e}")
        return False


def fetch_user_photo(access_token):
    """Fetches user profile photo from Microsoft Graph as base64."""
    if not access_token:
        return None
    try:
        url = "https://graph.microsoft.com/v1.0/me/photo/$value"
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200 and resp.content:
            return base64.b64encode(resp.content).decode("utf-8")
    except Exception as e:
        logging.debug(f"User photo fetch failed (likely no photo set): {e}")
    return None


# ----- Application Helpers -----
def ensure_csv():
    """Ensures the CSV exists with headers."""
    with CSV_LOCK:
        if not os.path.exists(CURRENT_CSV) or os.path.getsize(CURRENT_CSV) == 0:
            with open(CURRENT_CSV, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(CSV_HEADERS)


snipe_session = requests.Session()

# OCR Character Confusion Map (for Ground-Truth Snipe-IT Validation)
CONFUSION_MAP = {
    '0': ['O', 'Q', 'D'],
    'O': ['0', 'Q', 'D'],
    'Q': ['0', 'O'],
    '1': ['I', 'L'],
    'I': ['1', 'L'],
    'L': ['1', 'I'],
    '5': ['S'],
    'S': ['5'],
    '8': ['B'],
    'B': ['8'],
    '2': ['Z'],
    'Z': ['2'],
    'U': ['V'],
    'V': ['U'],
}

def generate_confusion_variants(term, max_variants=10):
    """Generates plausible serial variants based on common OCR character confusion."""
    clean = term.strip().upper()
    variants = [clean]
    seen = {clean}

    # High-priority Lenovo serial heuristic: PC-10... -> PC-1Q...
    if re.search(r'\b[A-Z]{2}[-]?[1I][0O]', clean):
        q_fix = re.sub(r'([A-Z]{2}[-]?[1I])[0O]', r'\g<1>Q', clean)
        if q_fix not in seen:
            variants.append(q_fix)
            seen.add(q_fix)

    # Single-character substitution variants
    for idx, ch in enumerate(clean):
        if ch in CONFUSION_MAP:
            for alt in CONFUSION_MAP[ch]:
                variant = clean[:idx] + alt + clean[idx+1:]
                if variant not in seen:
                    variants.append(variant)
                    seen.add(variant)
                if len(variants) >= max_variants:
                    break
        if len(variants) >= max_variants:
            break

    return variants

def format_temple_tag(tag):
    """Normalizes Temple asset tag by stripping common prefixes without truncation."""
    if not tag:
        return ""
    tag_str = str(tag).strip()
    clean = re.sub(r'^(?:CPH|TU)[-_]?', '', tag_str, flags=re.I)
    return clean.strip()


def append_row(data):
    ensure_csv()
    target_serial = data.get("Serial Number", "").strip()

    with CSV_LOCK:
        if target_serial:
            try:
                with open(CURRENT_CSV, "r", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    next(reader, None)  # Skip Header
                    for row in reader:
                        if (
                            len(row) > 2
                            and row[2].strip().lower() == target_serial.lower()
                        ):
                            return False, "Duplicate Serial detected in this batch."
            except Exception as e:
                logging.error(f"Read error during dupe check: {e}")

        try:
            row = [
                data.get("Equipment Type", ""),
                data.get("Item Description", ""),
                target_serial,
                data.get("Temple Tag", "N/A"),
            ]

            with open(CURRENT_CSV, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(row)

            return True, "Saved"
        except Exception as e:
            return False, str(e)


# --- Common Enterprise Hardware Catalog & Device Classifier ---
HARDWARE_CATALOG = {
    # HP Enterprise & Pro Printers
    "CE957A": ("Printer", "HP LaserJet Enterprise 500 Color M551dn"),
    "CE958A": ("Printer", "HP LaserJet Enterprise 500 Color M551xh"),
    "CE956A": ("Printer", "HP LaserJet Enterprise 500 Color M551n"),
    "CE525A": ("Printer", "HP LaserJet Pro P1102w"),
    "CE866A": ("Printer", "HP Color LaserJet Pro CP1025nw"),
    "CF284A": ("Printer", "HP LaserJet Pro 400 M401dn"),
    "CF285A": ("Printer", "HP LaserJet Pro 400 M401dw"),
    "CF286A": ("Printer", "HP LaserJet Pro 400 M401dne"),
    "CF068A": ("Printer", "HP LaserJet Enterprise 600 M601dn"),
    "CF069A": ("Printer", "HP LaserJet Enterprise 600 M602dn"),
    "CF070A": ("Printer", "HP LaserJet Enterprise 600 M603dn"),
    "CF116A": ("Printer", "HP LaserJet Enterprise 500 MFP M525dn"),
    "CZ244A": ("Printer", "HP LaserJet Pro MFP M127fn"),
    "CZ181A": ("Printer", "HP LaserJet Pro MFP M125nw"),
    "G3Q57A": ("Printer", "HP LaserJet Pro M402dn"),
    "G3Q58A": ("Printer", "HP LaserJet Pro M402dw"),
    "W1A53A": ("Printer", "HP LaserJet Pro M404dn"),
    "W1A56A": ("Printer", "HP LaserJet Pro M404dw"),
    "W1A28A": ("Printer", "HP Color LaserJet Pro MFP M479fdw"),
    "W1A29A": ("Printer", "HP Color LaserJet Pro MFP M479fdn"),
    "W1A30A": ("Printer", "HP Color LaserJet Pro MFP M479dw"),
    "Q5911A": ("Printer", "HP LaserJet 4250dtn"),
    "Q5401A": ("Printer", "HP LaserJet 4250n"),
    "Q5912A": ("Printer", "HP LaserJet 4350dtn"),
    "CC434A": ("Printer", "HP Color LaserJet CP2025dn"),
    "CB494A": ("Printer", "HP Color LaserJet CP1518ni"),
    "B5L24A": ("Printer", "HP Color LaserJet Enterprise M553n"),
    "B5L25A": ("Printer", "HP Color LaserJet Enterprise M553dn"),
    "B5L26A": ("Printer", "HP Color LaserJet Enterprise M553x"),
    "F2A68A": ("Printer", "HP LaserJet Enterprise M506n"),
    "F2A69A": ("Printer", "HP LaserJet Enterprise M506dn"),
    "1PV87A": ("Printer", "HP LaserJet Enterprise M507n"),
    "1PV88A": ("Printer", "HP LaserJet Enterprise M507dn"),
    "7KW75A": ("Printer", "HP Color LaserJet Enterprise MFP M480f"),
    "3PZ55A": ("Printer", "HP LaserJet Enterprise MFP M430f"),
    "J7Z99A": ("Printer", "HP Color LaserJet Enterprise M652dn"),
    "J8A04A": ("Printer", "HP Color LaserJet Enterprise M653dn"),
    "K0Q18A": ("Printer", "HP LaserJet Enterprise M608dn"),
    "K0Q19A": ("Printer", "HP LaserJet Enterprise M609dn"),
    # Dell Docks
    "WD15": ("Docking Station", "Dell Dock WD15"),
    "WD19": ("Docking Station", "Dell Dock WD19"),
    "WD19S": ("Docking Station", "Dell Dock WD19S"),
    "WD19TB": ("Docking Station", "Dell Thunderbolt Dock WD19TB"),
    "WD19TBS": ("Docking Station", "Dell Thunderbolt Dock WD19TBS"),
    "WD22TB4": ("Docking Station", "Dell Thunderbolt 4 Dock WD22TB4"),
    "D6000": ("Docking Station", "Dell Universal Dock D6000"),
    "TB16": ("Docking Station", "Dell Thunderbolt Dock TB16"),
    "DA300": ("Docking Station", "Dell Mobile Adapter DA300"),
    "DA310": ("Docking Station", "Dell Mobile Adapter DA310"),
    "HD22Q": ("Docking Station", "Dell Dual Charge Dock HD22Q"),
    # Lenovo Docks
    "40AY": ("Docking Station", "Lenovo ThinkPad Universal USB-C Dock"),
    "40AN": ("Docking Station", "Lenovo ThinkPad Thunderbolt 3 Workstation Dock Gen 2"),
    "40B0": ("Docking Station", "Lenovo ThinkPad Universal Thunderbolt 4 Dock"),
    "40AJ": ("Docking Station", "Lenovo ThinkPad Ultra Docking Station"),
    "40AF": ("Docking Station", "Lenovo ThinkPad Hybrid USB-C with USB-A Dock"),
    "40AS": ("Docking Station", "Lenovo ThinkPad USB-C Dock Gen 2"),
    "40AC": ("Docking Station", "Lenovo ThinkPad Thunderbolt 3 Dock"),
    "40A9": ("Docking Station", "Lenovo ThinkPad USB-C Dock"),
    # Lenovo ThinkPad & ThinkCentre Machine Types
    "20VF": ("Laptop", "Lenovo ThinkBook 14 G2 ARE"),
    "20U9": ("Laptop", "Lenovo ThinkPad X1 Carbon Gen 8"),
    "20XW": ("Laptop", "Lenovo ThinkPad X1 Carbon Gen 9"),
    "21CB": ("Laptop", "Lenovo ThinkPad X1 Carbon Gen 10"),
    "21AH": ("Laptop", "Lenovo ThinkPad T14 Gen 3"),
    "20UD": ("Laptop", "Lenovo ThinkPad T14 Gen 1"),
    "20W0": ("Laptop", "Lenovo ThinkPad T14 Gen 2"),
    "20L7": ("Laptop", "Lenovo ThinkPad T480s"),
    "20L5": ("Laptop", "Lenovo ThinkPad T480"),
    "20N2": ("Laptop", "Lenovo ThinkPad T490"),
    "10MR": ("Computer", "Lenovo ThinkCentre M710q Tiny"),
    "10T7": ("Computer", "Lenovo ThinkCentre M720q Tiny"),
    "11DT": ("Computer", "Lenovo ThinkCentre M70q Gen 1"),
    "11MY": ("Computer", "Lenovo ThinkCentre M70q Gen 2"),
    "11T3": ("Computer", "Lenovo ThinkCentre M70q Gen 3"),
    "30E0": ("Computer", "Lenovo ThinkStation P340 Tiny"),
    # Apple Hardware
    "A2338": ("Laptop", "Apple MacBook Pro 13-inch (M1/M2)"),
    "A2442": ("Laptop", "Apple MacBook Pro 14-inch (M1 Pro/Max)"),
    "A2485": ("Laptop", "Apple MacBook Pro 16-inch (M1 Pro/Max)"),
    "A2337": ("Laptop", "Apple MacBook Air (M1)"),
    "A2681": ("Laptop", "Apple MacBook Air 13-inch (M2)"),
    "A1993": ("Computer", "Apple Mac mini (2018)"),
    "A2330": ("Computer", "Apple Mac mini (M1)"),
    "A2686": ("Computer", "Apple Mac mini (M2)"),
    "A2615": ("Computer", "Apple Mac Studio"),
    "A2115": ("Computer", "Apple iMac 27-inch Retina 5K"),
}

def infer_hardware_from_barcode(code):
    """
    Infers Equipment Type and Item Description from barcode values,
    manufacturer part numbers, and serial patterns.
    """
    if not code:
        return None
    raw_str = str(code).strip()
    # Strip trailing option/regional codes like #ABA, #201, #B19, etc.
    base_code = raw_str.split('#')[0].strip()
    clean = re.sub(r'[^A-Z0-9]', '', base_code.upper())
    if not clean:
        return None

    # 1. Exact catalog match
    if clean in HARDWARE_CATALOG:
        return HARDWARE_CATALOG[clean]

    # 2. Lenovo 1S barcode format (e.g. 1S20VFPC1QLZMC)
    if clean.startswith("1S") and len(clean) >= 14:
        mach = clean[2:6]
        if mach in HARDWARE_CATALOG:
            return HARDWARE_CATALOG[mach]
        if mach.startswith(('20', '21')):
            return ("Laptop", f"Lenovo ThinkPad/ThinkBook ({mach})")
        if mach.startswith(('10', '11', '12')):
            return ("Computer", f"Lenovo ThinkCentre ({mach})")
        if mach.startswith('30'):
            return ("Computer", f"Lenovo ThinkStation ({mach})")

    # 3. Dell PPID barcode (e.g. CN-0P2419-74261-89B-1234 or CN0P2419...)
    if len(clean) >= 20 and clean[:2] in ('CN', 'KR', 'MX', 'JP', 'US', 'VN') and clean[2] == '0':
        dell_pn = clean[3:8]
        if dell_pn in HARDWARE_CATALOG:
            return HARDWARE_CATALOG[dell_pn]
        if re.match(r'^[PUESACDFW][0-9]{2}[0-9]{2}[A-Z]{0,2}$', dell_pn):
            return ("Monitor", f"Dell Monitor ({dell_pn})")

    # 4. HP Printer product number patterns (e.g. CE957A, CF284A, W1A53A)
    if re.match(r'^[CDEGJKMQTVW1347][A-Z0-9]{1,3}[0-9]{2,3}[A-Z]$', clean) and len(clean) in (6, 7):
        return ("Printer", f"HP LaserJet Printer ({clean})")

    # 5. HP Printer serial format (10 chars, starts with factory country code)
    if len(clean) == 10 and clean[:2] in ('CN', 'VN', 'MY', 'TH', 'SG', 'MX', 'CR', 'JP', 'US', 'PH'):
        return ("Printer", "HP LaserJet Printer")

    # 6. Brother Printer patterns (HL-..., MFC-..., DCP-..., QL-...)
    if re.match(r'^(HL|MFC|DCP|QL)[0-9A-Z]+$', clean):
        return ("Printer", f"Brother Printer ({clean})")

    # 7. Zebra Label Printer patterns (ZD..., GX..., GK..., ZT..., LP...)
    if re.match(r'^(ZD|GX|GK|ZT|LP|TLP)[0-9A-Z]+$', clean):
        return ("Printer", f"Zebra Label Printer ({clean})")

    # 8. Epson Printer patterns (ET-..., WF-..., XP-..., TM-...)
    if re.match(r'^(ET|WF|XP|TM|LQ)[0-9A-Z]+$', clean):
        return ("Printer", f"Epson Printer ({clean})")

    # 9. Lexmark Printer patterns (MS..., MX..., CS..., CX...)
    if re.match(r'^(MS|MX|CS|CX)[0-9]{3,4}[A-Z0-9]*$', clean):
        return ("Printer", f"Lexmark Printer ({clean})")

    # 10. Dell Monitor patterns (P2419H, U2419H, U2720Q, E2420H, S2721HS, etc.)
    if re.match(r'^[PUESACDFW][0-9]{2}[0-9]{2}[A-Z]{0,3}$', clean) and len(clean) in (5, 6, 7):
        return ("Monitor", f"Dell Monitor ({clean})")

    # 11. Dell Dock patterns (WD15, WD19, WD19S, WD22, D6000, etc.)
    if re.match(r'^(WD15|WD19|WD19S|WD19TB|WD19TBS|WD22TB4|D6000|TB16|DA300|DA310|HD22Q)$', clean):
        return ("Docking Station", f"Dell Dock ({clean})")

    # 12. Lenovo ThinkPad Dock machine types (40AY, 40AN, 40B0, etc.)
    if re.match(r'^40[A-Z0-9]{2}$', clean):
        return ("Docking Station", f"Lenovo ThinkPad Dock ({clean})")

    # 13. Lenovo machine types without 1S prefix (e.g. 20VF, 11DT, 10MR)
    if re.match(r'^(20|21)[A-Z0-9]{2}$', clean):
        return ("Laptop", f"Lenovo ThinkPad/ThinkBook ({clean})")
    if re.match(r'^(10|11|12)[A-Z0-9]{2}$', clean):
        return ("Computer", f"Lenovo ThinkCentre ({clean})")
    if re.match(r'^30[A-Z0-9]{2}$', clean):
        return ("Computer", f"Lenovo ThinkStation ({clean})")

    # 14. Cisco Network Switches
    if re.match(r'^(WSC|C9200|C9300|C9500|SG[0-9]{3}|SF[0-9]{3})[A-Z0-9]*$', clean):
        return ("Other", f"Cisco Switch ({clean})")

    # 15. APC & Tripp Lite UPS
    if re.match(r'^(SMT|SMX|SUA|BR|SURT)[0-9]{3,4}[A-Z0-9]*$', clean):
        return ("Other", f"APC Smart-UPS ({clean})")

    return None


def lookup_snipe(term):
    """
    Looks up asset in Snipe-IT with connection pooling, null protection,
    fuzzy search verification, and OCR confusion-map fallback.
    """
    if not SNIPE_URL or not SNIPE_TOKEN or not term:
        return None

    base_api = SNIPE_URL.rstrip("/")
    if base_api.endswith("/hardware"):
        base_api = base_api.replace("/hardware", "")

    headers = {"Authorization": f"Bearer {SNIPE_TOKEN}", "Accept": "application/json"}

    def get_data(url, params=None):
        try:
            r = snipe_session.get(
                url,
                headers=headers,
                params=params,
                timeout=SNIPE_TIMEOUT,
                verify=SNIPE_VERIFY_SSL,
            )
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict) and "id" in d:
                    return [d]
                if isinstance(d, dict) and "rows" in d:
                    return d.get("rows", [])
            return []
        except Exception as e:
            logging.warning(f"[Snipe-IT] Request to {url} failed: {e}")
            return []

    clean_term = term.strip()
    primary_terms = [clean_term]

    if clean_term.upper().startswith("1S") and len(clean_term) >= 14:
        primary_terms.append(clean_term[-8:])
        primary_terms.append(clean_term[2:])

    if "-" in clean_term:
        primary_terms.append(clean_term.replace("-", ""))
    elif len(clean_term) == 8 and clean_term[2] != "-":
        primary_terms.append(f"{clean_term[:2]}-{clean_term[2:]}")

    # Build search list with confusion variants
    all_terms = list(primary_terms)
    for t in primary_terms:
        for var in generate_confusion_variants(t):
            if var not in all_terms:
                all_terms.append(var)

    target_set = {re.sub(r'[^A-Za-z0-9]', '', x).upper() for x in all_terms}

    rows = []
    for t in all_terms:
        # 1. Exact Tag lookup
        rows = get_data(f"{base_api}/hardware/bytag/{t}")
        if rows:
            break
        # 2. Exact Serial lookup
        rows = get_data(f"{base_api}/hardware/byserial/{t}")
        if rows:
            break
        # 3. Verified fuzzy search fallback
        fuzzy_rows = get_data(f"{base_api}/hardware", params={"search": t, "limit": 1})
        if fuzzy_rows:
            cand = fuzzy_rows[0]
            cand_s = re.sub(r'[^A-Za-z0-9]', '', str(cand.get("serial") or "")).upper()
            cand_t = re.sub(r'[^A-Za-z0-9]', '', str(cand.get("asset_tag") or "")).upper()
            if cand_s in target_set or cand_t in target_set:
                rows = fuzzy_rows
                break
            else:
                logging.info(f"[Snipe-IT] Discarded unverified fuzzy match for '{t}': {cand_s} / {cand_t}")

    if rows:
        hw = rows[0]
        # Null-safe access
        manuf_data = hw.get("manufacturer")
        manuf = (manuf_data.get("name", "") if isinstance(manuf_data, dict) else "") or ""

        model_data = hw.get("model")
        model = (model_data.get("name", "") if isinstance(model_data, dict) else "") or ""

        cat_data = hw.get("category")
        cat_name = (cat_data.get("name", "Computer") if isinstance(cat_data, dict) else "Computer") or "Computer"

        full_desc = f"{manuf} {model}".strip()

        return {
            "Equipment Type": cat_name,
            "Item Description": full_desc,
            "Serial Number": hw.get("serial") or "",
            "Temple Tag": format_temple_tag(hw.get("asset_tag") or ""),
        }
    return None


# ----- Authentication Interceptor -----
@app.before_request
def check_sso_authentication():
    if not SSO_ENABLED:
        return None

    # Exempt public static assets & images
    if request.path.startswith(("/static", "/images")):
        return None

    # Exempt authentication flow endpoints
    if (
        request.endpoint in {"login", "auth_callback"}
        or request.path.startswith(("/auth/callback", "/callback", "/login", "/logout"))
    ):
        return None

    # If user is already authenticated
    if session.get("user_logged_in"):
        return None

    # If this is an AJAX / JSON API call, respond with 401
    if (
        request.is_json
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.path.startswith(
            (
                "/ocr",
                "/lookup",
                "/add",
                "/recent",
                "/finalize",
                "/reset_batch",
                "/delete_row",
                "/edit_row",
                "/completed_files",
                "/download",
            )
        )
    ):
        return (
            jsonify(
                {
                    "error": "Authentication required. Please sign in with Temple SSO.",
                    "auth_required": True,
                }
            ),
            401,
        )

    # Save target URL to return user after authentication (never redirect back into auth endpoints)
    if request.path not in {"/login", "/logout"} and not request.path.startswith(("/auth", "/callback")):
        session["next_url"] = request.url
    else:
        session["next_url"] = url_for("index")
    return redirect(url_for("login"))


# ----- Authentication Routes -----
@app.route("/login")
def login():
    if session.get("user_logged_in"):
        return redirect(url_for("index"))

    # Display login template if user explicitly logged out or on error
    if request.args.get("logged_out") or request.args.get("error"):
        return render_template(
            "login.html",
            logged_out=request.args.get("logged_out"),
            error=request.args.get("error"),
            redirect_uri=request.args.get("redirect_uri"),
        )

    # Generate state token to prevent CSRF
    oauth_state = secrets.token_hex(16)
    session["oauth_state"] = oauth_state

    redirect_uri = get_redirect_uri()
    logging.info(f"Initiating Temple SSO login with redirect URI: {redirect_uri}")

    auth_url = f"https://login.microsoftonline.com/{SSO_TENANT_ID}/oauth2/v2.0/authorize"
    params = {
        "client_id": SSO_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": SSO_SCOPES,
        "state": oauth_state,
    }
    return redirect(f"{auth_url}?{urllib.parse.urlencode(params)}")


@app.route("/auth/callback.php")
@app.route("/auth/callback")
@app.route("/callback.php")
@app.route("/callback")
def auth_callback():
    error = request.args.get("error")
    error_desc = request.args.get("error_description")
    if error:
        logging.error(f"OAuth callback error: {error} - {error_desc}")
        return (
            render_template(
                "login.html",
                error=f"{error}: {error_desc}",
                redirect_uri=get_redirect_uri(),
            ),
            400,
        )

    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state:
        return (
            render_template(
                "login.html",
                error="Missing authorization code or state parameter from Microsoft.",
            ),
            400,
        )

    saved_state = session.pop("oauth_state", None)
    if not saved_state or state != saved_state:
        return (
            render_template(
                "login.html",
                error="Invalid state parameter. Possible CSRF attack. Please try again.",
            ),
            400,
        )

    redirect_uri = get_redirect_uri()
    token_data, err = exchange_code_for_token(code, redirect_uri)
    if not token_data or "access_token" not in token_data:
        logging.error(f"Token exchange failed: {err}")
        return (
            render_template(
                "login.html",
                error=f"Unable to authenticate: {err}",
                redirect_uri=redirect_uri,
            ),
            400,
        )

    access_token = token_data.get("access_token")
    id_token = token_data.get("id_token")
    user_data = decode_jwt(id_token) if id_token else {}

    user_email = (
        user_data.get("email")
        or user_data.get("preferred_username")
        or user_data.get("upn")
        or "Unknown"
    )

    # Validate Group Membership against authorized AD/Entra groups
    logging.info(f"Callback received for {user_email}. Verifying membership in {ALLOWED_GROUPS}...")
    is_authorized = check_user_group_membership(
        access_token, ALLOWED_GROUPS, user_email=user_email
    )
    if not is_authorized:
        logging.warning(
            f"Access Denied: User {user_email} is not in allowed groups ({ALLOWED_GROUPS})."
        )
        return render_template("access_denied.html", user_email=user_email), 403

    user_name = user_data.get("name") or user_email.split("@")[0]
    user_photo = fetch_user_photo(access_token)
    if user_photo:
        USER_PHOTOS[user_email] = user_photo

    # Store only essential identifiers in session cookie to strictly stay under 4KB limit
    session["user_logged_in"] = True
    session["user"] = {
        "name": user_name,
        "email": user_email,
    }
    session.permanent = True

    next_url = session.pop("next_url", None)
    # Strictly validate relative path (starts with / and not //) to prevent open redirects or HTML injection
    if not next_url or not str(next_url).startswith("/") or str(next_url).startswith("//") or any(bad in str(next_url).lower() for bad in ("callback", "login", "logout")):
        next_url = url_for("index")

    safe_next_url = urllib.parse.quote(str(next_url), safe="/?=&")

    # Use an HTML 200 response with client-side redirect.
    # This ensures Safari and iOS (ITP) save the session cookie before navigating to the dashboard,
    # completely eliminating the "Too many redirects" loop on Safari.
    html_redirect = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Signing in | CPH CRC Scanner</title>
    <meta http-equiv="refresh" content="0;url={safe_next_url}">
    <script>window.location.replace({json.dumps(safe_next_url)});</script>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; text-align: center; padding-top: 100px; color: #555;">
    <p style="font-size: 1.1rem; font-weight: 600;">Signing in to CPH CRC Scanner...</p>
</body>
</html>"""
    return html_redirect, 200, {"Content-Type": "text/html"}


@app.route("/logout")
def logout():
    session.clear()
    post_logout_uri = url_for("login", logged_out="true", _external=True)
    microsoft_logout_url = (
        f"https://login.microsoftonline.com/{SSO_TENANT_ID}/oauth2/v2.0/logout?"
        + urllib.parse.urlencode({"post_logout_redirect_uri": post_logout_uri})
    )
    return redirect(microsoft_logout_url)


# ----- Application Routes -----
@app.route("/images/<path:filename>")
def serve_image(filename):
    return send_from_directory("images", filename)


@app.route("/")
def index():
    ensure_csv()
    user = session.get("user")
    user_context = None
    if user:
        user_context = dict(user)
        user_context["photo"] = USER_PHOTOS.get(user.get("email"))
    return render_template("index.html", user=user_context)


def parse_equipment_text(raw_text):
    candidates = []
    seen = set()

    def add_candidate(val, label, confidence):
        clean = re.sub(r'[^A-Za-z0-9]', '', val).upper()
        if not clean or len(clean) < 4 or len(clean) > 28:
            return
        if clean in seen:
            return
        seen.add(clean)

        # Standardize formatting (e.g. PC-1QLZMC or MJ-06F3K2)
        formatted = val.upper().replace('—', '-').replace('–', '-').replace(' ', '')
        if len(clean) == 8 and clean[:2].isalpha() and clean[2:].isalnum() and '-' not in formatted:
            formatted = f"{clean[:2]}-{clean[2:]}"

        candidates.append({
            "value": formatted,
            "raw": clean,
            "label": label,
            "confidence": confidence
        })

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

    # 1. High confidence pattern recognition
    for line in lines:
        # Lenovo 1S barcode string (e.g. 1S20UH000FUSPC1QLZMC -> serial is PC1QLZMC)
        m_1s = re.search(r'1S[A-Za-z0-9]{4,16}([A-Za-z0-9]{8})\b', line, re.I)
        if m_1s:
            add_candidate(m_1s.group(1), "Lenovo Serial", 95)

        # S/N, SIN, or Serial Number (handles OCR misreading slash as I/1, dash as em-dash)
        m_sn = re.search(r'(?:S[I/1|l]?N|Serial(?:\s*No\.?|\s*Number)?)(?:\s*\([^)]*\))?\s*[:#\s.-]*\s*([A-Za-z0-9]{2}[-\s—–]?[A-Za-z0-9]{6,12})\b', line, re.I)
        if m_sn:
            sn_val = m_sn.group(1).replace('—', '-').replace('–', '-').replace(' ', '').upper()
            add_candidate(sn_val, "Serial Number", 92)

            # OCR character confusion heuristic: '10' / '1O' vs '1Q' in Lenovo serials (e.g. PC-10LZMC -> PC-1QLZMC)
            if re.search(r'\b[A-Z]{2}[-]?[1I][0O]', sn_val):
                q_corrected = re.sub(r'([A-Z]{2}[-]?[1I])[0O]', r'\g<1>Q', sn_val)
                add_candidate(q_corrected, "Serial Number (Q corrected)", 94)

        # Type / Model Number (e.g. Type Number: 20UH-000FUS)
        m_type = re.search(r'(?:Type(?:\s*No\.?|\s*Number)?|Model)\s*[:#\s.-]*\s*([A-Za-z0-9]{4}[-\s—–]?[A-Za-z0-9]{4,8})\b', line, re.I)
        if m_type:
            type_val = m_type.group(1).replace('—', '-').replace('–', '-').replace(' ', '').upper()
            add_candidate(type_val, "Type / Model", 80)

        # Dell Service Tag
        m_st = re.search(r'(?:Service\s*Tag|S/?T)(?:\s*\([^)]*\))?\s*[:#\s.-]*\s*([A-Za-z0-9]{7})\b', line, re.I)
        if m_st:
            add_candidate(m_st.group(1), "Dell Service Tag", 92)

        # Temple Asset Tag (TU123456 or Asset Tag: 123456)
        m_tu = re.search(r'\b(TU\d{5,7})\b', line, re.I)
        if m_tu:
            add_candidate(m_tu.group(1), "Temple Asset Tag", 90)

        m_asset = re.search(r'(?:Asset\s*Tag|Tag(?:\s*No\.?)?)(?:\s*\([^)]*\))?\s*[:#\s.-]*\s*([A-Za-z0-9]{5,10})\b', line, re.I)
        if m_asset:
            add_candidate(m_asset.group(1), "Asset Tag", 85)

        # MAC Address
        m_mac = re.search(r'(?:MAC|LAN)\s*[:#\s-]*\s*([0-9A-Fa-f]{12})\b', line, re.I)
        if m_mac:
            add_candidate(m_mac.group(1), "MAC Address", 70)

    # 2. Standalone alphanumeric tokens that look like serials (6 to 12 chars)
    for token in re.findall(r'\b[A-Za-z0-9]{2}[-\s—–]?[A-Za-z0-9]{6}\b', raw_text):
        clean_tok = re.sub(r'[^A-Za-z0-9]', '', token).upper()
        if clean_tok in {
            "SERIAL", "NUMBER", "SERVICETAG", "MODEL", "LENOVO", "TEMPLE", 
            "TEMPLEU", "UNIVERSITY", "EXPRESS", "OPTIPLEX", "THINKCENTRE",
            "PRODUCT", "SYSTEM", "DESKTOP", "LAPTOP", "COMPUTER"
        }:
            continue
        has_letter = any(c.isalpha() for c in clean_tok)
        has_digit = any(c.isdigit() for c in clean_tok)
        if has_letter and has_digit:
            add_candidate(token.replace('—', '-').replace('–', '-'), "Alphanumeric Code", 60)
            if re.search(r'\b[A-Z]{2}[-]?[1I][0O]', token.upper()):
                q_corrected = re.sub(r'([A-Z]{2}[-]?[1I])[0O]', r'\g<1>Q', token.upper()).replace('—', '-').replace('–', '-')
                add_candidate(q_corrected, "Alphanumeric (Q corrected)", 65)

    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    primary = candidates[0]["value"] if candidates else None

    return {
        "primary": primary,
        "candidates": candidates,
        "raw_text": raw_text
    }


@app.route("/ocr", methods=["POST"])
def api_ocr():
    if not BARCODE_AVAILABLE and not ZXING_AVAILABLE:
        return jsonify({
            "error": "Barcode scanning engine (zxing-cpp) is not installed on this server.",
            "success": False
        }), 500

    img_data = None

    if "file" in request.files:
        img_data = request.files["file"].read()
    elif request.is_json and "image" in request.json:
        raw_b64 = request.json["image"]
        if "," in raw_b64:
            raw_b64 = raw_b64.split(",", 1)[1]
        try:
            img_data = base64.b64decode(raw_b64)
        except Exception as e:
            return jsonify({"error": f"Invalid base64 image data: {str(e)}", "success": False}), 400
    else:
        return jsonify({
            "error": "No image provided. Please send a file upload or base64 JSON.",
            "success": False
        }), 400

    try:
        image = Image.open(io.BytesIO(img_data))
        image = ImageOps.exif_transpose(image)

        # Clamped scaling for barcode detection: clamp max dimension to 2400px
        w, h = image.size
        if w > 2400 or h > 2400:
            scale = 2200.0 / max(w, h)
            image = image.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)

        candidates = []
        seen = set()

        def register_candidate(val, label, confidence, is_verified=False):
            clean = re.sub(r'[^A-Za-z0-9]', '', str(val).split('#')[0]).upper()
            if not clean or len(clean) < 4 or clean in seen:
                return
            seen.add(clean)
            formatted = val.upper().replace('—', '-').replace('–', '-').replace(' ', '')
            if len(clean) == 8 and clean[:2].isalpha() and clean[2:].isalnum() and '-' not in formatted:
                formatted = f"{clean[:2]}-{clean[2:]}"
            candidates.append({
                "value": formatted,
                "raw": clean,
                "label": label,
                "confidence": confidence,
                "verified": is_verified
            })

        def parse_barcode_result(raw_text, b_type):
            raw_data = (raw_text or "").strip()
            if not raw_data:
                return False

            # Check if 2D barcode contains a Lenovo or Dell support URL with serial embedded
            lower_raw = raw_data.lower()
            if "lenovo.com" in lower_raw or "pcsupport" in lower_raw:
                parts = [p for p in raw_data.split('?')[0].split('#')[0].split('/') if p]
                if parts:
                    last_seg = parts[-1].strip().upper()
                    if 6 <= len(last_seg) <= 12 and re.match(r'^[A-Z0-9]+$', last_seg):
                        register_candidate(last_seg, f"Barcode Serial ({b_type})", 100)
                        return True

            clean_b = re.sub(r'[^A-Za-z0-9]', '', raw_data.split('#')[0]).upper()
            if not clean_b:
                return False

            # Lenovo 1S barcode format: 1S + 4-char machine type + 8-char serial (e.g. 1S20VFPC1QLZMC)
            if clean_b.startswith("1S") and len(clean_b) >= 14:
                serial_part = clean_b[-8:]
                type_part = clean_b[2:-8]
                register_candidate(serial_part, f"Barcode Serial ({b_type})", 100, is_verified=True)
                if len(type_part) >= 4:
                    fmt_type = f"{type_part[:4]}-{type_part[4:]}" if len(type_part) == 10 else type_part
                    mach = type_part[:4]
                    hw_info = HARDWARE_CATALOG.get(mach) or infer_hardware_from_barcode(mach)
                    lbl = f"Barcode Model ({hw_info[1]})" if hw_info else f"Barcode Model ({b_type})"
                    register_candidate(fmt_type, lbl, 85)
                return True

            # Temple Asset Tag barcode (TU12345 or 5-7 digits)
            if clean_b.startswith("TU") or (clean_b.isdigit() and len(clean_b) in (5, 6, 7)):
                register_candidate(clean_b, f"Barcode Tag ({b_type})", 100, is_verified=True)
                return True

            # Dell PPID barcode (e.g. CN-0P2419-74261-89B-1234 or CN0P2419...)
            if len(clean_b) >= 20 and clean_b[:2] in ('CN', 'KR', 'MX', 'JP', 'US', 'VN') and clean_b[2] == '0':
                dell_pn = clean_b[3:8]
                register_candidate(clean_b, f"Dell PPID ({b_type})", 92)
                hw_info = infer_hardware_from_barcode(dell_pn)
                lbl = f"Dell Model ({hw_info[1]})" if hw_info else f"Dell Part ({dell_pn})"
                register_candidate(dell_pn, lbl, 85)
                return True

            # Hardware model vs serial classification
            hw_match = infer_hardware_from_barcode(clean_b)
            is_model_code = False
            if clean_b in HARDWARE_CATALOG:
                is_model_code = True
            elif re.match(r'^[CDEGJKMQTVW1347][A-Z0-9]{1,3}[0-9]{2,3}[A-Z]$', clean_b) and len(clean_b) in (6, 7):
                is_model_code = True
            elif re.match(r'^(WD15|WD19|WD19S|WD19TB|WD19TBS|WD22TB4|D6000|TB16|DA300|DA310|HD22Q)$', clean_b):
                is_model_code = True
            elif re.match(r'^[PUESACDFW][0-9]{2}[0-9]{2}[A-Z]{0,3}$', clean_b) and len(clean_b) in (5, 6, 7):
                is_model_code = True

            if is_model_code and hw_match:
                # Registered as model barcode (confidence 88 so serials take priority as primary if present)
                register_candidate(clean_b, f"Barcode Model ({hw_match[1]})", 88)
                return True

            # General hardware serial barcode
            if len(clean_b) >= 4:
                lbl = f"Barcode Serial ({b_type})"
                if hw_match and hw_match[0] == "Printer":
                    lbl = f"Printer Serial ({b_type})"
                register_candidate(clean_b, lbl, 95)
                # If starts with 'S' prefix followed by 6-12 chars (common in HP/Dell barcodes)
                if clean_b.startswith("S") and len(clean_b) >= 7 and not clean_b[1].isdigit():
                    register_candidate(clean_b[1:], f"Barcode Serial ({b_type})", 92)
                return True

            return False

        # --- STEP 1: Multi-Pass Barcode Decoding (zxing-cpp) ---
        barcode_found = False
        if ZXING_AVAILABLE:
            # Pass A: Direct decode on original image
            try:
                z_results = zxingcpp.read_barcodes(image)
                for b in z_results:
                    if parse_barcode_result(b.text, str(b.format).split(".")[-1]):
                        barcode_found = True
            except Exception as z_err:
                logging.warning(f"[Barcode] zxing-cpp pass 1 error: {z_err}")

            # Pass B: Contrast enhanced grayscale (helps with faint or glare-affected stickers)
            if not barcode_found:
                try:
                    gray = image.convert("L")
                    enhancer = ImageEnhance.Contrast(gray)
                    boosted = enhancer.enhance(1.4)
                    z_results = zxingcpp.read_barcodes(boosted)
                    for b in z_results:
                        if parse_barcode_result(b.text, str(b.format).split(".")[-1]):
                            barcode_found = True
                except Exception as e:
                    logging.debug(f"[Barcode] zxing-cpp pass 2 error: {e}")

            # Pass C: Inverted grayscale (for dark stickers with light barcode lines)
            if not barcode_found:
                try:
                    gray = image.convert("L")
                    inv = ImageOps.invert(gray)
                    z_results = zxingcpp.read_barcodes(inv)
                    for b in z_results:
                        if parse_barcode_result(b.text, str(b.format).split(".")[-1]):
                            barcode_found = True
                except Exception as e:
                    logging.debug(f"[Barcode] zxing-cpp pass 3 error: {e}")

        # --- STEP 2: Pyzbar Fallback ---
        if not barcode_found and BARCODE_AVAILABLE:
            try:
                barcodes = decode_barcode(image)
                for b in barcodes:
                    raw_data = b.data.decode('utf-8', errors='replace').strip()
                    if parse_barcode_result(raw_data, b.type):
                        barcode_found = True
            except Exception as b_err:
                logging.warning(f"[Barcode] pyzbar decode error: {b_err}")

        # Check if any barcodes were detected
        if not candidates:
            return jsonify({
                "success": False,
                "error": "No barcode detected in the photo. Please make sure the barcode or QR code is clear, well-lit, and in focus.",
                "candidates": []
            }), 200

        # --- STEP 3: Hardware Inference from Candidates ---
        inferred_type = None
        inferred_desc = None
        for cand in candidates:
            c_val = cand.get("value") or cand.get("raw")
            c_clean = re.sub(r'[^A-Z0-9]', '', str(c_val).split('#')[0].upper())
            c_match = infer_hardware_from_barcode(c_clean)
            if c_match:
                if c_clean in HARDWARE_CATALOG:
                    inferred_type, inferred_desc = c_match
                    break
                if not inferred_type:
                    inferred_type, inferred_desc = c_match

        # --- STEP 4: Snipe-IT Ground-Truth Validation ---
        verified_primary = None
        if SNIPE_URL and SNIPE_TOKEN and candidates:
            for cand in candidates[:5]:
                snipe_match = lookup_snipe(cand["value"])
                if snipe_match and snipe_match.get("Serial Number"):
                    canonical_serial = snipe_match["Serial Number"]
                    cand["value"] = canonical_serial
                    cand["confidence"] = 100
                    cand["verified"] = True
                    cand["label"] = f"{cand['label']} (✓ Verified in Snipe-IT)"
                    verified_primary = canonical_serial
                    if snipe_match.get("Equipment Type"):
                        inferred_type = snipe_match.get("Equipment Type")
                    if snipe_match.get("Item Description"):
                        inferred_desc = snipe_match.get("Item Description")
                    logging.info(f"[Barcode] Candidate {cand['raw']} VERIFIED against Snipe-IT as: {canonical_serial}")
                    break

        candidates.sort(key=lambda x: x["confidence"], reverse=True)
        primary = verified_primary or (candidates[0]["value"] if candidates else None)

        logging.info(f"[Barcode Scan] Processed image -> Found {len(candidates)} barcode candidates. Primary: {primary}, Inferred: {inferred_type} / {inferred_desc}")

        return jsonify({
            "success": True,
            "primary": primary,
            "candidates": candidates,
            "raw_text": primary or "",
            "inferred_type": inferred_type,
            "inferred_desc": inferred_desc,
            "auto_detected": bool(inferred_type)
        })
    except Exception as e:
        logging.error(f"[Barcode] Processing error: {e}", exc_info=True)
        return jsonify({"error": f"Barcode scan failed: {str(e)}", "success": False}), 500


@app.route("/lookup", methods=["POST"])
def api_lookup():
    data = request.json or {}
    term = data.get("serial", "").strip()

    res = lookup_snipe(term)

    if res:
        res["found_in_snipe"] = True
        res["auto_detected"] = False
        return jsonify(res)

    is_likely_tag = term.upper().startswith("CPH") or (
        term.isdigit() and len(term) < 8
    )

    inferred = infer_hardware_from_barcode(term)
    if inferred:
        inferred_type, inferred_desc = inferred
        return jsonify(
            {
                "Equipment Type": inferred_type,
                "Item Description": inferred_desc,
                "Serial Number": ("" if is_likely_tag else term),
                "Temple Tag": format_temple_tag(term if is_likely_tag else ""),
                "found_in_snipe": False,
                "auto_detected": True,
            }
        )

    return jsonify(
        {
            "Equipment Type": "Computer",
            "Item Description": "",
            "Serial Number": ("" if is_likely_tag else term),
            "Temple Tag": format_temple_tag(term if is_likely_tag else ""),
            "found_in_snipe": False,
            "auto_detected": False,
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
                    all_rows = reader[1:]
                    all_rows.reverse()
                    return jsonify({"items": all_rows})
    except:
        pass
    return jsonify({"items": []})


@app.route("/finalize", methods=["POST"])
def api_finalize():
    with CSV_LOCK:
        if not os.path.exists(CURRENT_CSV):
            return jsonify({"error": "No data to finalize"}), 400

        today_str = datetime.now().strftime("%Y%m%d")
        base_name = f"{today_str}-cph-crc"
        filename = f"{base_name}.xlsx"
        dest_path = os.path.join(COMPLETED_FOLDER, filename)

        counter = 1
        while os.path.exists(dest_path):
            filename = f"{base_name}-{counter}.xlsx"
            dest_path = os.path.join(COMPLETED_FOLDER, filename)
            counter += 1

        try:
            if os.path.exists(TEMPLATE_FILE):
                wb = load_workbook(TEMPLATE_FILE)
                ws = wb.active
            else:
                logging.warning(
                    f"Template {TEMPLATE_FILE} not found. Creating a blank one."
                )
                wb = Workbook()
                ws = wb.active
                ws.append(CSV_HEADERS)

            with open(CURRENT_CSV, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                next(reader, None)

                for row in reader:
                    # Sanitize cells to prevent CSV/Excel formula injection
                    sanitized_row = []
                    for cell in row:
                        cell_str = str(cell)
                        if cell_str and cell_str[0] in ("=", "+", "-", "@"):
                            sanitized_row.append(f"'{cell_str}")
                        else:
                            sanitized_row.append(cell_str)
                    ws.append(sanitized_row)

            wb.save(dest_path)
            os.remove(CURRENT_CSV)

            return jsonify({"ok": True, "filename": filename})

        except Exception as e:
            logging.error(f"Finalize Error: {e}")
            return jsonify({"error": str(e)}), 500


@app.route("/reset_batch", methods=["POST"])
def api_reset_batch():
    """Wipes the current CSV."""
    with CSV_LOCK:
        with open(CURRENT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)

    return jsonify({"ok": True})


@app.route("/completed_files", methods=["GET"])
def api_completed_files():
    files = []
    if os.path.exists(COMPLETED_FOLDER):
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


@app.route("/delete_row", methods=["POST"])
def api_delete_row():
    data = request.json or {}
    serial_to_delete = data.get("serial", "").strip().lower()

    with CSV_LOCK:
        if not os.path.exists(CURRENT_CSV):
            return jsonify({"error": "No active batch"}), 400

        try:
            with open(CURRENT_CSV, "r", encoding="utf-8") as f:
                rows = list(csv.reader(f))

            if not rows:
                return jsonify({"ok": True})

            header = rows[0]
            new_rows = [header] + [
                r
                for r in rows[1:]
                if len(r) > 2 and r[2].strip().lower() != serial_to_delete
            ]

            with open(CURRENT_CSV, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerows(new_rows)

            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


@app.route("/edit_row", methods=["POST"])
def api_edit_row():
    data = request.json or {}
    old_serial = data.get("old_serial", "").strip().lower()
    new_data = data.get("new_data", {})

    with CSV_LOCK:
        try:
            with open(CURRENT_CSV, "r", encoding="utf-8") as f:
                rows = list(csv.reader(f))

            if not rows:
                return jsonify({"error": "No active batch"}), 400

            header = rows[0]
            new_rows = [header]

            for r in rows[1:]:
                if len(r) > 2 and r[2].strip().lower() == old_serial:
                    new_rows.append(
                        [
                            new_data.get("Equipment Type", r[0]),
                            new_data.get("Item Description", r[1]),
                            new_data.get("Serial Number", r[2]),
                            new_data.get("Temple Tag", len(r) > 3 and r[3] or "N/A"),
                        ]
                    )
                else:
                    new_rows.append(r)

            with open(CURRENT_CSV, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerows(new_rows)

            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    ensure_csv()
    ssl_ctx = (
        ("cert.pem", "key.pem")
        if os.path.exists("cert.pem") and os.path.exists("key.pem")
        else None
    )
    flask_debug = os.getenv("FLASK_DEBUG", "false").lower() in ("true", "1", "yes")
    app.run(host="0.0.0.0", port=5000, debug=flask_debug, ssl_context=ssl_ctx)
