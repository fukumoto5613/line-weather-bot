from dataclasses import dataclass
from datetime import datetime, timedelta
import os
import threading
from typing import Callable, Dict, Optional
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage


app = Flask(__name__)


# =========================
# Configuration
# =========================
CHANNEL_ACCESS_TOKEN = os.environ["CHANNEL_ACCESS_TOKEN"]
CHANNEL_SECRET = os.environ["CHANNEL_SECRET"]
SEND_TOKEN = os.environ["SEND_TOKEN"]
APP_VERSION = os.environ.get("APP_VERSION", "2026-04-12-1")

JST = ZoneInfo("Asia/Tokyo")
LATITUDE = 33.5902
LONGITUDE = 130.4017
OPEN_METEO_TIMEOUT_SECONDS = 10
FORECAST_CACHE_SECONDS = 300

MORNING_NOTIFICATION_HOUR = 7
MORNING_NOTIFICATION_START_MINUTE = 30
EVENING_NOTIFICATION_HOUR = 18
MORNING_CURRENT_RAIN_THRESHOLD = 50
MORNING_RETURN_RAIN_THRESHOLD = 10
EVENING_RETURN_RAIN_THRESHOLD = 30
UMBRELLA_THRESHOLD = 30


# =========================
# External clients
# =========================
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
http_session = requests.Session()


# =========================
# Runtime state
# =========================
_forecast_cache: Dict[str, object] = {"fetched_at": None, "forecast": None}
_forecast_cache_lock = threading.Lock()

_last_sent_dates: Dict[str, Optional[str]] = {
    "morning": None,
    "evening": None,
}
_last_sent_dates_lock = threading.Lock()


@dataclass(frozen=True)
class ForecastSnapshot:
    current_prob: Optional[int]
    today_19_prob: Optional[int]
    tomorrow_8_prob: Optional[int]


@dataclass(frozen=True)
class NotificationWindow:
    name: str
    hour: int
    last_sent_key: str
    build_message: Callable[[ForecastSnapshot], Optional[str]]
    start_minute: int = 0


# =========================
# Formatting helpers
# =========================
def umbrella_message(prob: Optional[int]) -> str:
    if prob is None:
        return "☀（不明%）"
    if prob >= UMBRELLA_THRESHOLD:
        return f"☂要る（{prob}%）"
    return f"☀（{prob}%）"


def build_weather_message(snapshot: ForecastSnapshot) -> str:
    return (
        f"中央区の天気\n"
        f"version：{APP_VERSION}\n"
        f"いま：{umbrella_message(snapshot.current_prob)}\n"
        f"帰り：{umbrella_message(snapshot.today_19_prob)}\n"
        f"明日：{umbrella_message(snapshot.tomorrow_8_prob)}"
    )


def build_morning_alert_message(snapshot: ForecastSnapshot) -> Optional[str]:
    current_is_rainy = (
        snapshot.current_prob is not None
        and snapshot.current_prob >= MORNING_CURRENT_RAIN_THRESHOLD
    )
    return_is_rainy = (
        snapshot.today_19_prob is not None
        and snapshot.today_19_prob >= MORNING_RETURN_RAIN_THRESHOLD
    )

    if current_is_rainy and return_is_rainy:
        return (
            f"朝だ。今の天気は☔（{snapshot.current_prob}%）だ。"
            f"帰りの天気も☔（{snapshot.today_19_prob}%）だ。☂持ってこい"
        )
    if current_is_rainy:
        return f"朝だ。今の天気は☔（{snapshot.current_prob}%）だ。☂持ってこい"
    if return_is_rainy:
        return f"朝だ。帰りの天気は☔（{snapshot.today_19_prob}%）だ。☂持ってこい"
    return None


def build_evening_alert_message(snapshot: ForecastSnapshot) -> Optional[str]:
    if (
        snapshot.today_19_prob is None
        or snapshot.today_19_prob < EVENING_RETURN_RAIN_THRESHOLD
    ):
        return None
    return f"帰りの時間だ。19時の天気は☔（{snapshot.today_19_prob}%）だ。☂持って帰れ"


# =========================
# Forecast helpers
# =========================
def _forecast_api_url() -> str:
    return (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LATITUDE}"
        f"&longitude={LONGITUDE}"
        "&hourly=precipitation_probability"
        "&timezone=Asia%2FTokyo"
    )


def _fetch_precipitation_data_from_api() -> Dict[str, int]:
    response = http_session.get(_forecast_api_url(), timeout=OPEN_METEO_TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    probabilities = hourly.get("precipitation_probability", [])

    if len(times) != len(probabilities):
        raise ValueError("Open-Meteo response is invalid: hourly arrays length mismatch")

    return dict(zip(times, probabilities))


def get_precipitation_forecast(force_refresh: bool = False) -> Dict[str, int]:
    now = datetime.now(JST)

    with _forecast_cache_lock:
        fetched_at = _forecast_cache["fetched_at"]
        forecast = _forecast_cache["forecast"]

        if (
            not force_refresh
            and fetched_at is not None
            and forecast is not None
            and (now - fetched_at).total_seconds() < FORECAST_CACHE_SECONDS
        ):
            return forecast  # type: ignore[return-value]

    forecast = _fetch_precipitation_data_from_api()

    with _forecast_cache_lock:
        _forecast_cache["fetched_at"] = now
        _forecast_cache["forecast"] = forecast

    return forecast


def build_snapshot(
    forecast: Optional[Dict[str, int]] = None,
    now: Optional[datetime] = None,
) -> ForecastSnapshot:
    current_time = now or datetime.now(JST)
    forecast_map = forecast or get_precipitation_forecast()

    today = current_time.strftime("%Y-%m-%d")
    tomorrow = (current_time + timedelta(days=1)).strftime("%Y-%m-%d")
    current_hour = current_time.strftime("%Y-%m-%dT%H:00")

    return ForecastSnapshot(
        current_prob=forecast_map.get(current_hour),
        today_19_prob=forecast_map.get(f"{today}T19:00"),
        tomorrow_8_prob=forecast_map.get(f"{tomorrow}T08:00"),
    )


# =========================
# LINE / notification helpers
# =========================
def reply_text(reply_token: str, text: str) -> None:
    line_bot_api.reply_message(reply_token, TextSendMessage(text=text))


def broadcast_text(text: str) -> None:
    line_bot_api.broadcast(TextSendMessage(text=text))


def is_already_sent(window_key: str, today_str: str) -> bool:
    with _last_sent_dates_lock:
        return _last_sent_dates.get(window_key) == today_str


def mark_as_sent(window_key: str, today_str: str) -> None:
    with _last_sent_dates_lock:
        _last_sent_dates[window_key] = today_str


def is_weekend(now: datetime) -> bool:
    return now.weekday() >= 5


def process_notification_window(
    window: NotificationWindow,
    now: datetime,
) -> tuple[str, int]:
    today_str = now.strftime("%Y-%m-%d")

    if is_weekend(now):
        return f"skip: weekend / version={APP_VERSION}", 200

    if now.hour != window.hour:
        return f"skip: not {window.name} notification hour / version={APP_VERSION}", 200

    if now.minute < window.start_minute:
        return f"skip: before {window.name} notification minute / version={APP_VERSION}", 200

    if is_already_sent(window.last_sent_key, today_str):
        return f"skip: already sent this {window.name}", 200

    try:
        forecast = get_precipitation_forecast()
        snapshot = build_snapshot(forecast=forecast, now=now)
        message = window.build_message(snapshot)
    except Exception:
        app.logger.exception("Failed to build %s notification", window.name)
        return f"error: failed to fetch forecast / version={APP_VERSION}", 500

    if message is None:
        return f"skip: {window.name} conditions not met / version={APP_VERSION}", 200

    broadcast_text(message)
    mark_as_sent(window.last_sent_key, today_str)
    return f"sent: {window.name} broadcast / version={APP_VERSION}", 200


MORNING_WINDOW = NotificationWindow(
    name="morning",
    hour=MORNING_NOTIFICATION_HOUR,
    start_minute=MORNING_NOTIFICATION_START_MINUTE,
    last_sent_key="morning",
    build_message=build_morning_alert_message,
)

EVENING_WINDOW = NotificationWindow(
    name="evening",
    hour=EVENING_NOTIFICATION_HOUR,
    last_sent_key="evening",
    build_message=build_evening_alert_message,
)


# =========================
# Routes
# =========================
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
        handler.handle(body, signature)
    except InvalidSignatureError:
        return "Invalid signature", 400
    except Exception:
        app.logger.exception("Unhandled error in webhook")
        return "OK", 200

    return "OK", 200


@app.route("/send", methods=["GET"])
def send_weather():
    token = request.args.get("token", "")
    if token != SEND_TOKEN:
        return "forbidden", 403

    now = datetime.now(JST)

    if now.hour == MORNING_WINDOW.hour:
        return process_notification_window(MORNING_WINDOW, now)
    if now.hour == EVENING_WINDOW.hour:
        return process_notification_window(EVENING_WINDOW, now)

    return f"skip: not notification hour / version={APP_VERSION}", 200


# =========================
# LINE event handlers
# =========================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        forecast = get_precipitation_forecast()
        snapshot = build_snapshot(forecast=forecast)
        message = build_weather_message(snapshot)
    except Exception:
        app.logger.exception("Failed to handle LINE message")
        message = (
            "天気の取得に失敗しました。少ししてからもう一度試してください。\n"
            f"version：{APP_VERSION}"
        )

    reply_text(event.reply_token, message)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
