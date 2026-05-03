import os
import tempfile
from typing import Iterable

from openai import OpenAI


class VoiceDesignConfigError(RuntimeError):
    pass


def transcribe_audio_bytes(audio_bytes: bytes, *, filename: str = "voice.webm") -> str:
    if not audio_bytes:
        raise ValueError("empty_audio")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise VoiceDesignConfigError("OPENAI_API_KEY_missing")

    suffix = os.path.splitext(filename or "")[1] or ".webm"
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        client = OpenAI(api_key=api_key)
        with open(tmp_path, "rb") as handle:
            transcript = client.audio.transcriptions.create(model="whisper-1", file=handle)
        return str(getattr(transcript, "text", "") or "").strip()
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


async def build_spec_draft(transcripts: Iterable[str], *, loop_type: str = "digital", target: str = "Arch2RTL") -> str:
    from utils.llm_utils import run_llm_fallback

    cleaned = [item.strip() for item in transcripts if item and item.strip()]
    if not cleaned:
        raise ValueError("transcripts_required")

    joined = "\n\n".join(f"Voice note {idx + 1}:\n{text}" for idx, text in enumerate(cleaned))
    prompt = f"""You are helping create a concise chip design specification for ChipLoop.

Target workflow: {target}
Loop: {loop_type}

Convert these voice notes into a structured, reviewable engineering spec.
Do not invent unexplained requirements. If information is missing, add a short "Questions / assumptions" section.
Use plain text with sections:
- Goal
- Top module or block name
- Interfaces
- Behavior
- Constraints
- Verification intent
- Deliverables
- Questions / assumptions

Voice notes:
{joined}
"""
    draft = await run_llm_fallback(prompt)
    return (draft or joined).strip()
