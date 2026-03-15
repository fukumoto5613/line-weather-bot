from flask import Flask, request
import os
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import threading

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import TextSendMessage, MessageEvent, TextMessage

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.environ["CHANNEL_ACCESS_TOKEN"]
CHANNEL_SECRET = os.environ["CHANNEL_SECRET"]
SEND_TOKEN = os.environ["SEND_TOKEN"]
TEST_USER_ID = os.environ["TEST_USER_ID"]
APP_VERSION = os.environ.get("APP_VERSION", "2026-03-15-1")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
JST = ZoneInfo("Asia/Tokyo")

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
    if prob is None:
        return "おはよう。今日の天気は不明だ。あとでまた確認してくれ"
    if prob >= 10:
        return f"おはよう。今日の天気は☔（{prob}%）だ。☂持ってこい"
    return f"おはよう。今日の天気は☀（{prob}%）だ。今日は傘はなくてよさそうだ"


def evening_message(prob):
    if prob is None:
        return "お疲れ様だよ。いまの天気は不明だ。あとでまた確認してくれ"
    if prob >= 10:
        return f"お疲れ様だよ。いまの天気は☔（{prob}%）だ。☂持って帰れ"
    return f"お疲れ様だよ。いまの天気は☀（{prob}%）だ。帰りは傘なしで大丈夫そうだ"


def send_notification(text):
    line_bot_api.push_message(TEST_USER_ID, TextSendMessage(text=text))


@app.route("/", methods=["GET"])
def home():
    return f"LINE Weather Bot running / version={APP_VERSION}", 200


@app.route("/healthz", methods=["GET"])
def healthz():
    return f"ok / version={APP_VERSION}", 200


@app.route("/version", methods=["GET"])
def version():
    return {"app": "line-weather-bot", "version": APP_VERSION}, 200


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
    return dict(zip(times, probs))


def get_current_precipitation_probability():
    forecast = fetch_precipitation_data()
    now = datetime.now(JST)
    current_hour = now.strftime("%Y-%m-%dT%H:00")
    return forecast.get(current_hour)


def get_weather():
    forecast = fetch_precipitation_data()

    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    current_hour = now.strftime("%Y-%m-%dT%H:00")

    now_prob = forecast.get(current_hour)
    prob_19 = forecast.get(f"{today}T19:00")
    prob_8 = forecast.get(f"{tomorrow}T08:00")

    return (
        f"中央区の天気\n"
        f"version：{APP_VERSION}\n"
        f"いま：{umbrella_message(now_prob)}\n"
        f"帰り：{umbrella_message(prob_19)}\n"
        f"明日：{umbrella_message(prob_8)}"
    )


def get_morning_weather_message():
    current_prob = get_current_precipitation_probability()
    return morning_message(current_prob)



def get_evening_weather_message():
    current_prob = get_current_precipitation_probability()
    return evening_message(current_prob)


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        message = get_weather()
    except Exception:
        message = (
            f"天気の取得に失敗しました。少ししてからもう一度試してください。\n"
            f"version：{APP_VERSION}"
        )

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

    now = datetime.now(JST)

    # 月曜=0, 日曜=6 → 土日スキップ
    if now.weekday() >= 5:
        return "skip: weekend", 200

    today_str = now.strftime("%Y-%m-%d")

    # 朝8時台
    if now.hour == 8:
        if last_morning_sent_date == today_str:
            return "skip: already sent this morning", 200

        message = get_morning_weather_message()
        send_notification(message)
        last_morning_sent_date = today_str
        return f"sent: morning to TEST_USER_ID / version={APP_VERSION}", 200

    # 18時台
    if now.hour == 18:
        if last_evening_sent_date == today_str:
            return "skip: already sent this evening", 200

        message = get_evening_weather_message()
        send_notification(message)
        last_evening_sent_date = today_str
        return f"sent: evening to TEST_USER_ID / version={APP_VERSION}", 200

    return f"skip: not notification hour / version={APP_VERSION}", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
