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
RANGE = "'FAQ'!A:E"

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
                "key": (row[0] or "").lower().strip(),
                "en_q": (row[1] or "").lower().strip(),
                "en_a": (row[2] or "").strip(),
                "jp_q": (row[3] or "").strip(),
                "jp_a": (row[4] or "").strip()
            })
    return faq

try:
    FAQ = load_faq()
except Exception as e:
    print("FAQ load failed:", str(e), flush=True)
    FAQ = []

# -------------------------------
# Helper functions (FAQ)
# -------------------------------

def detect_language(text):
    return "jp" if re.search("[ぁ-んァ-ン一-龯]", text or "") else "en"

def find_faq(text):
    t = (text or "").lower()
    lang = detect_language(text)

    for item in FAQ:
        keys = [k.strip() for k in (item.get("key") or "").split(",")]

        for k in keys:
            if k and k in t:
                return item.get(f"{lang}_a")

        q = (item.get(f"{lang}_q") or "").lower().strip()
        if q and q in t:
            return item.get(f"{lang}_a")

    return None

# -------------------------------
# Quitting date flow (state)
# -------------------------------

USER_STATE = {}

def is_quit_trigger(text):
    return (text or "").strip().lower() in [
        "quit", "resign", "退職", "辞める", "退会", "やめる"
    ]

def is_valid_iso_date(s):
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", s or ""))

# -------------------------------
# Call Apps Script (URL-only auth)
# -------------------------------

def call_apps_script_quitting(line_user_id, staff_id, quitting_date):
    url = os.environ.get("APPS_SCRIPT_URL", "").strip()

    if not url:
        print("Missing APPS_SCRIPT_URL", flush=True)
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
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )

        print("AppsScript status:", r.status_code, flush=True)
        print("AppsScript raw:", (r.text or "")[:500], flush=True)

        try:
            return r.json()
        except Exception:
            return {
                "ok": False,
                "error": "NON_JSON",
                "status": r.status_code,
                "text": (r.text or "")[:200],
            }

    except Exception as e:
        print("AppsScript request failed:", str(e), flush=True)
        return {"ok": False, "error": "REQUEST_FAILED", "detail": str(e)}

# -------------------------------
# LINE setup
# -------------------------------

app = Flask(__name__)
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_TOKEN", "").strip()

def reply(text, token):
    if not CHANNEL_ACCESS_TOKEN:
        print("Missing LINE_TOKEN", flush=True)
        return

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
        user_id = event.get("source", {}).get("userId")
        user_text = (event.get("message", {}).get("text") or "").strip()
        reply_token = event.get("replyToken")

        if not reply_token or not user_text:
            continue

        # Group protection
        if source_type != "user":
            if not user_text.lower().startswith("!hr"):
                continue
            user_text = user_text[3:].strip()

        # ---- Quit flow start ----
        if user_id and is_quit_trigger(user_text):
            USER_STATE[user_id] = {"step": "WAIT_STAFFID"}
            reply(
                "退職日申請を開始します。\n社員番号（例：2338）を入力してください。\n\n"
                "Starting quitting date request.\nPlease enter your Staff ID (e.g., 2338).",
                reply_token
            )
            continue

        # ---- Quit flow continue ----
        if user_id and user_id in USER_STATE:
            st = USER_STATE[user_id]

            if st["step"] == "WAIT_STAFFID":
                if not re.match(r"^\d{3,6}$", user_text):
                    reply("社員番号の形式が正しくありません。例：2338", reply_token)
                    continue

                st["staff_id"] = user_text
                st["step"] = "WAIT_DATE"
                reply(
                    "退職希望日を入力してください。\n形式：YYYY-MM-DD\n例：2026-03-31",
                    reply_token
                )
                continue

            if st["step"] == "WAIT_DATE":
                if not is_valid_iso_date(user_text):
                    reply("日付形式が正しくありません。例：2026-03-31", reply_token)
                    continue

                result = call_apps_script_quitting(
                    user_id,
                    st["staff_id"],
                    user_text
                )

                USER_STATE.pop(user_id, None)

                if result.get("ok"):
                    reply(
                        "申請を受け付けました。HRよりご連絡します。",
                        reply_token
                    )
                else:
                    print("Quitting error:", result, flush=True)
                    reply(
                        "システムエラーが発生しました。HRへご連絡ください。",
                        reply_token
                    )
                continue

        # ---- FAQ ----
        answer = find_faq(user_text)
        if answer:
            reply(answer, reply_token)
        else:
            reply(
                "申し訳ありません。その質問は人事に転送されました。"
                if detect_language(user_text) == "jp"
                else "Sorry, HR will follow up on this.",
                reply_token
            )

    return "OK"

# -------------------------------
# Local dev only
# -------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
