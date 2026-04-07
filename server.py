#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Transcriptor Lemonfox — Servidor web
API key se lee de la variable de entorno LEMONFOX_API_KEY
Soporta archivos de audio y URLs de YouTube (via cobalt.tools)
"""

import os
import json
import time
import tempfile
import re
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
import requests as req_lib

APP_DIR = Path(__file__).parent.resolve()

app = Flask(__name__, static_folder=str(APP_DIR / "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

LEMONFOX_URL = "https://api.lemonfox.ai/v1/audio/transcriptions"
COBALT_URL = "https://api.cobalt.tools"
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".webm", ".mp4", ".ogg", ".flac", ".aac", ".mpga", ".opus"}


def get_api_key():
    return os.environ.get("LEMONFOX_API_KEY", "").strip()


def send_to_lemonfox(audio_path, filename, language, speaker_labels):
    """Send an audio file to Lemonfox and return transcription text."""
    api_key = get_api_key()
    if not api_key:
        return None, "API key no configurada en el servidor."

    response_format = "verbose_json" if speaker_labels else "text"
    mime_map = {
        ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
        ".webm": "audio/webm", ".mp4": "audio/mp4", ".ogg": "audio/ogg",
        ".flac": "audio/flac", ".aac": "audio/aac", ".opus": "audio/opus",
    }
    ext = Path(filename).suffix.lower()
    mime = mime_map.get(ext, "application/octet-stream")

    with open(audio_path, "rb") as f:
        data = {"response_format": response_format}
        if language:
            data["language"] = language
        if speaker_labels:
            data["speaker_labels"] = "true"
        headers = {"Authorization": f"Bearer {api_key}"}

        max_attempts = 3
        resp = None
        for attempt in range(1, max_attempts + 1):
            f.seek(0)
            files = {"file": (filename, f, mime)}
            try:
                resp = req_lib.post(
                    LEMONFOX_URL, files=files, data=data,
                    headers=headers, timeout=(15, 900),
                )
            except req_lib.exceptions.Timeout:
                return None, "Timeout — el audio puede ser muy largo."
            except req_lib.exceptions.ConnectionError:
                return None, "Error de conexión con Lemonfox."

            if resp.status_code == 200:
                break
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                time.sleep(2 ** attempt)
                continue

            try:
                body = resp.json()
                msg = body.get("error", {}).get("message", "") or json.dumps(body, ensure_ascii=False)
            except Exception:
                msg = resp.text[:500]
            return None, f"Error HTTP {resp.status_code}: {msg}"

        if resp is None:
            return None, "No se pudo contactar a Lemonfox."

    if response_format == "text":
        return resp.text.strip(), None
    else:
        try:
            rdata = resp.json()
        except Exception:
            return None, "Respuesta inesperada de Lemonfox."
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
        return text, None


def download_youtube_audio(url):
    """Download audio from YouTube using cobalt.tools API. Returns (file_path, title, error)."""
    try:
        r = req_lib.post(
            COBALT_URL,
            json={
                "url": url,
                "audioFormat": "mp3",
                "isAudioOnly": True,
                "filenameStyle": "basic",
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
    except Exception as e:
        return None, None, f"Error contactando cobalt: {str(e)[:200]}"

    if r.status_code != 200:
        try:
            err = r.json()
            msg = err.get("error", {}).get("code", "") or json.dumps(err, ensure_ascii=False)
        except Exception:
            msg = r.text[:300]
        return None, None, f"Cobalt error: {msg}"

    data = r.json()
    status = data.get("status", "")

    # cobalt returns a URL to download the audio
    download_url = None
    title = "youtube_audio"

    if status == "redirect" or status == "stream":
        download_url = data.get("url", "")
    elif status == "tunnel":
        download_url = data.get("url", "")
    elif status == "picker":
        # Multiple options, pick first audio
        picker = data.get("picker", [])
        if picker:
            download_url = picker[0].get("url", "")
    elif status == "error":
        err_code = data.get("error", {}).get("code", "unknown")
        return None, None, f"Cobalt no pudo procesar el video: {err_code}"
    else:
        return None, None, f"Respuesta inesperada de cobalt: {status}"

    if not download_url:
        return None, None, "No se obtuvo URL de descarga de cobalt."

    # Download the actual audio file
    try:
        audio_resp = req_lib.get(download_url, timeout=300, stream=True)
        if audio_resp.status_code != 200:
            return None, None, f"Error descargando audio: HTTP {audio_resp.status_code}"
    except Exception as e:
        return None, None, f"Error descargando audio: {str(e)[:200]}"

    # Save to temp file
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    for chunk in audio_resp.iter_content(chunk_size=8192):
        tmp.write(chunk)
    tmp.close()

    # Try to extract title from content-disposition or use default
    cd = audio_resp.headers.get("content-disposition", "")
    if "filename=" in cd:
        try:
            title = cd.split("filename=")[-1].strip('"').rsplit(".", 1)[0]
        except Exception:
            pass

    return tmp.name, title, None


@app.route("/")
def index():
    return send_from_directory(str(APP_DIR / "static"), "index.html")


@app.route("/api/status", methods=["GET"])
def status():
    return jsonify({"ready": bool(get_api_key())})


@app.route("/api/transcribe", methods=["POST"])
def transcribe():
    audio = request.files.get("file")
    if not audio:
        return jsonify({"error": "No se recibió archivo de audio."}), 400

    ext = "." + audio.filename.rsplit(".", 1)[-1].lower() if "." in audio.filename else ""
    if ext not in AUDIO_EXTS:
        return jsonify({"error": f"Formato no soportado: {ext}"}), 400

    language = request.form.get("language", "spanish")
    speaker_labels = request.form.get("speaker_labels", "false") == "true"

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        audio.save(tmp)
        tmp_path = tmp.name

    try:
        text, error = send_to_lemonfox(tmp_path, audio.filename, language, speaker_labels)
    finally:
        os.unlink(tmp_path)

    if error:
        return jsonify({"error": error}), 500
    return jsonify({"text": text})


@app.route("/api/transcribe-youtube", methods=["POST"])
def transcribe_youtube():
    data = request.json or {}
    url = data.get("url", "").strip()
    language = data.get("language", "spanish")
    speaker_labels = data.get("speaker_labels", False)

    if not url:
        return jsonify({"error": "No se proporcionó URL."}), 400

    yt_pattern = r'(youtube\.com/watch|youtu\.be/|youtube\.com/shorts/)'
    if not re.search(yt_pattern, url):
        return jsonify({"error": "URL no parece ser de YouTube."}), 400

    # Download via cobalt
    audio_path, title, dl_error = download_youtube_audio(url)
    if dl_error:
        return jsonify({"error": dl_error}), 400

    try:
        safe_title = re.sub(r'[^\w\s-]', '', title or "youtube_audio")[:80].strip()
        filename = f"{safe_title}.mp3"
        text, tx_error = send_to_lemonfox(audio_path, filename, language, speaker_labels)
    finally:
        if audio_path and os.path.exists(audio_path):
            os.unlink(audio_path)

    if tx_error:
        return jsonify({"error": tx_error}), 500
    return jsonify({"text": text, "title": title or "YouTube video"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"\n  Transcriptor Lemonfox — http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
