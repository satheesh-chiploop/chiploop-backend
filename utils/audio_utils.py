from voice_design import transcribe_audio_bytes

def transcribe_audio(file_bytes: bytes) -> str:
    """Compatibility wrapper for legacy voice endpoints."""
    return transcribe_audio_bytes(file_bytes, filename="voice.webm")
