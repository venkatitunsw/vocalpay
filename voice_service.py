import re

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
