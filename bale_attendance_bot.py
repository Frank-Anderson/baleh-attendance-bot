#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bale (بله) Attendance Bot
=========================
Location-based class attendance bot for a Bale (web.bale.ai) group.
All user-facing text is Persian. Keeps the group clean by doing all
verification in a private chat and only editing ONE pinned message
in the group (never posting a new line per student).

FILE STRUCTURE OF THIS SCRIPT (for future reference / editing):
  - CONFIG                      : section to edit before running
  - class BaleAPI               : all HTTP calls to the Bale Bot API
  - STATE HELPERS               : load_state / save_state / haversine_m
  - build_attendance_text()     : renders the pinned message text
  - start_session() / end_session()
  - handle_message()            : handles group + private text/location messages
  - handle_callback_query()     : handles the "اعلام حضور" button press
  - main()                      : long-polling loop
"""

import json
import math
import os
import time
from datetime import datetime

import requests

# ============================================================
# CONFIG - EDIT THESE BEFORE RUNNING
# ============================================================

# The four secret/changeable values below are NOT typed in this file anymore.
# They are read from "environment variables" - settings you type into the
# hosting website (Render). This keeps your bot token out of GitHub, even if
# the repository is public. The text after the comma is only the placeholder
# used when the variable is not set (for example when testing on your PC).

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")  # token from @Bot_Father in Bale

UNIVERSITY_NAME = "دانشگاه تهران جنوب"

# Exact coordinates of the classroom / campus. Get these from Google Maps:
# right-click the exact spot on the map -> the first item in the menu is
# the lat,lng pair -> click it to copy, e.g. 35.712345, 51.398765
TARGET_LAT = float(os.environ.get("TARGET_LAT", "35.0"))   # real latitude
TARGET_LON = float(os.environ.get("TARGET_LON", "51.0"))   # real longitude

RADIUS_METERS = 200       # how close a student must be to be accepted

# Bale user IDs allowed to run /شروع_کلاس و /پایان_کلاس.
# Send /myid to the bot in a private chat to find your own ID.
# In Render, write several ids separated by commas, e.g.  123456,789012
# They are always compared as strings (avoids bugs from Bale sending ids
# sometimes as strings and sometimes as numbers).
TEACHER_IDS = {
    item.strip()
    for item in os.environ.get("TEACHER_IDS", "111111111").split(",")
    if item.strip()
}

# State file is anchored to this script's own folder (not the current
# working directory) so it saves in the same place whether you run this
# file directly or PythonAnywhere loads it through app_webhook.py via WSGI.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "attendance_state.json")

API_BASE = f"https://tapi.bale.ai/bot{BOT_TOKEN}/"

# Commands recognized in the group (with or without leading slash)
START_CLASS_TRIGGERS = {"/شروع_کلاس", "/شروع کلاس", "شروع_کلاس", "شروع کلاس"}
END_CLASS_TRIGGERS = {"/پایان_کلاس", "/پایان کلاس", "پایان_کلاس", "پایان کلاس"}


# ============================================================
# BaleAPI - thin wrapper around the HTTP endpoints
# ============================================================

class BaleAPI:
    def __init__(self, base_url):
        self.base_url = base_url

    def call(self, method, params=None):
        """POST to https://tapi.bale.ai/bot<token>/<method> and return parsed JSON.
        Never raises on API-level errors (ok: false) - caller checks res.get('ok')."""
        url = self.base_url + method
        try:
            resp = requests.post(url, json=params or {}, timeout=30)
            return resp.json()
        except Exception as exc:
            print(f"[BaleAPI] request to {method} failed: {exc}")
            return {"ok": False, "description": str(exc)}

    def get_updates(self, offset, limit=100):
        return self.call("getUpdates", {"offset": offset, "limit": limit})

    def send_message(self, chat_id, text, reply_markup=None):
        params = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self.call("sendMessage", params)

    def edit_message_text(self, chat_id, message_id, text):
        return self.call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text,
        })

    def delete_message(self, chat_id, message_id):
        return self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def pin_chat_message(self, chat_id, message_id):
        return self.call("pinChatMessage", {"chat_id": chat_id, "message_id": message_id})

    def unpin_chat_message(self, chat_id, message_id):
        return self.call("unpinChatMessage", {"chat_id": chat_id, "message_id": message_id})

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        params = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text:
            params["text"] = text
        return self.call("answerCallbackQuery", params)


api = BaleAPI(API_BASE)


# ============================================================
# STATE HELPERS
# ============================================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "update_offset": 0,
        "sessions": {},   # group_chat_id (str) -> session dict
        "pending": {},     # user_id (str) -> {"group_chat_id":, "name":}
        "pv_warned": {},   # f"{group_chat_id}_{user_id}" -> True
    }


def save_state(state):
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STATE_FILE)


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in meters between two lat/lon points."""
    r = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def try_delete(chat_id, message_id):
    if message_id is None:
        return
    try:
        api.delete_message(chat_id, message_id)
    except Exception:
        pass


def try_answer_callback(callback_query_id, text=None, show_alert=False):
    try:
        api.answer_callback_query(callback_query_id, text=text, show_alert=show_alert)
    except Exception:
        pass


# ============================================================
# ATTENDANCE SHEET RENDERING
# ============================================================

def build_attendance_text(session):
    lines = [
        "📋 حضور و غیاب کلاس",
        f"📅 تاریخ: {session['date']}",
        f"📍 محل: {UNIVERSITY_NAME}",
        "",
    ]
    confirmed = session["confirmed"]
    if not confirmed:
        lines.append("هنوز دانشجویی اعلام حضور نکرده است.")
    else:
        lines.append(f"✅ تعداد حاضرین: {len(confirmed)} نفر")
        lines.append("")
        ordered = sorted(confirmed.items(), key=lambda kv: kv[1]["time"])
        for i, (_uid, info) in enumerate(ordered, start=1):
            lines.append(f"{i}. {info['name']} — {info['time']}")
    if not session.get("active", True):
        lines.append("")
        lines.append("🔒 این جلسه به پایان رسیده است.")
    return "\n".join(lines)


def refresh_group_sheet(state, group_chat_id, session):
    text = build_attendance_text(session)
    api.edit_message_text(group_chat_id, session["list_message_id"], text)
    save_state(state)


# ============================================================
# SESSION LIFECYCLE
# ============================================================

def start_session(state, group_chat_id):
    key = str(group_chat_id)
    old = state["sessions"].get(key)
    if old and old.get("list_message_id"):
        try:
            api.unpin_chat_message(group_chat_id, old["list_message_id"])
        except Exception:
            pass

    session = {
        "active": True,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "list_message_id": None,
        "confirmed": {},
    }
    state["sessions"][key] = session

    res = api.send_message(group_chat_id, build_attendance_text(session))
    if res.get("ok"):
        msg_id = res["result"]["message_id"]
        session["list_message_id"] = msg_id
        api.pin_chat_message(group_chat_id, msg_id)

    button_markup = {
        "inline_keyboard": [[{"text": "📍 اعلام حضور", "callback_data": "attend"}]]
    }
    api.send_message(
        group_chat_id,
        "برای اعلام حضور در این جلسه، روی دکمه زیر بزنید 👇",
        reply_markup=button_markup,
    )
    save_state(state)


def end_session(state, group_chat_id):
    key = str(group_chat_id)
    session = state["sessions"].get(key)
    if not session:
        return
    session["active"] = False
    refresh_group_sheet(state, group_chat_id, session)
    save_state(state)


# ============================================================
# UPDATE HANDLERS
# ============================================================

def handle_private_location(state, user_id, name, location):
    key = str(user_id)
    pending_entry = state["pending"].get(key)
    if not pending_entry:
        api.send_message(
            user_id,
            "الان منتظر موقعیت مکانی از شما نبودم.\n"
            "لطفاً ابتدا در گروه کلاس روی دکمه «📍 اعلام حضور» بزنید.",
        )
        return

    dist = haversine_m(location["latitude"], location["longitude"], TARGET_LAT, TARGET_LON)

    if dist <= RADIUS_METERS:
        group_chat_id = pending_entry["group_chat_id"]
        session = state["sessions"].get(str(group_chat_id))
        if session and session.get("active"):
            session["confirmed"][key] = {
                "name": pending_entry["name"],
                "time": datetime.now().strftime("%H:%M"),
            }
            del state["pending"][key]
            api.send_message(user_id, "✅ حضور شما با موفقیت در کلاس ثبت شد. موفق باشید!")
            refresh_group_sheet(state, group_chat_id, session)
        else:
            api.send_message(user_id, "این جلسه دیگر فعال نیست.")
            del state["pending"][key]
            save_state(state)
    else:
        api.send_message(
            user_id,
            f"❌ فاصله شما تا {UNIVERSITY_NAME} حدود {int(dist)} متر است، "
            f"که بیشتر از حد مجاز ({RADIUS_METERS} متر) است.\n"
            "لطفاً داخل محدوده کلاس شوید و دوباره موقعیت خود را ارسال کنید.",
        )
        save_state(state)


def handle_message(state, message):
    chat = message["chat"]
    chat_id = chat["id"]
    chat_type = chat["type"]
    text = (message.get("text") or "").strip()
    location = message.get("location")
    sender = message.get("from") or {}
    user_id = sender.get("id")

    if chat_type == "private":
        if location and user_id is not None:
            handle_private_location(state, user_id, None, location)
        elif text == "/start":
            api.send_message(
                user_id,
                "سلام! 👋\n"
                "برای اعلام حضور در کلاس، به گروه کلاس بروید و روی دکمه "
                "«📍 اعلام حضور» بزنید. سپس از همینجا موقعیت مکانی خود را برایم بفرستید.",
            )
        elif text == "/myid":
            api.send_message(user_id, f"شناسه عددی شما: {user_id}")
        elif location is None:
            # Ignore other private chit-chat quietly, or reply minimally.
            pass
        return

    if chat_type in ("group", "supergroup"):
        if text in START_CLASS_TRIGGERS:
            try_delete(chat_id, message.get("message_id"))
            if str(user_id) in TEACHER_IDS:
                start_session(state, chat_id)
            return
        if text in END_CLASS_TRIGGERS:
            try_delete(chat_id, message.get("message_id"))
            if str(user_id) in TEACHER_IDS:
                end_session(state, chat_id)
            return
        # Any other group text/messages: intentionally ignored to keep the
        # group free of bot noise.
        return


def handle_callback_query(state, callback_query):
    data = callback_query.get("data")
    if data != "attend":
        return

    cq_id = callback_query["id"]
    from_user = callback_query["from"]
    message = callback_query.get("message") or {}
    group_chat = message.get("chat") or {}
    group_chat_id = group_chat.get("id")

    session = state["sessions"].get(str(group_chat_id))
    if not session or not session.get("active"):
        try_answer_callback(cq_id, "در حال حاضر جلسه‌ای برای حضور و غیاب فعال نیست.", show_alert=True)
        return

    uid = str(from_user["id"])
    name = (from_user.get("first_name", "") + " " + from_user.get("last_name", "")).strip()
    if not name:
        name = from_user.get("username", "دانشجو")

    if uid in session["confirmed"]:
        try_answer_callback(cq_id, "شما قبلاً حضور خود را ثبت کرده‌اید ✅", show_alert=True)
        return

    dm_text = (
        f"سلام {name} 👋\n"
        f"برای ثبت حضور در «{UNIVERSITY_NAME}»، لطفاً موقعیت مکانی فعلی خود را "
        "با دکمه زیر (یا از طریق پیوست > موقعیت مکانی) ارسال کنید."
    )
    location_keyboard = {
        "keyboard": [[{"text": "📍 ارسال موقعیت مکانی من", "request_location": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }
    res = api.send_message(from_user["id"], dm_text, reply_markup=location_keyboard)

    if res.get("ok"):
        state["pending"][uid] = {"group_chat_id": group_chat_id, "name": name}
        try_answer_callback(cq_id, "یک پیام خصوصی برایتان ارسال شد؛ لطفاً از آنجا موقعیت خود را بفرستید 📍")
    else:
        try_answer_callback(
            cq_id,
            "ابتدا در پیام خصوصی به بازو /start بزنید، سپس دوباره روی این دکمه بزنید.",
            show_alert=True,
        )
        warn_key = f"{group_chat_id}_{uid}"
        if not state["pv_warned"].get(warn_key):
            api.send_message(
                group_chat_id,
                f"⚠️ {name} لطفاً یک‌بار در خصوصی به بازو پیام /start بدهید تا بتوانم موقعیت شما را دریافت کنم، سپس دوباره دکمه را بزنید.",
            )
            state["pv_warned"][warn_key] = True

    save_state(state)


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    state = load_state()
    print("Bale attendance bot started. Polling for updates...")
    while True:
        res = api.get_updates(state["update_offset"])
        if not res.get("ok"):
            print("[main] getUpdates failed:", res.get("description"))
            time.sleep(3)
            continue

        updates = res.get("result", [])
        for update in updates:
            state["update_offset"] = update["update_id"] + 1
            try:
                if "message" in update:
                    handle_message(state, update["message"])
                elif "callback_query" in update:
                    handle_callback_query(state, update["callback_query"])
            except Exception as exc:
                print("[main] error handling update:", exc)
            save_state(state)

        if not updates:
            time.sleep(1.5)


if __name__ == "__main__":
    # If main() ever raises (network drop, temporary Bale API outage, etc.),
    # don't let the whole bot die - log it and restart after a short pause.
    # Only Ctrl+C (KeyboardInterrupt) actually stops it.
    while True:
        try:
            main()
        except KeyboardInterrupt:
            print("Stopped by user (Ctrl+C).")
            break
        except Exception as exc:
            print(f"[main] bot crashed with: {exc}\nRestarting in 5 seconds...")
            time.sleep(5)
