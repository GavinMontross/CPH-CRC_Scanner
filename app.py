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

load_dotenv()

# ----- Logging & App Setup -----
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = Flask(__name__, static_folder="static", template_folder="templates")

# Flask Session Security
app.secret_key = os.getenv(
    "FLASK_SECRET_KEY", "cph-crc-scanner-session-secret-9b2f8a1c4e7d0"
)
app.config["SESSION_COOKIE_NAME"] = "cph_crc_session"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
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
# Derived from Paley Conference Dashboard configuration:
# - afd02e22-938a-49ae-b86b-660f8bb70f5b: CPH-PaleyConferenceDashboard - AD
# - 9d5bcc98-312b-4d60-97bb-be9b9c2b0d34: CPH Deans Office - Entra
# - 54956bf4-79e4-434d-8bdd-3deda3cb9131: CPH-PaleyConference-RoomPermissions - Entra
DEFAULT_ALLOWED_GROUPS = [
    "afd02e22-938a-49ae-b86b-660f8bb70f5b",
    "9d5bcc98-312b-4d60-97bb-be9b9c2b0d34",
    "54956bf4-79e4-434d-8bdd-3deda3cb9131",
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


def check_user_group_membership(access_token, allowed_group_ids):
    """
    Checks if the authenticated user is a member of any of the allowed AD/Entra groups
    via Microsoft Graph checkMemberGroups API.
    """
    if not allowed_group_ids:
        return True  # Open to all if no groups specified

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
            return len(matching_groups) > 0
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


def format_temple_tag(tag):
    """Removes 'CPH' or cuts the string off at 5 characters."""
    if not tag:
        return ""

    tag_str = str(tag).strip()

    if tag_str.upper().startswith("CPH"):
        return tag_str[3:]
    else:
        return tag_str[:5]


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

    clean_term = term.strip()
    terms_to_try = [clean_term]

    if clean_term.upper().startswith("1S") and len(clean_term) >= 14:
        terms_to_try.append(clean_term[-8:])
        terms_to_try.append(clean_term[2:])

    if "-" in clean_term:
        terms_to_try.append(clean_term.replace("-", ""))
    elif len(clean_term) == 8 and clean_term[2] != "-":
        terms_to_try.append(f"{clean_term[:2]}-{clean_term[2:]}")

    rows = []
    for t in terms_to_try:
        rows = get_data(f"{base_api}/hardware/bytag/{t}")
        if not rows:
            rows = get_data(f"{base_api}/hardware/byserial/{t}")
        if not rows:
            rows = get_data(f"{base_api}/hardware", params={"search": t, "limit": 1})
        if rows:
            break

    if rows:
        hw = rows[0]
        manuf = hw.get("manufacturer", {}).get("name", "")
        model = hw.get("model", {}).get("name", "")
        full_desc = f"{manuf} {model}".strip()

        return {
            "Equipment Type": hw.get("category", {}).get("name", "Computer"),
            "Item Description": full_desc,
            "Serial Number": hw.get("serial", ""),
            "Temple Tag": format_temple_tag(hw.get("asset_tag", "")),
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
    if request.endpoint in {"login", "auth_callback"}:
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

    # Save target URL to return user after authentication
    session["next_url"] = request.url
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
    is_authorized = check_user_group_membership(access_token, ALLOWED_GROUPS)
    if not is_authorized:
        logging.warning(
            f"Access Denied: User {user_email} is not in allowed groups ({ALLOWED_GROUPS})."
        )
        return render_template("access_denied.html", user_email=user_email), 403

    user_name = user_data.get("name") or user_email.split("@")[0]
    user_photo = fetch_user_photo(access_token)

    session["user_logged_in"] = True
    session["user"] = {
        "name": user_name,
        "email": user_email,
        "photo": user_photo,
    }
    session["access_token"] = access_token
    session.permanent = True

    next_url = session.pop("next_url", None)
    return redirect(next_url or url_for("index"))


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
    return render_template("index.html", user=session.get("user"))


@app.route("/lookup", methods=["POST"])
def api_lookup():
    data = request.json or {}
    term = data.get("serial", "").strip()

    res = lookup_snipe(term)

    if res:
        res["found_in_snipe"] = True
        return jsonify(res)

    is_likely_tag = term.upper().startswith("CPH") or (
        term.isdigit() and len(term) < 8
    )

    return jsonify(
        {
            "Equipment Type": "Computer",
            "Item Description": "",
            "Serial Number": ("" if is_likely_tag else term),
            "Temple Tag": format_temple_tag(term if is_likely_tag else ""),
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
                    ws.append(row)

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
    app.run(host="0.0.0.0", port=5000, debug=True, ssl_context=ssl_ctx)
