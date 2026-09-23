"""Nova 2 Sonic に渡すツールの定義と実行。

ツールを足すときはこのファイルだけ触ればよいようにしてある。
TOOL_SPECS に仕様を追加し、HANDLERS に実行関数を登録する。
"""

import asyncio
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable

# zoneinfo は slim イメージだと tzdata が無く失敗することがあるため、固定オフセットで持つ
JST = timezone(timedelta(hours=9), "JST")
WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _schema(properties: dict | None = None, required: list | None = None) -> str:
    """inputSchema は JSON スキーマを「文字列化して」渡す仕様。"""
    return json.dumps({
        "type": "object",
        "properties": properties or {},
        "required": required or [],
    })


TOOL_SPECS: list[dict] = [
    {
        "toolSpec": {
            "name": "getDateTime",
            "description": (
                "現在の日本時間の日付、曜日、時刻を返す。"
                "今日の日付、今日が何曜日か、今何時かを聞かれたときに使う。"
            ),
            "inputSchema": {"json": _schema()},
        }
    },
    {
        "toolSpec": {
            "name": "getWeather",
            "description": (
                "指定した都市の現在の天気と、今後3日間の予報を返す。"
                "天気、気温、雨が降るか、いつ晴れるかなどを聞かれたときに使う。"
            ),
            "inputSchema": {
                "json": _schema(
                    {
                        "city": {
                            "type": "string",
                            "description": "都市名。必ずローマ字の英語表記で渡す（例: Yokohama, Tokyo, Osaka）。",
                        }
                    },
                    ["city"],
                )
            },
        }
    },
]

# モデルは学習時点の日付を「今日」だと思い込み、天気も知らないため、ツールを使うよう明示する。
# 後半は読み上げの指示。放っておくと3日分を箇条書きで小数点まで読み上げる。
TOOL_INSTRUCTIONS = (
    "日付・曜日・時刻に関する質問には、推測で答えず、必ず getDateTime ツールを使ってください。"
    "天気・気温・雨・晴れの見込みに関する質問には、必ず getWeather ツールを使ってください。"
    "都市名が分からない場合は Tokyo を使ってください。"
    "ツールの結果は音声で読み上げられます。要点だけを1〜2文で話してください。"
    "箇条書きや記号は使わず、数値は整数で言ってください。"
    "天気予報は、聞かれない限り今日と明日の傾向だけを伝え、雨の確率が高ければ傘を勧めてください。"
)


def get_date_time(_args: dict) -> dict:
    now = datetime.now(JST)
    return {
        "date": now.strftime("%Y-%m-%d"),
        "weekday": f"{WEEKDAYS_JA[now.weekday()]}曜日",
        "time": now.strftime("%H:%M"),
        "timezone": "Asia/Tokyo",
    }


# ---------------------------------------------------------------
# 天気（Open-Meteo：APIキー不要・無料）
# ---------------------------------------------------------------

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# 実測で TLS ハンドシェイクに約6秒かかる環境があったため、余裕を持たせる
HTTP_TIMEOUT_SECONDS = 15

# WMO 天気コードを日本語にする。モデルに数字を渡すと読み上げで誤訳しやすい
WEATHER_CODES_JA = {
    0: "快晴", 1: "晴れ", 2: "一部曇り", 3: "曇り",
    45: "霧", 48: "霧（着氷）",
    51: "弱い霧雨", 53: "霧雨", 55: "強い霧雨",
    61: "小雨", 63: "雨", 65: "大雨",
    66: "弱い着氷性の雨", 67: "強い着氷性の雨",
    71: "小雪", 73: "雪", 75: "大雪", 77: "霧雪",
    80: "にわか雨", 81: "強いにわか雨", 82: "激しいにわか雨",
    85: "にわか雪", 86: "強いにわか雪",
    95: "雷雨", 96: "雷雨（ひょう）", 99: "激しい雷雨（ひょう）",
}


def _get_json(url: str, params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={"User-Agent": "jarvis-voice-assistant/0.1"},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _describe(code: Any) -> str:
    return WEATHER_CODES_JA.get(code, f"不明（コード {code}）")


def _round(value: Any) -> Any:
    """小数を渡すとモデルが「28.7度」と小数点まで読み上げるため、整数に丸める。"""
    return round(value) if isinstance(value, (int, float)) else value


@lru_cache(maxsize=64)
def _geocode(city: str) -> tuple[float, float, str, str] | None:
    """地名から緯度経度を引く。結果は変わらないのでプロセス内で覚えておく。

    天気の取得は地名検索と予報の2回通信が必要で、それぞれ TLS の確立に
    数秒かかる。2回目以降はこちらを省けるので、待ち時間が半分になる。
    """
    geo = _get_json(GEOCODE_URL, {"name": city, "count": 1, "language": "ja", "format": "json"})
    places = geo.get("results") or []
    if not places:
        return None
    place = places[0]
    return (place["latitude"], place["longitude"], place.get("name", city), place.get("admin1", ""))


def get_weather(args: dict) -> dict:
    city = (args.get("city") or "Tokyo").strip()

    # 1段目：地名から緯度経度を引く（キャッシュあり）
    found = _geocode(city.lower())
    if found is None:
        return {"error": f"都市が見つからない: {city}"}
    latitude, longitude, name, region = found

    # 2段目：緯度経度から天気を引く
    forecast = _get_json(FORECAST_URL, {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,weather_code,wind_speed_10m,precipitation",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "Asia/Tokyo",
        "forecast_days": 2,
    })

    current = forecast.get("current", {})
    daily = forecast.get("daily", {})

    days = []
    for i, date in enumerate(daily.get("time", [])):
        days.append({
            "label": ["今日", "明日", "明後日"][i] if i < 3 else date,
            "date": date,
            "weather": _describe(daily["weather_code"][i]),
            "max_c": _round(daily["temperature_2m_max"][i]),
            "min_c": _round(daily["temperature_2m_min"][i]),
            "rain_probability_pct": daily["precipitation_probability_max"][i],
        })

    return {
        "location": name,
        "region": region,
        "current": {
            "weather": _describe(current.get("weather_code")),
            "temperature_c": _round(current.get("temperature_2m")),
            "wind_kmh": _round(current.get("wind_speed_10m")),
            "precipitation_mm": current.get("precipitation"),
        },
        "forecast": days,
    }


# ツール名の大文字小文字をモデルが揺らすことがあるため、小文字で照合する
HANDLERS: dict[str, Callable[[dict], dict]] = {
    "getdatetime": get_date_time,
    "getweather": get_weather,
}


async def run_tool(name: str, raw_content: str) -> dict[str, Any]:
    """ツールを実行し、モデルに返す dict を作る。失敗しても例外は投げない。"""
    handler = HANDLERS.get(name.lower())
    if handler is None:
        return {"error": f"unknown tool: {name}"}

    try:
        args = json.loads(raw_content) if raw_content else {}
    except json.JSONDecodeError:
        args = {}

    try:
        # HTTP を叩くツールがイベントループを止めないよう、別スレッドで実行する
        return await asyncio.to_thread(handler, args)
    except Exception as exc:
        return {"error": str(exc)}
