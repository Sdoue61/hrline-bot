from flask import Flask, request
import requests
import os
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build

# -------------------------------
# Google Sheets setup
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
# Helper functions
# -------------------------------

def detect_language(text):
    return "jp" if re.search("[ぁ-んァ-ン一-龯]", text) else "en"

def find_faq(text):
    t = text.lower()
    lang = detect_language(text)

    for item in FAQ:
        if item["key"] in t or item[f"{lang}_q"] in t:
            return item[f"{lang}_a"]
    return None

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
        "messages": [
            {"type": "text", "text": text}
        ]
    }
    requests.post(url, headers=headers, json=data)

# -------------------------------
# Webhook
# -------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    for event in data.get("events", []):
        if event["type"] != "message":
            continue

        source = event["source"]["type"]
        user_text = event["message"]["text"]
        reply_token = event["replyToken"]

        # Group protection
        if source != "user":
            if not user_text.lower().startswith("!hr"):
                return "OK"
            user_text = user_text[3:].strip()

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
# Run server
# -------------------------------

app.run(host="0.0.0.0", port=10000)
