from flask import Flask, request
import os
import requests
from datetime import datetime, timedelta
import threading

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import TextSendMessage, MessageEvent, TextMessage

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.environ["CHANNEL_ACCESS_TOKEN"]
CHANNEL_SECRET = os.environ["CHANNEL_SECRET"]
SEND_TOKEN = os.environ["SEND_TOKEN"]

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 重複送信防止用（Render Free の再起動でリセットされる点は許容）
last_morning_sent_date = None
last_evening_sent_date = None


def umbrella_message(prob):
    if prob is None:
        return "☀（不明%）"
    if prob >= 30:
        return f"☂要る（{prob}%）"
    return f"☀（{prob}%）"


def morning_message(prob):
    return f"おはよう。今日の天気は☔（{prob}%）だ。☂持ってこい"


def evening_message(prob):
    return f"お疲れ様だよ。いまの天気は☔（{prob}%）だ。☂持って帰れ"


@app.route("/", methods=["GET"])
def home():
    return "LINE Weather Bot running", 200


@app.route("/healthz", methods=["GET"])
def healthz():
    return "ok", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    if not signature:
        return "OK", 200

    try:
        threading.Thread(
            target=handler.handle,
            args=(body, signature),
            daemon=True
        ).start()
    except InvalidSignatureError:
        return "Invalid signature", 400
    except Exception:
        return "OK", 200

    return "OK", 200


def fetch_precipitation_data():
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=33.5902"
        "&longitude=130.4017"
        "&hourly=precipitation_probability"
        "&timezone=Asia%2FTokyo"
    )

    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()

    times = data["hourly"]["time"]
    probs = data["hourly"]["precipitation_probability"]
    return times, probs


def get_current_precipitation_probability():
    times, probs = fetch_precipitation_data()

    now = datetime.now()
    current_hour = now.strftime("%Y-%m-%dT%H:00")

    for i, t in enumerate(times):
        if t == current_hour:
            return probs[i]

    return None


def get_weather():
    times, probs = fetch_precipitation_data()

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


def get_morning_weather_message():
    current_prob = get_current_precipitation_probability()
    if current_prob is None:
        return None
    if current_prob < 10:
        return None
    return morning_message(current_prob)


def get_evening_weather_message():
    current_prob = get_current_precipitation_probability()
    if current_prob is None:
        return None
    if current_prob < 10:
        return None
    return evening_message(current_prob)


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    message = get_weather()
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=message)
    )


@app.route("/send", methods=["GET"])
def send_weather():
    global last_morning_sent_date, last_evening_sent_date

    token = request.args.get("token", "")
    if token != SEND_TOKEN:
        return "forbidden", 403

    now = datetime.now()

    # 月曜=0, 日曜=6 → 土日スキップ
    if now.weekday() >= 5:
        return "skip: weekend", 200

    today_str = now.strftime("%Y-%m-%d")

    # 朝8時台
    if now.hour == 8:
        if last_morning_sent_date == today_str:
            return "skip: already sent this morning", 200

        message = get_morning_weather_message()
        if message is None:
            return "skip: no rain this morning", 200

        line_bot_api.broadcast(TextSendMessage(text=message))
        last_morning_sent_date = today_str
        return "sent: morning", 200

    # 18時台
    if now.hour == 18:
        if last_evening_sent_date == today_str:
            return "skip: already sent this evening", 200

        message = get_evening_weather_message()
        if message is None:
            return "skip: no rain this evening", 200

        line_bot_api.broadcast(TextSendMessage(text=message))
        last_evening_sent_date = today_str
        return "sent: evening", 200

    return "skip: not notification hour", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)