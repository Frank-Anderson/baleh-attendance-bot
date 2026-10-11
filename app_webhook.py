# -*- coding: utf-8 -*-
"""
Webhook version of the bot, for free always-on hosting on PythonAnywhere.

Instead of the bot repeatedly asking Bale "any new messages?" (long-polling,
what bale_attendance_bot.py's main() does), Bale itself calls this URL the
instant a message/button-press happens.

This file imports everything from bale_attendance_bot.py - CONFIG values,
BaleAPI, handle_message(), handle_callback_query(), load_state()/save_state() -
so you only ever edit BOT_TOKEN / TARGET_LAT / TARGET_LON / TEACHER_IDS in
that one file, never here.

THREE ROUTES, for debugging without needing to touch Bale at all:
  GET  /                      -> confirms the web app is alive at all
  GET  /config-check          -> confirms your config values were actually
                                  filled in (not still the placeholders)
  GET  /webhook/<token>       -> confirms this exact URL/route exists
                                  (visit it in a browser - it will NOT
                                  process a real update, since Bale only
                                  ever sends POST here, but a 200 response
                                  proves the route is registered correctly,
                                  which is the single most common thing
                                  that goes wrong in this setup)
  POST /webhook/<token>       -> the real endpoint Bale sends updates to
"""

from flask import Flask, request

import bale_attendance_bot as bot

app = Flask(__name__)

WEBHOOK_PATH = f"/webhook/{bot.BOT_TOKEN}"


@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    # This print() lands in PythonAnywhere's error log even though nothing
    # is wrong - it's the easiest way to confirm "did a request even arrive"
    # without having to inspect Bale's side at all.
    print(f"[webhook] received update with keys: {list(update.keys())}")

    state = bot.load_state()
    try:
        if "message" in update:
            bot.handle_message(state, update["message"])
        elif "callback_query" in update:
            bot.handle_callback_query(state, update["callback_query"])
    except Exception as exc:
        print(f"[webhook] error handling update: {exc}")
    bot.save_state(state)
    return {"ok": True}


@app.route(WEBHOOK_PATH, methods=["GET"])
def webhook_probe():
    return (
        "This route exists and is reachable by GET.\n"
        "Real updates from Bale arrive via POST, not by visiting this page - "
        "but if you can see this text, the route itself is registered correctly."
    )


@app.route("/config-check", methods=["GET"])
def config_check():
    token_filled_in = "PUT_YOUR_BOT_TOKEN_HERE" not in bot.BOT_TOKEN
    coords_filled_in = not (bot.TARGET_LAT == 35.0 and bot.TARGET_LON == 51.0)
    teacher_filled_in = "111111111" not in bot.TEACHER_IDS
    return {
        "bot_token_looks_configured": token_filled_in,
        "coordinates_look_configured": coords_filled_in,
        "teacher_ids_look_configured": teacher_filled_in,
        "webhook_route_prefix": WEBHOOK_PATH[:10] + "...(rest hidden)",
    }


@app.route("/", methods=["GET"])
def health():
    return "Bale attendance bot webhook is running."


if __name__ == "__main__":
    app.run(debug=True)
