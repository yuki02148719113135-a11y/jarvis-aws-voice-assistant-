"""Nova 2 Sonic への疎通確認。

test.pcm（ヘッダなし 16kHz / 16bit / モノラル）を実時間ペースで送り、
文字起こしと応答テキストを表示し、返ってきた音声を out.wav に保存する。

Nova 2 Sonic はプロンプトに最低1つの音声コンテンツを要求するため、
テキストのみのセッションは ValidationException になる。
入力は 16kHz、出力は 24kHz で、サンプルレートが異なる点に注意。
"""

import asyncio
import base64
import json
import os
import uuid
import wave
from pathlib import Path
from typing import Any

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

MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-2-sonic-v1:0")
REGION = os.getenv("AWS_REGION", "ap-northeast-1")
AUDIO_FILE = Path(os.getenv("AUDIO_FILE", "test.pcm"))
OUTPUT_WAV = Path(os.getenv("OUTPUT_WAV", "out.wav"))

INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000

CHUNK_SIZE = 512
CHUNK_INTERVAL_SECONDS = 0.016  # 実時間の発話ペースを再現する
SILENCE_CHUNKS = 125            # 末尾に約2秒の無音を足して発話終了を伝える
RESPONSE_WAIT_SECONDS = 3

# 認証情報が誤っていても例外が飛ばず無限に待つ仕様のため、必ず上限を切る
TIMEOUT_SECONDS = 90

SYSTEM_PROMPT = "You are a friendly assistant. Keep your responses short, generally one or two sentences."


def event(payload: dict) -> str:
    """イベント JSON を組み立てる。テンプレート文字列だと引用符や日本語で壊れる。"""
    return json.dumps({"event": payload}, ensure_ascii=False)


async def send(stream: Any, event_json: str) -> None:
    await stream.input_stream.send(
        InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=event_json.encode("utf-8"))
        )
    )


def init_events(prompt: str, system_content: str, audio_content: str) -> list[str]:
    """session → prompt → content の順に開くイベント列。"""
    return [
        event({
            "sessionStart": {
                "inferenceConfiguration": {
                    "maxTokens": 1024,
                    "topP": 0.9,
                    "temperature": 0.7,
                }
            }
        }),
        event({
            "promptStart": {
                "promptName": prompt,
                "textOutputConfiguration": {"mediaType": "text/plain"},
                "audioOutputConfiguration": {
                    "mediaType": "audio/lpcm",
                    "sampleRateHertz": OUTPUT_SAMPLE_RATE,
                    "sampleSizeBits": 16,
                    "channelCount": 1,
                    "voiceId": "matthew",
                    "encoding": "base64",
                    "audioType": "SPEECH",
                },
            }
        }),
        event({
            "contentStart": {
                "promptName": prompt,
                "contentName": system_content,
                "type": "TEXT",
                "interactive": True,
                "role": "SYSTEM",
                "textInputConfiguration": {"mediaType": "text/plain"},
            }
        }),
        event({
            "textInput": {
                "promptName": prompt,
                "contentName": system_content,
                "content": SYSTEM_PROMPT,
            }
        }),
        event({
            "contentEnd": {"promptName": prompt, "contentName": system_content}
        }),
        event({
            "contentStart": {
                "promptName": prompt,
                "contentName": audio_content,
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
        }),
    ]


async def publish_audio(stream: Any, prompt: str, content: str) -> None:
    """PCM を 512 バイトずつ、実時間ペースで送る。"""
    try:
        sent = 0
        with AUDIO_FILE.open("rb") as source:
            while chunk := source.read(CHUNK_SIZE):
                sent += 1
                await send(stream, event({
                    "audioInput": {
                        "promptName": prompt,
                        "contentName": content,
                        "content": base64.b64encode(chunk).decode("utf-8"),
                    }
                }))
                await asyncio.sleep(CHUNK_INTERVAL_SECONDS)

        if sent == 0:
            raise RuntimeError(f"音声が読めなかった: {AUDIO_FILE}")
        print(f"[send] {sent} チャンク送信")

        silence = base64.b64encode(bytes(CHUNK_SIZE)).decode("utf-8")
        for _ in range(SILENCE_CHUNKS):
            await send(stream, event({
                "audioInput": {
                    "promptName": prompt,
                    "contentName": content,
                    "content": silence,
                }
            }))
            await asyncio.sleep(CHUNK_INTERVAL_SECONDS)

        await send(stream, event({"contentEnd": {"promptName": prompt, "contentName": content}}))
        await asyncio.sleep(RESPONSE_WAIT_SECONDS)
        await send(stream, event({"promptEnd": {"promptName": prompt}}))
        await send(stream, event({"sessionEnd": {}}))
    finally:
        await stream.input_stream.close()


def write_wav(pcm: bytes) -> None:
    """生の PCM に WAV ヘッダーを付けて保存する。"""
    with wave.open(str(OUTPUT_WAV), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)          # 16bit
        out.setframerate(OUTPUT_SAMPLE_RATE)
        out.writeframes(pcm)

    seconds = len(pcm) / (OUTPUT_SAMPLE_RATE * 2)
    print(f"[save] {OUTPUT_WAV} に保存（{len(pcm):,} バイト / 約 {seconds:.1f} 秒）")


async def receive(stream: Any) -> None:
    _, output = await stream.await_output()
    if output is None:
        raise RuntimeError("出力ストリームが返ってこなかった")

    audio = bytearray()
    chunks = 0

    async for item in output:
        if isinstance(item, InvokeModelWithBidirectionalStreamOutputChunk):
            payload = item.value.bytes_
            if not payload:
                continue
            data = json.loads(payload.decode("utf-8")).get("event", {})

            if "textOutput" in data:
                # 最初のテキストは入力音声の文字起こし、以降がモデルの返答
                print(f"[text] {data['textOutput'].get('content', '')}")

            if "audioOutput" in data:
                content = data["audioOutput"].get("content", "")
                if content:
                    audio.extend(base64.b64decode(content))
                    chunks += 1

            if "completionEnd" in data:
                print(f"[done] 音声チャンク {chunks} 件を受信")
                if audio:
                    write_wav(bytes(audio))
                else:
                    print("[warn] 音声が1件も返ってこなかった")
                return
        elif isinstance(item, InvokeModelWithBidirectionalStreamOutputUnknown):
            raise RuntimeError(f"未知のイベント: {item.tag}")
        else:
            value = getattr(item, "value", None)
            raise RuntimeError(getattr(value, "message", None) or type(item).__name__)


async def run() -> None:
    if not AUDIO_FILE.is_file() or AUDIO_FILE.stat().st_size == 0:
        raise SystemExit(f"音声ファイルがない、または空: {AUDIO_FILE}")

    print(f"model={MODEL_ID} region={REGION} audio={AUDIO_FILE}")

    config = await AsyncBedrockRuntimeConfig.resolve(
        region=REGION,
        transport=AWSCRTHTTPClient(),  # 双方向ストリーミングには CRT が必須
    )

    async with AsyncBedrockRuntimeClient(config=config) as client:
        stream = await client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=MODEL_ID)
        )
        prompt = str(uuid.uuid4())
        audio_content = str(uuid.uuid4())

        async with stream:
            for e in init_events(prompt, str(uuid.uuid4()), audio_content):
                await send(stream, e)

            await asyncio.gather(
                publish_audio(stream, prompt, audio_content),
                receive(stream),
            )


async def main() -> None:
    try:
        await asyncio.wait_for(run(), timeout=TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        print(
            f"\n{TIMEOUT_SECONDS}秒で応答がなかった。"
            "認証情報が誤っている場合、SDKは例外を出さずに待ち続ける。"
            "AWS_PROFILE と ~/.aws のマウントを確認すること。"
        )
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
