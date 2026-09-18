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

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("jarvis")

MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-2-sonic-v1:0")
REGION = os.getenv("AWS_REGION", "ap-northeast-1")
VOICE_ID = os.getenv("VOICE_ID", "matthew")

INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a helpful voice assistant. Keep responses short, "
    "generally one or two sentences.",
)

app = FastAPI()


def event(payload: dict) -> str:
    return json.dumps({"event": payload}, ensure_ascii=False)


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
                "content": SYSTEM_PROMPT,
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
            drain = asyncio.create_task(bedrock_to_browser(ws, stream))

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


async def bedrock_to_browser(ws: WebSocket, stream: Any) -> None:
    """Bedrock の出力をブラウザへ転送する。音声は base64 のまま渡す。"""
    _, output = await stream.await_output()
    if output is None:
        raise RuntimeError("出力ストリームが返ってこなかった")

    async for item in output:
        if isinstance(item, InvokeModelWithBidirectionalStreamOutputChunk):
            payload = item.value.bytes_
            if not payload:
                continue
            data = json.loads(payload.decode("utf-8")).get("event", {})

            if "textOutput" in data:
                text = data["textOutput"].get("content", "")
                role = data["textOutput"].get("role", "")
                log.info("[%s] %s", role or "text", text)
                await ws.send_json({"type": "text", "role": role, "content": text})

            if "audioOutput" in data:
                content = data["audioOutput"].get("content", "")
                if content:
                    await ws.send_json({"type": "audio", "content": content})

            # モデルが話し始めたらブラウザ側の再生をリセットさせる
            if "contentStart" in data and data["contentStart"].get("type") == "AUDIO":
                await ws.send_json({"type": "speech_start"})

            if "contentEnd" in data and data["contentEnd"].get("type") == "AUDIO":
                await ws.send_json({"type": "speech_end"})

        elif isinstance(item, InvokeModelWithBidirectionalStreamOutputUnknown):
            raise RuntimeError(f"未知のイベント: {item.tag}")
        else:
            value = getattr(item, "value", None)
            raise RuntimeError(getattr(value, "message", None) or type(item).__name__)


app.mount("/static", StaticFiles(directory="static"), name="static")
