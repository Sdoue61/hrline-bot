from flask import Flask, request
import requests
import os
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build

# =========================================================
# Google Sheets setup (FAQ)
# =========================================================

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
    print("FAQ load failed:", e, flush=True)
    FAQ = []

def detect_language(text: str) -> str:
    return "jp" if re.search(r"[ぁ-んァ-ン一-龯]", text or "") else "en"

def find_faq(text: str):
    t = (text or "").lower()
    lang = detect_language(text)

    for item in FAQ:
        keys = [k.strip() for k in item.get("key", "").split(",")]
        for k in keys:
            if k and k in t:
                return item.get(f"{lang}_a")

        q = (item.get(f"{lang}_q") or "").lower()
        if q and q in t:
            return item.get(f"{lang}_a")

    return None

# =========================================================
# Quitting flow state (in-memory MVP)
# =========================================================

USER_STATE = {}

def is_quit_trigger(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ["quit", "resign", "退職", "辞める", "辞めたい", "やめる", "退会"]

def is_cancel_trigger(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ["cancel", "cancel quit", "キャンセル", "取消", "取り消し", "中止"]

def is_valid_iso_date(s: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", (s or "").strip()))

REASONS = [
    ("家庭の事情", "Family reasons"),
    ("健康上の理由", "Health reasons"),
    ("引っ越し", "Moving"),
    ("学業・進学", "School / Study"),
    ("転職", "New job"),
    ("その他", "Other"),
]
VALID_REASON_JP = {jp for jp, _ in REASONS}
REASON_NUM_MAP = {str(i+1): REASONS[i][0] for i in range(len(REASONS))}

def call_apps_script_quitting(line_user_id: str, staff_id: str, quitting_date: str, reason: str, comment: str) -> dict:
    url = os.environ.get("APPS_SCRIPT_URL")
    api_key = os.environ.get("APPS_SCRIPT_API_KEY")

    if not url or not api_key:
        print("Missing APPS_SCRIPT_URL or APPS_SCRIPT_API_KEY", flush=True)
        return {"ok": False, "error": "MISSING_ENV"}

    payload = {
        "action": "createQuittingRequest",
        "lineUserId": line_user_id,
        "staffId": staff_id,
        "quittingDate": quitting_date,
        "reason": reason or "",
        "comment": comment or "",
    }

    try:
        r = requests.post(
            url,
            headers={"Content-Type": "application/json", "X-API-KEY": api_key},
            json=payload,
            timeout=15,
        )
        print("AppsScript status:", r.status_code, flush=True)
        print("AppsScript raw:", (r.text or "")[:500], flush=True)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "error": "NON_JSON", "status": r.status_code, "text": (r.text or "")[:200]}
    except Exception as e:
        print("AppsScript request failed:", str(e), flush=True)
        return {"ok": False, "error": "REQUEST_FAILED", "detail": str(e)}

# =========================================================
# LINE Messaging helpers
# =========================================================

app = Flask(__name__)
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_TOKEN") or os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

def reply_messages(reply_token: str, messages: list):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {"replyToken": reply_token, "messages": messages}
    resp = requests.post(url, headers=headers, json=data, timeout=15)

    # IMPORTANT: show LINE errors in Render logs
    if resp.status_code != 200:
        print("LINE reply FAILED:", resp.status_code, resp.text[:500], flush=True)

def reply_text(reply_token: str, text: str):
    reply_messages(reply_token, [{"type": "text", "text": text}])

def reason_menu_text() -> str:
    # fallback menu (always works)
    lines = [
        "退職理由を選択してください（番号でもOK）:",
        "1. 家庭の事情",
        "2. 健康上の理由",
        "3. 引っ越し",
        "4. 学業・進学",
        "5. 転職",
        "6. その他",
        "",
        "Please choose the reason (you can type 1–6)."
    ]
    return "\n".join(lines)

def reply_reason_quick(reply_token: str):
    # Real quickReply buttons
    items = []
    for i, (jp, _) in enumerate(REASONS, start=1):
        label = f"{i}.{jp}"
        items.append({
            "type": "action",
            "action": {
                "type": "message",
                "label": label[:20],
                "text": jp  # what gets sent back when tapped
            }
        })

    msg = {
        "type": "text",
        "text": "退職理由を選択してください（ボタン or 番号1〜6）。\nPlease choose using buttons or type 1–6.",
        "quickReply": {"items": items}
    }

    reply_messages(reply_token, [msg])

    # If LINE rejected the quickReply payload, user still needs guidance:
    # Send fallback menu (always works)
    reply_text(reply_token, reason_menu_text())

# =========================================================
# Webhook
# =========================================================

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

        print("IN:", {"source": source_type, "user_id": user_id, "text": user_text}, flush=True)
        print("STATE:", USER_STATE.get(user_id), flush=True)

        if not reply_token or not user_text:
            continue

        # Group protection
        if source_type != "user":
            if not user_text.lower().startswith("!hr"):
                continue
            user_text = user_text[3:].strip()

        # Cancel
        if user_id and is_cancel_trigger(user_text):
            USER_STATE.pop(user_id, None)
            reply_text(reply_token, "申請をキャンセルしました。\n\nRequest cancelled.")
            continue

        # Start
        if user_id and is_quit_trigger(user_text):
            USER_STATE[user_id] = {"step": "WAIT_STAFFID"}
            reply_text(
                reply_token,
                "退職日申請を開始します。\n社員番号（例：2338）を入力してください。\n\n"
                "Starting quitting date request.\nPlease enter your Staff ID (e.g., 2338)."
            )
            continue

        # Flow
        if user_id and user_id in USER_STATE:
            st = USER_STATE[user_id]
            step = st.get("step")

            if step == "WAIT_STAFFID":
                staff_id = user_text
                if not re.match(r"^\d{3,6}$", staff_id):
                    reply_text(reply_token, "社員番号の形式が正しくありません。例：2338\n\nStaff ID example: 2338")
                    continue

                st["staff_id"] = staff_id
                st["step"] = "WAIT_DATE"
                reply_text(reply_token,
                    "退職希望日（最後の勤務日）を入力してください。\n形式：YYYY-MM-DD\n例：2026-03-31\n\n"
                    "Enter quitting date.\nFormat: YYYY-MM-DD (e.g., 2026-03-31)"
                )
                continue

            if step == "WAIT_DATE":
                quitting_date = user_text
                if not is_valid_iso_date(quitting_date):
                    reply_text(reply_token, "日付の形式が正しくありません。例：2026-03-31\n\nInvalid date example: 2026-03-31")
                    continue

                st["quitting_date"] = quitting_date
                st["step"] = "WAIT_REASON"

                # THIS is where you previously had “nothing happened”
                reply_reason_quick(reply_token)
                continue

            if step == "WAIT_REASON":
                # Accept number OR JP reason text
                reason = REASON_NUM_MAP.get(user_text) or user_text

                if reason not in VALID_REASON_JP:
                    # Re-show menu
                    reply_reason_quick(reply_token)
                    continue

                st["reason"] = reason

                if reason == "その他":
                    st["step"] = "WAIT_COMMENT"
                    reply_text(reply_token,
                        "『その他』を選択しました。簡単に理由を入力してください。\n\n"
                        "You chose 'Other'. Please type a short reason."
                    )
                    continue

                # Submit now
                result = call_apps_script_quitting(
                    line_user_id=user_id,
                    staff_id=st.get("staff_id", ""),
                    quitting_date=st.get("quitting_date", ""),
                    reason=st.get("reason", ""),
                    comment=""
                )
                USER_STATE.pop(user_id, None)

                if result.get("ok"):
                    reply_text(reply_token, "申請を受け付けました。HRよりご連絡します。\n\nRequest received. HR will contact you.")
                else:
                    reply_text(reply_token, "申請は受け付けましたが、登録でエラー。HRへご連絡ください。\n\nSystem error. Please contact HR.")
                continue

            if step == "WAIT_COMMENT":
                comment = user_text[:300]
                result = call_apps_script_quitting(
                    line_user_id=user_id,
                    staff_id=st.get("staff_id", ""),
                    quitting_date=st.get("quitting_date", ""),
                    reason=st.get("reason", ""),
                    comment=comment
                )
                USER_STATE.pop(user_id, None)

                if result.get("ok"):
                    reply_text(reply_token, "申請を受け付けました。HRよりご連絡します。\n\nRequest received. HR will contact you.")
                else:
                    reply_text(reply_token, "申請は受け付けましたが、登録でエラー。HRへご連絡ください。\n\nSystem error. Please contact HR.")
                continue

        # Default FAQ
        answer = find_faq(user_text)
        if answer:
            reply_text(reply_token, answer)
        else:
            reply_text(reply_token, "申し訳ありません。その質問は人事に転送されました。" if detect_language(user_text) == "jp"
                       else "Sorry, HR will follow up on this.")

    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
