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
    result = sheets.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=RANGE).execute()
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
# FAQ helpers
# -------------------------------

def detect_language(text: str) -> str:
    return "jp" if re.search("[ぁ-んァ-ン一-龯]", text or "") else "en"

def find_faq(text: str):
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
# LINE setup
# -------------------------------

app = Flask(__name__)
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_TOKEN", "").strip()

def line_reply(reply_token: str, messages: list):
    if not CHANNEL_ACCESS_TOKEN:
        print("Missing LINE_TOKEN", flush=True)
        return
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {"replyToken": reply_token, "messages": messages}
    try:
        requests.post(url, headers=headers, json=data, timeout=10)
    except Exception as e:
        print("LINE reply failed:", str(e), flush=True)

def reply_text(reply_token: str, text: str):
    line_reply(reply_token, [{"type": "text", "text": text}])

def reply_text_quick(reply_token: str, text: str, options: list):
    """
    options: list of button labels (same label is sent back as text)
    """
    items = [{"type": "action", "action": {"type": "message", "label": opt, "text": opt}} for opt in options]
    line_reply(reply_token, [{
        "type": "text",
        "text": text,
        "quickReply": {"items": items}
    }])

# -------------------------------
# Apps Script call (URL auth)
# -------------------------------

def call_apps_script(payload: dict) -> dict:
    url = os.environ.get("APPS_SCRIPT_URL", "").strip()
    if not url:
        print("Missing APPS_SCRIPT_URL", flush=True)
        return {"ok": False, "error": "MISSING_ENV"}

    try:
        r = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=20)
        print("AppsScript status:", r.status_code, flush=True)
        print("AppsScript raw:", (r.text or "")[:500], flush=True)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "error": "NON_JSON", "text": (r.text or "")[:200]}
    except Exception as e:
        print("AppsScript request failed:", str(e), flush=True)
        return {"ok": False, "error": "REQUEST_FAILED", "detail": str(e)}

# -------------------------------
# Quitting flow state
# -------------------------------

USER_STATE = {}

def is_quit_trigger(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ["quit", "resign", "退職", "辞める", "やめる"]

def is_cancel_flow_trigger(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ["退職申請キャンセル", "退職キャンセル", "cancel quit", "cancel quit request"]

def is_cancel_word(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ["cancel", "キャンセル", "中止", "やめる"]

def is_valid_staff_id(text: str) -> bool:
    return bool(re.match(r"^\d{3,6}$", (text or "").strip()))

def is_valid_iso_date(text: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", (text or "").strip()))

REASON_JP = [
    "転職（Job change）",
    "家庭都合（Family）",
    "学業（Study）",
    "健康（Health）",
    "契約満了（End of contract）",
    "その他（Other）",
]
REASON_MAP = {
    "転職（Job change）": "Job change",
    "家庭都合（Family）": "Family reasons",
    "学業（Study）": "Study",
    "健康（Health）": "Health",
    "契約満了（End of contract）": "End of contract",
    "その他（Other）": "Other",
}

# -------------------------------
# HR command security
# -------------------------------

def is_hr_user(line_user_id: str) -> bool:
    allow = os.getenv("HR_LINE_USER_IDS", "").strip()
    if not allow:
        # If you don't set HR_LINE_USER_IDS, HR commands are disabled for safety.
        return False
    allowed_ids = [x.strip() for x in allow.split(",") if x.strip()]
    return line_user_id in allowed_ids

def parse_hr_command(text: str):
    # Expected: approve <requestId> [comment...]
    #           reject <requestId> [comment...]
    #           cancel <requestId> [comment...]
    t = (text or "").strip()
    parts = t.split()
    if len(parts) < 2:
        return None
    cmd = parts[0].lower()
    req_id = parts[1].strip()
    comment = " ".join(parts[2:]).strip()
    if cmd not in ["approve", "reject", "cancel"]:
        return None
    if not req_id:
        return None
    return cmd, req_id, comment

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
        print("LINE userId =", user_id, flush=True)
        user_text_raw = (event.get("message", {}).get("text") or "")
        user_text = user_text_raw.strip()
        reply_token = event.get("replyToken")

        if not reply_token or not user_text:
            continue

        # Group protection: only respond if starts with !hr
        if source_type != "user":
            if not user_text.lower().startswith("!hr"):
                continue
            user_text = user_text[3:].strip()

        # ---- Global cancel word during any active state ----
        if user_id and user_id in USER_STATE and is_cancel_word(user_text):
            USER_STATE.pop(user_id, None)
            reply_text(reply_token, "キャンセルしました。\nCancelled.")
            continue

        # ---- HR commands (only for allowed HR users) ----
        if user_text.lower().startswith(("approve ", "reject ", "cancel ")):
            if not user_id or not is_hr_user(user_id):
                reply_text(reply_token, "権限がありません（HRのみ）。\nNot authorized (HR only).")
                continue

            parsed = parse_hr_command(user_text)
            if not parsed:
                reply_text(reply_token,
                           "コマンド形式：\napprove <RequestID>\nreject <RequestID> <comment>\ncancel <RequestID> <comment>")
                continue

            cmd, req_id, comment = parsed
            action_map = {
                "approve": "approveQuittingRequest",
                "reject": "rejectQuittingRequest",
                "cancel": "cancelRequestById",
            }
            payload = {
                "action": action_map[cmd],
                "requestId": req_id,
                "hrLineUserId": user_id,
                "hrComment": comment,
            }
            result = call_apps_script(payload)
            if result.get("ok"):
                reply_text(reply_token, f"OK: {cmd} {req_id}")
            else:
                reply_text(reply_token, f"NG: {result.get('error')}\n{result.get('detail','')}")
            continue

        # ---- Cancel latest quitting request command (staff) ----
        if user_id and is_cancel_flow_trigger(user_text):
            USER_STATE[user_id] = {"step": "CANCEL_WAIT_STAFFID"}
            reply_text(reply_token, "退職申請のキャンセルをします。\n社員番号（例：2338）を入力してください。")
            continue

        if user_id and user_id in USER_STATE and USER_STATE[user_id].get("step") == "CANCEL_WAIT_STAFFID":
            if not is_valid_staff_id(user_text):
                reply_text(reply_token, "社員番号の形式が正しくありません。例：2338")
                continue
            USER_STATE[user_id]["staff_id"] = user_text
            USER_STATE[user_id]["step"] = "CANCEL_CONFIRM"
            reply_text_quick(reply_token,
                             "最新の退職申請（New/InReview）をキャンセルしますか？",
                             ["はい（Yes）", "いいえ（No）"])
            continue

        if user_id and user_id in USER_STATE and USER_STATE[user_id].get("step") == "CANCEL_CONFIRM":
            if user_text not in ["はい（Yes）", "いいえ（No）"]:
                reply_text(reply_token, "ボタンから選択してください。")
                continue
            if user_text == "いいえ（No）":
                USER_STATE.pop(user_id, None)
                reply_text(reply_token, "キャンセルしませんでした。\nNo changes made.")
                continue

            staff_id = USER_STATE[user_id].get("staff_id", "")
            USER_STATE.pop(user_id, None)

            payload = {
                "action": "cancelLatestQuittingRequest",
                "lineUserId": user_id,
                "staffId": staff_id,
                "hrComment": "Cancelled by staff via LINE",
            }
            result = call_apps_script(payload)
            if result.get("ok"):
                reply_text(reply_token, "最新の退職申請をキャンセルしました。\nCancelled your latest quitting request.")
            else:
                reply_text(reply_token, "キャンセルに失敗しました。HRへご連絡ください。\nFailed. Please contact HR.")
            continue

        # ---- Start quitting flow ----
        if user_id and is_quit_trigger(user_text):
            USER_STATE[user_id] = {"step": "Q_WAIT_STAFFID"}
            reply_text(reply_token,
                       "退職日申請を開始します。\n社員番号（例：2338）を入力してください。\n\n"
                       "Starting quitting date request.\nPlease enter your Staff ID (e.g., 2338).")
            continue

        # ---- Continue quitting flow ----
        if user_id and user_id in USER_STATE:
            st = USER_STATE[user_id]

            if st.get("step") == "Q_WAIT_STAFFID":
                if not is_valid_staff_id(user_text):
                    reply_text(reply_token, "社員番号の形式が正しくありません。例：2338\n\nStaff ID example: 2338")
                    continue
                st["staff_id"] = user_text
                st["step"] = "Q_WAIT_DATE"
                reply_text(reply_token,
                           "退職希望日（最後の勤務日）を入力してください。\n形式：YYYY-MM-DD\n例：2026-03-31\n\n"
                           "Enter quitting date.\nFormat: YYYY-MM-DD (e.g., 2026-03-31)")
                continue

            if st.get("step") == "Q_WAIT_DATE":
                if not is_valid_iso_date(user_text):
                    reply_text(reply_token, "日付形式が正しくありません。例：2026-03-31\n\nExample: 2026-03-31")
                    continue
                st["quitting_date"] = user_text
                st["step"] = "Q_WAIT_REASON"
                reply_text_quick(
                    reply_token,
                    "退職理由を選んでください（Choose a reason）",
                    REASON_JP + ["キャンセル（Cancel）"]
                )
                continue

            if st.get("step") == "Q_WAIT_REASON":
                if user_text == "キャンセル（Cancel）" or is_cancel_word(user_text):
                    USER_STATE.pop(user_id, None)
                    reply_text(reply_token, "キャンセルしました。\nCancelled.")
                    continue

                if user_text not in REASON_MAP:
                    reply_text(reply_token, "ボタンから選択してください。")
                    continue

                reason = REASON_MAP[user_text]
                st["reason"] = reason

                if reason == "Other":
                    st["step"] = "Q_WAIT_COMMENT"
                    reply_text(reply_token,
                               "補足コメントがあれば入力してください（なければ「なし」）。\n\n"
                               "Optional comment (or type 'none').")
                else:
                    st["comment"] = ""
                    st["step"] = "Q_CONFIRM"
                    reply_text_quick(
                        reply_token,
                        f"以下で申請します。\nStaffID: {st['staff_id']}\n退職日: {st['quitting_date']}\n理由: {st['reason']}\n\n送信しますか？",
                        ["送信（Submit）", "キャンセル（Cancel）"]
                    )
                continue

            if st.get("step") == "Q_WAIT_COMMENT":
                comment = user_text
                if comment.lower() in ["none", "なし", "無し", "no"]:
                    comment = ""
                st["comment"] = comment
                st["step"] = "Q_CONFIRM"
                reply_text_quick(
                    reply_token,
                    f"以下で申請します。\nStaffID: {st['staff_id']}\n退職日: {st['quitting_date']}\n理由: {st['reason']}\nコメント: {st['comment'] or '(なし)'}\n\n送信しますか？",
                    ["送信（Submit）", "キャンセル（Cancel）"]
                )
                continue

            if st.get("step") == "Q_CONFIRM":
                if user_text == "キャンセル（Cancel）" or is_cancel_word(user_text):
                    USER_STATE.pop(user_id, None)
                    reply_text(reply_token, "キャンセルしました。\nCancelled.")
                    continue
                if user_text != "送信（Submit）":
                    reply_text(reply_token, "ボタンから選択してください。")
                    continue

                payload = {
                    "action": "createQuittingRequest",
                    "lineUserId": user_id,
                    "staffId": st.get("staff_id", ""),
                    "quittingDate": st.get("quitting_date", ""),
                    "reason": st.get("reason", ""),
                    "comment": st.get("comment", ""),
                }
                result = call_apps_script(payload)
                USER_STATE.pop(user_id, None)

                if result.get("ok"):
                    rid = result.get("requestId", "")
                    reply_text(reply_token,
                               f"申請を受け付けました。HRよりご連絡します。\n受付番号: {rid}\n\n"
                               "Your request has been received. HR will review and contact you.")
                else:
                    print("Quitting error:", result, flush=True)
                    reply_text(reply_token,
                               "システム登録でエラーが発生しました。HRへご連絡ください。\n\n"
                               "System error saving it. Please contact HR.")
                continue

        # ---- FAQ fallback ----
        answer = find_faq(user_text)
        if answer:
            reply_text(reply_token, answer)
        else:
            if detect_language(user_text) == "jp":
                reply_text(reply_token, "申し訳ありません。その質問は人事に転送されました。")
            else:
                reply_text(reply_token, "Sorry, HR will follow up on this.")

    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
