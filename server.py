from flask import Flask, request
import requests
import os

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_TOKEN")

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if "events" in data:
        for event in data["events"]:
            if event["type"] == "message":
                reply_token = event["replyToken"]
                user_text = event["message"]["text"]

                reply(user_text, reply_token)

    return "OK"

def reply(text, token):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "replyToken": token,
        "messages": [
            {"type": "text", "text": f"You said: {text}"}
        ]
    }
    requests.post(url, headers=headers, json=data)

app.run(host="0.0.0.0", port=10000)
