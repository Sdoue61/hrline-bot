from flask import Flask, request
import requests
import os
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build

# -------------------------------
# Google Sheets setup (FAQ)
# -------------------------------

KEY_PATH = "/etc/secrets/google-creds.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

creds = service_account.Credentials.from_service_account_file(KEY_PATH, scopes=SCOPES)
sheets = build("sheets", "v4", credentials=creds)

SHEET_ID = "12TV6k9J7Icm_2P3IKFPEMJxMVpYhvE1IDgXexwt4jfY"
RANGE = "FAQ!A:E"

def load_faq():
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=RANGE
    ).execute()

    rows = result.get("values", [])
    faq = []

    for row in rows[1:]:
        if len(row) >= 5:
            faq.append({
                "key": row[0].lower().strip(),
                "en_q": row[1].lower().strip(),
                "en_a": row[2].strip(),
                "jp_q": row[3].strip(),
                "jp_a": row[4].strip()
            })
    return faq

FAQ = load_faq()

# -------------------------------
# Helper functions (FAQ)
# -------------------------------

def detect_language(text):
    return "jp" if re.search("[ぁ-んァ-ン一-龯]", text) else "en"

def find_faq(text):
    t = text.lower()
    lang = detect_language(text)

    for item in FAQ:
        # allow multiple keywords in key column
        keys = [k.strip() for k in item["key"].split(",")]

        for k in keys:
            if k and k in t:
                return item[f"{lang}_a"]

        # fallback to full question match
        if item[f"{lang}_q"] and item[f"{lang}_q"] in t:
            return item[f"{lang}_a"]

    return None

# -------------------------------
# Quitting date flow (MVP state)
# -------------------------------

# In-memory state: { user_id: {"step": "...", "staff_id": "..."} }
# NOTE: resets on deploy/restart. Good enough for MVP.
USER_STATE = {}

def is_quit_trigger(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ["quit", "resign", "退職", "辞める", "退会", "やめる"]

def is_valid_iso_date(s: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", (s or "").strip()))

def call_apps_script_quitting(line_user_id: str, staff_id: str, quitting_date: str) -> dict:
    url = os.environ.get("APPS_SCRIPT_URL")
    api_key = os.environ.get("APPS_SCRIPT_API_KEY")

    if not url or not api_key:
        print("Missing APPS_SCRIPT_URL or APPS_SCRIPT_API_KEY")
        return {"ok": False, "error": "MISSING_ENV"}

    payload = {
        "action": "createQuittingRequest",
        "lineUserId": line_user_id,
        "staffId": staff_id,
        "quittingDate": quitting_date,
        "reason": "",
        "comment": "",
    }

    try:
        r = requests.post(
            url,
            headers={"Content-Type": "application/json", "X-API-KEY": api_key},
            json=payload,
            timeout=10,
        )
        try:
            return r.json()
        except Exception:
            return {
                "ok": False,
                "error": "NON_JSON",
                "status": r.status_code,
                "text": r.text[:200],
            }
    except Exception as e:
        return {"ok": False, "error": "REQUEST_FAILED", "detail": str(e)}

# -------------------------------
# LINE setup
# -------------------------------

app = Flask(__name__)
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_TOKEN")

def reply(text, token):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "replyToken": token,
        "messages": [{"type": "text", "text": text}]
    }
    requests.post(url, headers=headers, json=data, timeout=10)

# -------------------------------
# Webhook
# -------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}

    for event in data.get("events", []):
        if event.get("type") != "message":
            continue
        if event.get("message", {}).get("type") != "text":
            continue

        source_type = event.get("source", {}).get("type")
        user_id = event.get("source", {}).get("userId")  # needed for state + Apps Script
        user_text = (event.get("message", {}).get("text") or "").strip()
        reply_token = event.get("replyToken")

        if not reply_token or not user_text:
            continue

        # Group protection: only respond if starts with !hr
        if source_type != "user":
            if not user_text.lower().startswith("!hr"):
                continue
            user_text = user_text[3:].strip()  # remove "!hr"

        # --- Quitting flow start ---
        if user_id and is_quit_trigger(user_text):
            USER_STATE[user_id] = {"step": "WAIT_STAFFID"}
            reply(
                "退職日申請を開始します。\n社員番号（例：2338）を入力してください。\n\n"
                "Starting quitting date request.\nPlease enter your Staff ID (e.g., 2338).",
                reply_token
            )
            continue

        # --- Quitting flow continue ---
        if user_id and user_id in USER_STATE:
            st = USER_STATE[user_id]

            if st.get("step") == "WAIT_STAFFID":
                staff_id = user_text

                # Basic StaffID format check (adjust if needed)
                if not re.match(r"^\d{3,6}$", staff_id):
                    reply(
                        "社員番号の形式が正しくありません。例：2338\n\n"
                        "Staff ID format looks wrong. Example: 2338.",
                        reply_token
                    )
                    continue

                st["staff_id"] = staff_id
                st["step"] = "WAIT_DATE"
                reply(
                    "退職希望日（最後の勤務日）を入力してください。\n形式：YYYY-MM-DD\n例：2026-03-31\n\n"
                    "Please enter quitting date (last working day).\nFormat: YYYY-MM-DD\nExample: 2026-03-31",
                    reply_token
                )
                continue

            if st.get("step") == "WAIT_DATE":
                quitting_date = user_text

                if not is_valid_iso_date(quitting_date):
                    reply(
                        "日付の形式が正しくありません。例：2026-03-31\n\n"
                        "Invalid date format. Example: 2026-03-31",
                        reply_token
                    )
                    continue

                result = call_apps_script_quitting(
                    line_user_id=user_id,
                    staff_id=st.get("staff_id", ""),
                    quitting_date=quitting_date
                )
                print("Apps Script result:", result)

                # Clear state regardless (avoid trapping user)
                USER_STATE.pop(user_id, None)

                if result.get("ok"):
                    reply(
                        "申請を受け付けました。内容を確認のうえ、HRよりご連絡します。\n\n"
                        "Your request has been received. HR will review and contact you.",
                        reply_token
                    )
                else:
                    reply(
                        "申請は受け付けましたが、システム登録でエラーが発生しました。HRへご連絡ください。\n\n"
                        "Your request was received, but there was a system error saving it. Please contact HR.",
                        reply_token
                    )
                continue

        # --- FAQ flow (default) ---
        answer = find_faq(user_text)

        if answer:
            reply(answer, reply_token)
        else:
            if detect_language(user_text) == "jp":
                reply("申し訳ありません。その質問は人事に転送されました。", reply_token)
            else:
                reply("Sorry, HR will follow up on this.", reply_token)

    return "OK"

# -------------------------------
# Run server (local dev only)
# -------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
