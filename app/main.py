import os

from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "jarvis", "model": os.getenv("BEDROCK_MODEL_ID", "unset")}
