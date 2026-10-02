import os
import re

# CPU int8 inference, per the blueprint's Tier 2 spec. Defaults to "tiny.en"
# rather than "small.en" -- much smaller first-run download (~75MB vs
# ~250MB) while still being a real, working local transcription path; set
# WHISPER_MODEL_SIZE to "small.en" (or any faster-whisper model name) for
# better accuracy once that's downloaded.
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "tiny.en")

_model = None

_DIGIT_WORDS = {
    "oh": "0", "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_DIGIT_WORD_RUN_RE = re.compile(
    r"(?:\b(?:oh|zero|one|two|three|four|five|six|seven|eight|nine)\b[\s,]*){3,}",
    re.IGNORECASE,
)


def normalize_spoken_numbers(text: str) -> str:
    """
    Whisper transcribes a spoken phone number/PayID as digit *words* far
    more often than digits ("oh four one two three four five six seven
    eight" rather than "0412345678") -- without converting those back to
    digits, a read-aloud PayID never matches the intent parser's PayID
    pattern. Only runs of 3+ consecutive digit-words are converted (a lone
    "one" or "two" in an ordinary sentence is left alone).
    """
    def _convert_run(match: re.Match) -> str:
        words = re.findall(r"[a-zA-Z]+", match.group(0))
        return "".join(_DIGIT_WORDS[w.lower()] for w in words)

    return _DIGIT_WORD_RUN_RE.sub(_convert_run, text)


def _get_model():
    """
    Loads the model once per process -- faster-whisper downloads weights to
    a local cache on first use (needs network access that one time) and
    keeps them on disk after that. Lazy so the whole app doesn't pay this
    cost (or need the dependency available) unless voice transcription is
    actually used.
    """
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    return _model


def transcribe_audio_bytes(audio_bytes: bytes, suffix: str = ".wav") -> str:
    """
    Transcribes raw audio bytes (any format ffmpeg/faster-whisper can read --
    wav, webm, mp3...) to text. Writes to a temp file because faster-whisper
    (via ctranslate2) reads from a path/file-like, not an in-memory buffer.
    """
    import tempfile

    model = _get_model()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes)
        temp_path = f.name

    try:
        segments, _info = model.transcribe(temp_path, language="en")
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return normalize_spoken_numbers(text)
    finally:
        os.unlink(temp_path)
