"""
WhatsApp অটো-রিপ্লাই বট (WhatsApp Cloud API)
- আপনি না থাকলে সাধারণ মেসেজের উত্তর দেয়
- জরুরি কল চাইলে তথ্য নিয়ে কল শিডিউল করে এবং আপনাকে নোটিফাই করে

মালিকের কমান্ড (শুধু OWNER_NUMBER থেকে): /on  /off  /list
"""
import os
import json
import time
from datetime import datetime

import requests
from flask import Flask, request

app = Flask(__name__)

# ---------- সেটিংস (Environment Variable থেকে) ----------
TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my-verify-token")
OWNER_NUMBER = os.environ.get("OWNER_NUMBER", "")  # যেমন 8801XXXXXXXXX
OWNER_NAME = os.environ.get("OWNER_NAME", "আমার মালিক")

CALLBACKS_FILE = "callbacks.json"
GREET_EVERY_SECONDS = 12 * 60 * 60  # ১২ ঘণ্টায় একবার গ্রিটিং

# ---------- FAQ (এখানে নিজের উত্তর বসান) ----------
FAQS = [
    (["দাম", "price", "cost", "রেট", "কত টাকা"],
     "দাম সম্পর্কে বিস্তারিত {owner} ফিরে এসে আপনাকে জানাবেন।"),
    (["সময়", "অফিস", "কখন", "hours", "open"],
     "সাধারণত আমরা সকাল ১০টা থেকে সন্ধ্যা ৬টা পর্যন্ত থাকি।"),
    (["ঠিকানা", "address", "location", "লোকেশন"],
     "ঠিকানা সম্পর্কে {owner} নিজে আপনাকে জানাবেন।"),
]

# ---------- মেসেজ টেমপ্লেট ----------
M = {
    "greeting": {
        "bn": "আসসালামু আলাইকুম! আমি {owner}-এর অ্যাসিস্ট্যান্ট। তিনি এখন অ্যাভেলেবেল নেই। আপনার মেসেজ তাকে জানিয়ে দেব। কীভাবে সাহায্য করতে পারি?",
        "en": "Hello! I'm {owner}'s assistant. They're not available right now, but I'll pass your message along. How can I help?",
    },
    "fallback": {
        "bn": "এই বিষয়ে আমি নিশ্চিত নই। {owner} ফিরে এসে নিজে আপনাকে রিপ্লাই দেবেন।",
        "en": "I'm not sure about that. {owner} will reply to you personally once they're back.",
    },
    "emergency_intro": {
        "bn": "{owner} এই মুহূর্তে অ্যাভেলেবেল নেই। অ্যাভেলেবেল হলে আপনার কলটি শিডিউল করে রাখা হবে এবং তিনিই আপনাকে কল করবেন। কিছু তথ্য দরকার।\n\nআপনার নাম কী?",
        "en": "{owner} isn't available right now. Your call will be scheduled and they'll call you once available. I need a few details.\n\nWhat's your name?",
    },
    "ask_number": {
        "bn": "ধন্যবাদ, {name}। কোন নম্বরে কল করবেন? (এই নম্বরেই হলে \"এই নম্বর\" লিখুন)",
        "en": "Thanks, {name}. Which number should they call? (Type \"same\" to use this number)",
    },
    "ask_reason": {
        "bn": "জরুরি বিষয়টা সংক্ষেপে লিখুন।",
        "en": "Please briefly describe what it's about.",
    },
    "ask_time": {
        "bn": "কখন কল পেলে সুবিধা হবে? (যেমন: আজ রাত ৯টার পর)",
        "en": "When is the best time to call you? (e.g. after 9 PM today)",
    },
    "confirm": {
        "bn": "✅ কল শিডিউল করা হয়েছে।\n\nনাম: {name}\nনম্বর: {number}\nবিষয়: {reason}\nসুবিধাজনক সময়: {time}\n\n{owner} অ্যাভেলেবেল হলে আপনাকে কল করবেন। (নির্দিষ্ট সময়ের নিশ্চয়তা দেওয়া সম্ভব নয়।)\n\nপ্রাণঘাতী জরুরি অবস্থা হলে অনুগ্রহ করে এখনই ৯৯৯-এ কল করুন।",
        "en": "✅ Call scheduled.\n\nName: {name}\nNumber: {number}\nReason: {reason}\nBest time: {time}\n\n{owner} will call you once available. (An exact time can't be guaranteed.)\n\nIf this is a life-threatening emergency, please call 999 right away.",
    },
}

EMERGENCY_WORDS = [
    "emergency", "urgent", "call me", "need a call", "জরুরি", "এমার্জেন্সি",
    "ইমার্জেন্সি", "কল দরকার", "কল লাগবে", "কল করতে হবে", "এখনই কথা",
]
SAME_NUMBER_WORDS = ["এই নম্বর", "এই নাম্বার", "same", "this number"]

# ---------- অবস্থা ----------
bot_active = True
sessions = {}      # sender -> {"step": ..., "data": {...}}
last_greeted = {}  # sender -> timestamp


# ---------- সহায়ক ফাংশন ----------
def lang_of(text):
    return "bn" if any("\u0980" <= ch <= "\u09FF" for ch in text) else "en"


def msg(key, lang, **kw):
    return M[key][lang].format(owner=OWNER_NAME, **kw)


def send(to, text):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        if r.status_code >= 400:
            print("Send error:", r.status_code, r.text)
    except requests.RequestException as e:
        print("Send exception:", e)


def load_callbacks():
    try:
        with open(CALLBACKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_callback(entry):
    items = load_callbacks()
    items.append(entry)
    with open(CALLBACKS_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def is_emergency(text):
    t = text.lower()
    return any(w in t for w in EMERGENCY_WORDS)


def find_faq(text):
    t = text.lower()
    for keywords, answer in FAQS:
        if any(k in t for k in keywords):
            return answer.format(owner=OWNER_NAME)
    return None


# ---------- মালিকের কমান্ড ----------
def handle_owner_command(text):
    global bot_active
    cmd = text.strip().lower()
    if cmd == "/off":
        bot_active = False
        return "বট বন্ধ করা হলো। এখন থেকে আপনি নিজে রিপ্লাই দেবেন।"
    if cmd == "/on":
        bot_active = True
        return "বট চালু করা হলো। আপনার হয়ে এখন বট রিপ্লাই দেবে।"
    if cmd == "/list":
        items = load_callbacks()
        pending = [c for c in items if c.get("status") == "pending"]
        if not pending:
            return "কোনো পেন্ডিং কল নেই।"
        lines = [
            f"{i}. {c['name']} - {c['number']}\n   বিষয়: {c['reason']}\n   সময়: {c['time']}"
            for i, c in enumerate(pending, 1)
        ]
        return "পেন্ডিং কল:\n\n" + "\n\n".join(lines)
    return None


# ---------- জরুরি কল ফ্লো ----------
def handle_emergency_step(sender, text):
    s = sessions[sender]
    lang = s["lang"]
    step, d = s["step"], s["data"]

    if step == "name":
        d["name"] = text.strip()
        s["step"] = "number"
        send(sender, msg("ask_number", lang, name=d["name"]))
    elif step == "number":
        if any(w in text.lower() for w in SAME_NUMBER_WORDS):
            d["number"] = sender
        else:
            d["number"] = text.strip()
        s["step"] = "reason"
        send(sender, msg("ask_reason", lang))
    elif step == "reason":
        d["reason"] = text.strip()
        s["step"] = "time"
        send(sender, msg("ask_time", lang))
    elif step == "time":
        d["time"] = text.strip()
        entry = {
            "name": d["name"],
            "number": d["number"],
            "reason": d["reason"],
            "time": d["time"],
            "whatsapp": sender,
            "created": datetime.now().isoformat(timespec="seconds"),
            "status": "pending",
        }
        save_callback(entry)
        send(sender, msg("confirm", lang, **{k: d[k] for k in ("name", "number", "reason", "time")}))
        if OWNER_NUMBER:
            send(
                OWNER_NUMBER,
                "🚨 জরুরি কল অনুরোধ\n\n"
                f"নাম: {d['name']}\nনম্বর: {d['number']}\n"
                f"বিষয়: {d['reason']}\nসুবিধাজনক সময়: {d['time']}",
            )
        del sessions[sender]


# ---------- মূল মেসেজ হ্যান্ডলার ----------
def handle_message(sender, text):
    # মালিকের কমান্ড
    if sender == OWNER_NUMBER:
        reply = handle_owner_command(text)
        if reply:
            send(sender, reply)
        return

    if not bot_active:
        return

    # চলমান জরুরি ফ্লো
    if sender in sessions:
        handle_emergency_step(sender, text)
        return

    lang = lang_of(text)

    # নতুন জরুরি অনুরোধ
    if is_emergency(text):
        sessions[sender] = {"step": "name", "data": {}, "lang": lang}
        send(sender, msg("emergency_intro", lang))
        return

    # গ্রিটিং (১২ ঘণ্টায় একবার)
    now = time.time()
    if now - last_greeted.get(sender, 0) > GREET_EVERY_SECONDS:
        last_greeted[sender] = now
        send(sender, msg("greeting", lang))
        # গ্রিটিংয়ের পর FAQ মিললে সেটাও পাঠাও
        faq = find_faq(text)
        if faq:
            send(sender, faq)
        return

    # FAQ বা ফলব্যাক
    faq = find_faq(text)
    send(sender, faq if faq else msg("fallback", lang))


# ---------- Webhook ----------
@app.route("/webhook", methods=["GET"])
def verify():
    if (
        request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return request.args.get("hub.challenge", ""), 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                for m in change.get("value", {}).get("messages", []):
                    if m.get("type") == "text":
                        handle_message(m["from"], m["text"]["body"])
    except Exception as e:  # ক্র্যাশ এড়াতে
        print("Webhook error:", e)
    return "OK", 200


@app.route("/")
def home():
    return "WhatsApp bot is running."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
