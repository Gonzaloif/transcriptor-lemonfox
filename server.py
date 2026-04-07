#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Transcriptor Lemonfox — Servidor web
Para uso local: python server.py
Para Railway/producción: se usa PORT del entorno
"""

import os
import json
import time
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory

import requests as req_lib

APP_DIR = Path(__file__).parent.resolve()

app = Flask(__name__, static_folder=str(APP_DIR / "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB

LEMONFOX_URL = "https://api.lemonfox.ai/v1/audio/transcriptions"
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".webm", ".mp4", ".ogg", ".flac", ".aac", ".mpga"}


@app.route("/")
def index():
    return send_from_directory(str(APP_DIR / "static"), "index.html")


@app.route("/api/transcribe", methods=["POST"])
def transcribe():
    # API key comes from the client (stored in their browser's localStorage)
    api_key = request.form.get("api_key", "").strip()
    if not api_key:
        return jsonify({"error": "Falta la API key. Configúrala en la app."}), 400

    audio = request.files.get("file")
    if not audio:
        return jsonify({"error": "No se recibió archivo de audio."}), 400

    ext = "." + audio.filename.rsplit(".", 1)[-1].lower() if "." in audio.filename else ""
    if ext not in AUDIO_EXTS:
        return jsonify({"error": f"Formato no soportado: {ext}"}), 400

    language = request.form.get("language", "spanish")
    speaker_labels = request.form.get("speaker_labels", "false") == "true"
    response_format = "verbose_json" if speaker_labels else "text"

    files = {
        "file": (audio.filename, audio.stream, audio.content_type or "application/octet-stream"),
    }
    data = {"response_format": response_format}
    if language:
        data["language"] = language
    if speaker_labels:
        data["speaker_labels"] = "true"

    headers = {"Authorization": f"Bearer {api_key}"}

    max_attempts = 3
    resp = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = req_lib.post(
                LEMONFOX_URL,
                files=files,
                data=data,
                headers=headers,
                timeout=(15, 900),
            )
        except req_lib.exceptions.Timeout:
            return jsonify({"error": "Timeout — el audio puede ser muy largo o la conexión lenta."}), 504
        except req_lib.exceptions.ConnectionError:
            return jsonify({"error": "Error de conexión con Lemonfox. Revisa tu internet."}), 502

        if resp.status_code == 200:
            break

        if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
            time.sleep(2 ** attempt)
            audio.stream.seek(0)
            files = {
                "file": (audio.filename, audio.stream, audio.content_type or "application/octet-stream"),
            }
            continue

        try:
            body = resp.json()
            msg = body.get("error", {}).get("message", "") or json.dumps(body, ensure_ascii=False)
        except Exception:
            msg = resp.text[:500]
        return jsonify({"error": f"Error HTTP {resp.status_code}: {msg}"}), resp.status_code

    if resp is None:
        return jsonify({"error": "No se pudo contactar a Lemonfox."}), 502

    # Parse response
    if response_format == "text":
        text = resp.text.strip()
    else:
        try:
            rdata = resp.json()
        except Exception:
            return jsonify({"error": "Respuesta inesperada de Lemonfox (no JSON)."}), 500
        segments = rdata.get("segments") or rdata.get("data") or []
        lines = []
        for seg in segments:
            spk = seg.get("speaker") or seg.get("speaker_label") or ""
            txt = (seg.get("text") or "").strip()
            if not txt:
                continue
            prefix = f"[{spk}] " if spk else ""
            lines.append(prefix + txt)
        text = "\n".join(lines) if lines else (rdata.get("text") or "").strip()

    return jsonify({"text": text})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"\n{'='*50}")
    print(f"  🎧 Transcriptor Lemonfox")
    print(f"  http://localhost:{port}")
    print(f"{'='*50}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
