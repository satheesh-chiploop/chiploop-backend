# utils/audio_utils.py
import os, tempfile
import openai

def transcribe_audio(file_bytes: bytes) -> str:
    """Transcribes raw audio bytes using Whisper API."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    tmp.write(file_bytes)
    tmp.close()

    openai.api_key = os.getenv("OPENAI_API_KEY")
    with open(tmp.name, "rb") as f:
        transcript = openai.Audio.transcriptions.create(model="whisper-1", file=f)
    os.remove(tmp.name)
    return transcript.text
