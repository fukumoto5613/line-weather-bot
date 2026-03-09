from flask import Flask, request

app = Flask(__name__)

@app.route("/")
def home():
    return "LINE Weather Bot running"

@app.route("/webhook", methods=["POST"])
def webhook():
    return "OK", 200

import requests
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import TextSendMessage, MessageEvent, TextMessage
from datetime import datetime, timedelta


def umbrella_message(prob):
    if prob is None:
        return "☀（不明%）"
    if prob >= 30:
        return f"☂要る（{prob}%）"
    return f"☀（{prob}%）"


app = Flask(__name__)

import os

CHANNEL_ACCESS_TOKEN = os.environ["CHANNEL_ACCESS_TOKEN"]
CHANNEL_SECRET = os.environ["CHANNEL_SECRET"]

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    print("Webhook body:", body)

    if not signature:
        return "OK", 200

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return "Invalid signature", 400
    except Exception as e:
        print("Webhook error:", e)
        return "Internal error", 500

    return "OK", 200


def get_weather():

    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=33.5902"
        "&longitude=130.4017"
        "&hourly=precipitation_probability"
        "&timezone=Asia%2FTokyo"
    )

    r = requests.get(url, timeout=10)
    data = r.json()

    times = data["hourly"]["time"]
    probs = data["hourly"]["precipitation_probability"]

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    current_hour = now.strftime("%Y-%m-%dT%H:00")

    now_prob = None
    prob_19 = None
    prob_8 = None

    for i, t in enumerate(times):
        if t == current_hour:
            now_prob = probs[i]

        if t == f"{today}T19:00":
            prob_19 = probs[i]

        if t == f"{tomorrow}T08:00":
            prob_8 = probs[i]

    return (
        f"中央区の天気\n"
        f"いま：{umbrella_message(now_prob)}\n"
        f"帰り：{umbrella_message(prob_19)}\n"
        f"明日：{umbrella_message(prob_8)}"
    )


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    message = get_weather()
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=message)
    )


@app.route("/send")
def send_weather():
    message = get_weather()
    line_bot_api.broadcast(TextSendMessage(text=message))
    return "sent"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
