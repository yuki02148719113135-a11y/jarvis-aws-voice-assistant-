"""Cognito が発行した ID トークンの検証。

ブラウザは Hosted UI でログインし、受け取ったトークンを
WebSocket 接続時にクエリで渡す。サーバーはここで署名と中身を確かめる。

COGNITO_USER_POOL_ID が未設定なら検証をスキップする（ローカル開発用）。
本番で未設定のまま起動しないよう、起動時にログへ出す。
"""

import json
import logging
import os
import time
import urllib.request
from typing import Any

from jose import jwt
from jose.exceptions import JWTError

log = logging.getLogger("jarvis")

USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID", "")
CLIENT_ID = os.getenv("COGNITO_CLIENT_ID", "")
REGION = os.getenv("AWS_REGION", "ap-northeast-1")

ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"

_jwks: dict[str, Any] | None = None
_jwks_at: float = 0.0
# 鍵は滅多に変わらないが、ローテーションに備えて1時間で取り直す。
_JWKS_TTL = 3600


class AuthError(Exception):
    pass


def enabled() -> bool:
    return bool(USER_POOL_ID and CLIENT_ID)


def _get_jwks() -> dict[str, Any]:
    global _jwks, _jwks_at
    if _jwks is None or time.time() - _jwks_at > _JWKS_TTL:
        with urllib.request.urlopen(f"{ISSUER}/.well-known/jwks.json", timeout=5) as res:
            _jwks = json.load(res)
        _jwks_at = time.time()
    return _jwks


def verify(token: str) -> dict[str, Any]:
    """検証に通ればクレームを返す。通らなければ AuthError。"""
    if not enabled():
        return {}
    if not token:
        raise AuthError("トークンがありません")

    try:
        # 署名・有効期限・発行者・宛先をまとめて検証する。
        # audience を渡さないと、別アプリ向けのトークンでも通ってしまう。
        claims = jwt.decode(
            token,
            _get_jwks(),
            algorithms=["RS256"],
            audience=CLIENT_ID,
            issuer=ISSUER,
            # ID トークンには at_hash（アクセストークンの指紋）が入るが、
            # 突き合わせる相手を送っていないため検証しない。
            # 署名・有効期限・発行者・宛先は引き続き検証される。
            options={"verify_at_hash": False},
        )
    except JWTError as exc:
        raise AuthError(f"トークンが不正です: {exc}") from exc

    if claims.get("token_use") != "id":
        raise AuthError("ID トークンではありません")
    return claims


def subject(claims: dict[str, Any]) -> str:
    """会話履歴のキーに使う、ユーザーを一意に表す値。"""
    return claims.get("sub", "")
