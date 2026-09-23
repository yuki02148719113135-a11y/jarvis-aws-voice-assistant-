"""会話履歴を DynamoDB に置くための薄い層。

タスクの中に履歴を持たないことで、再接続で別タスクに繋がっても
会話が続く。ALB のスティッキーセッションはこのために不要になる。

テーブル（persistent 層で作成）
    session_id (S, hash) / created_at (N, range) / expires_at (TTL)
"""

import asyncio
import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger("jarvis")

TABLE_NAME = os.getenv("CONVERSATIONS_TABLE", "jarvis-conversations")
REGION = os.getenv("AWS_REGION", "ap-northeast-1")

# 履歴の保持期間。TTL で自動的に消えるので、自分で消す処理は要らない。
TTL_DAYS = int(os.getenv("HISTORY_TTL_DAYS", "7"))

# 復元する件数。多いほど文脈は繋がるが、セッション開始が遅くなる。
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))

_table: Any | None = None


def _get_table() -> Any:
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    return _table


def _put(session_id: str, role: str, content: str) -> None:
    now_ms = int(time.time() * 1000)
    _get_table().put_item(Item={
        "session_id": session_id,
        "created_at": now_ms,
        "role": role,
        "content": content,
        "expires_at": int(time.time()) + TTL_DAYS * 86400,
    })


def _query(session_id: str, limit: int) -> list[dict]:
    # 新しい順に limit 件取り、古い順に並べ直して返す
    res = _get_table().query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("session_id").eq(session_id),
        ScanIndexForward=False,
        Limit=limit,
    )
    items = sorted(res.get("Items", []), key=lambda i: i["created_at"])
    return [{"role": i["role"], "content": i["content"]} for i in items]


async def save_turn(session_id: str, role: str, content: str) -> None:
    """1発言を記録する。失敗しても会話は止めない。"""
    if not session_id or not content.strip():
        return
    try:
        await asyncio.to_thread(_put, session_id, role, content)
    except (BotoCoreError, ClientError) as exc:
        log.warning("履歴の保存に失敗: %s", exc)


async def load_history(session_id: str, limit: int = HISTORY_LIMIT) -> list[dict]:
    """直近の発言を古い順で返す。失敗したら空で返し、新規会話として続ける。"""
    if not session_id:
        return []
    try:
        return await asyncio.to_thread(_query, session_id, limit)
    except (BotoCoreError, ClientError) as exc:
        log.warning("履歴の読み込みに失敗: %s", exc)
        return []
