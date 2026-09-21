"""ブラウザと Nova 2 Sonic を中継する WebSocket サーバー。

ブラウザ → (16kHz PCM) → このサーバー → Bedrock
Bedrock → (24kHz PCM) → このサーバー → ブラウザ

サーバーは変換をしない。サンプルレートの変換はブラウザ側で完結させる。
"""

import asyncio
import base64
import json
import logging
import os
import uuid
from collections import deque
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from smithy_http.aio.crt import AWSCRTHTTPClient

from aws_sdk_bedrock_runtime.client import AsyncBedrockRuntimeClient
from aws_sdk_bedrock_runtime.config import AsyncBedrockRuntimeConfig
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
    InvokeModelWithBidirectionalStreamOperationInput,
    InvokeModelWithBidirectionalStreamOutputChunk,
    InvokeModelWithBidirectionalStreamOutputUnknown,
)

from tools import TOOL_INSTRUCTIONS, TOOL_SPECS, run_tool

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("jarvis")

MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-2-sonic-v1:0")
REGION = os.getenv("AWS_REGION", "ap-northeast-1")
VOICE_ID = os.getenv("VOICE_ID", "matthew")

INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000

# 直近この件数のテキストを覚えておき、同じ内容が来たら捨てる。
# Nova Sonic は確定前と確定後の両方を textOutput で送ってくるため、
# そのまま流すと同じ発言が二重に並ぶ。stopReason の値に依存しない方法。
RECENT_TEXT_WINDOW = 12

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a helpful voice assistant. Keep responses short, "
    "generally one or two sentences.",
)

app = FastAPI()


def event(payload: dict) -> str:
    return json.dumps({"event": payload}, ensure_ascii=False)


def parse_interrupt(content: str) -> bool:
    """textOutput に紛れてくる {"interrupted": true} を判別する。

    Nova Sonic は割り込みを専用イベントではなく textOutput として送るため、
    中身を見て振り分けないと会話ログに JSON がそのまま並ぶ。
    """
    stripped = content.strip()
    if not stripped.startswith("{"):
        return False
    try:
        return bool(json.loads(stripped).get("interrupted"))
    except json.JSONDecodeError:
        return False


class SonicSession:
    """1本の WebSocket 接続に対応する Nova 2 Sonic セッション。"""

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.prompt = str(uuid.uuid4())
        self.audio_content = str(uuid.uuid4())
        self._closed = False

    async def send(self, event_json: str) -> None:
        if self._closed:
            return
        await self.stream.input_stream.send(
            InvokeModelWithBidirectionalStreamInputChunk(
                value=BidirectionalInputPayloadPart(bytes_=event_json.encode("utf-8"))
            )
        )

    async def start(self) -> None:
        """session → prompt → system → audio content の順に開く。"""
        system_content = str(uuid.uuid4())

        await self.send(event({
            "sessionStart": {
                "inferenceConfiguration": {
                    "maxTokens": 1024,
                    "topP": 0.9,
                    "temperature": 0.7,
                }
            }
        }))
        await self.send(event({
            "promptStart": {
                "promptName": self.prompt,
                "textOutputConfiguration": {"mediaType": "text/plain"},
                # ツールを使うには出力形式の宣言とツール一覧の両方が必要
                "toolUseOutputConfiguration": {"mediaType": "application/json"},
                "toolConfiguration": {"tools": TOOL_SPECS},
                "audioOutputConfiguration": {
                    "mediaType": "audio/lpcm",
                    "sampleRateHertz": OUTPUT_SAMPLE_RATE,
                    "sampleSizeBits": 16,
                    "channelCount": 1,
                    "voiceId": VOICE_ID,
                    "encoding": "base64",
                    "audioType": "SPEECH",
                },
            }
        }))
        await self.send(event({
            "contentStart": {
                "promptName": self.prompt,
                "contentName": system_content,
                "type": "TEXT",
                "interactive": True,
                "role": "SYSTEM",
                "textInputConfiguration": {"mediaType": "text/plain"},
            }
        }))
        await self.send(event({
            "textInput": {
                "promptName": self.prompt,
                "contentName": system_content,
                "content": f"{SYSTEM_PROMPT}\n{TOOL_INSTRUCTIONS}",
            }
        }))
        await self.send(event({
            "contentEnd": {"promptName": self.prompt, "contentName": system_content}
        }))

        # 音声コンテンツは開いたまま維持し、マイクの音を流し込み続ける
        await self.send(event({
            "contentStart": {
                "promptName": self.prompt,
                "contentName": self.audio_content,
                "type": "AUDIO",
                "interactive": True,
                "role": "USER",
                "audioInputConfiguration": {
                    "mediaType": "audio/lpcm",
                    "sampleRateHertz": INPUT_SAMPLE_RATE,
                    "sampleSizeBits": 16,
                    "channelCount": 1,
                    "audioType": "SPEECH",
                    "encoding": "base64",
                },
            }
        }))

    async def send_audio(self, pcm: bytes) -> None:
        await self.send(event({
            "audioInput": {
                "promptName": self.prompt,
                "contentName": self.audio_content,
                "content": base64.b64encode(pcm).decode("utf-8"),
            }
        }))

    async def send_tool_result(self, tool_use_id: str, result: dict) -> None:
        """contentStart → toolResult → contentEnd の3点セットで結果を返す。

        toolUseId が toolUse イベントのものと一致しないと、エラーにならずに
        同じツールが呼ばれ直し続ける。受け取った値をそのまま返すこと。
        """
        content_name = str(uuid.uuid4())

        await self.send(event({
            "contentStart": {
                "promptName": self.prompt,
                "contentName": content_name,
                "interactive": False,
                "type": "TOOL",
                "role": "TOOL",
                "toolResultInputConfiguration": {
                    "toolUseId": tool_use_id,
                    "type": "TEXT",
                    "textInputConfiguration": {"mediaType": "text/plain"},
                },
            }
        }))
        await self.send(event({
            "toolResult": {
                "promptName": self.prompt,
                "contentName": content_name,
                "content": json.dumps(result, ensure_ascii=False),
            }
        }))
        await self.send(event({
            "contentEnd": {"promptName": self.prompt, "contentName": content_name}
        }))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.stream.input_stream.close()
        except Exception:
            pass


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse("static/index.html")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    log.info("client connected")

    config = await AsyncBedrockRuntimeConfig.resolve(
        region=REGION,
        transport=AWSCRTHTTPClient(),
    )

    async with AsyncBedrockRuntimeClient(config=config) as client:
        stream = await client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=MODEL_ID)
        )
        session = SonicSession(stream)

        async with stream:
            await session.start()
            await ws.send_json({"type": "ready"})

            pump = asyncio.create_task(browser_to_bedrock(ws, session))
            drain = asyncio.create_task(bedrock_to_browser(ws, stream, session))

            done, pending = await asyncio.wait(
                {pump, drain}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await session.close()

            for task in done:
                if task.exception():
                    log.error("session ended with error: %s", task.exception())

    log.info("client disconnected")


async def browser_to_bedrock(ws: WebSocket, session: SonicSession) -> None:
    """ブラウザから来た 16kHz PCM をそのまま Bedrock へ流す。"""
    try:
        while True:
            pcm = await ws.receive_bytes()
            await session.send_audio(pcm)
    except WebSocketDisconnect:
        log.info("browser closed the socket")
    except Exception as exc:
        log.error("browser_to_bedrock: %s", exc)


async def bedrock_to_browser(ws: WebSocket, stream: Any, session: SonicSession) -> None:
    """Bedrock の出力をブラウザへ転送する。音声は base64 のまま渡す。"""
    _, output = await stream.await_output()
    if output is None:
        raise RuntimeError("出力ストリームが返ってこなかった")

    recent: deque[str] = deque(maxlen=RECENT_TEXT_WINDOW)
    pending_tool: dict | None = None

    async for item in output:
        if isinstance(item, InvokeModelWithBidirectionalStreamOutputChunk):
            payload = item.value.bytes_
            if not payload:
                continue
            data = json.loads(payload.decode("utf-8")).get("event", {})

            if "textOutput" in data:
                await handle_text(ws, data["textOutput"], recent)

            # toolUse で呼び出し内容を受け取り、contentEnd(TOOL) で実行する。
            # contentEnd が「モデルが結果を待っている」合図になる。
            if "toolUse" in data:
                pending_tool = data["toolUse"]

            if (
                "contentEnd" in data
                and data["contentEnd"].get("type") == "TOOL"
                and pending_tool is not None
            ):
                await handle_tool(ws, session, pending_tool)
                pending_tool = None

            if "audioOutput" in data:
                content = data["audioOutput"].get("content", "")
                if content:
                    await ws.send_json({"type": "audio", "content": content})

            if "contentStart" in data and data["contentStart"].get("type") == "AUDIO":
                await ws.send_json({"type": "speech_start"})

            if "contentEnd" in data and data["contentEnd"].get("type") == "AUDIO":
                await ws.send_json({"type": "speech_end"})

        elif isinstance(item, InvokeModelWithBidirectionalStreamOutputUnknown):
            raise RuntimeError(f"未知のイベント: {item.tag}")
        else:
            value = getattr(item, "value", None)
            raise RuntimeError(getattr(value, "message", None) or type(item).__name__)


async def handle_tool(ws: WebSocket, session: SonicSession, tool_use: dict) -> None:
    name = tool_use.get("toolName", "")
    tool_use_id = tool_use.get("toolUseId", "")
    raw = tool_use.get("content", "")

    result = await run_tool(name, raw)
    log.info("[TOOL] %s(%s) -> %s", name, raw, result)

    await session.send_tool_result(tool_use_id, result)
    await ws.send_json({"type": "tool", "name": name, "result": result})


async def handle_text(ws: WebSocket, text_output: dict, recent: deque) -> None:
    content = text_output.get("content", "")
    role = text_output.get("role", "")

    # 割り込みの通知。会話としては表示せず、再生中の音声を止めさせる
    if parse_interrupt(content):
        log.info("interrupted by user")
        await ws.send_json({"type": "interrupted"})
        return

    normalized = content.strip()
    if not normalized:
        return

    # 同じ内容が再送されてきたら捨てる
    key = f"{role}:{normalized}"
    if key in recent:
        return
    recent.append(key)

    log.info("[%s] %s", role or "text", content)
    await ws.send_json({"type": "text", "role": role, "content": content})


app.mount("/static", StaticFiles(directory="static"), name="static")
