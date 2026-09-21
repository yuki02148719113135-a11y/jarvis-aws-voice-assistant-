"""Nova 2 Sonic に渡すツールの定義と実行。

ツールを足すときはこのファイルだけ触ればよいようにしてある。
TOOL_SPECS に仕様を追加し、HANDLERS に実行関数を登録する。
"""

import json
from datetime import datetime, timedelta, timezone
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
]

# モデルは学習時点の日付を「今日」だと思い込むため、ツールを使うよう明示する
TOOL_INSTRUCTIONS = (
    "日付・曜日・時刻に関する質問には、推測で答えず、必ず getDateTime ツールを使ってください。"
)


def get_date_time(_args: dict) -> dict:
    now = datetime.now(JST)
    return {
        "date": now.strftime("%Y-%m-%d"),
        "weekday": f"{WEEKDAYS_JA[now.weekday()]}曜日",
        "time": now.strftime("%H:%M"),
        "timezone": "Asia/Tokyo",
    }


# ツール名の大文字小文字をモデルが揺らすことがあるため、小文字で照合する
HANDLERS: dict[str, Callable[[dict], dict]] = {
    "getdatetime": get_date_time,
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
        return handler(args)
    except Exception as exc:
        return {"error": str(exc)}
