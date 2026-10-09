import re

# Converts common Australian/English financial amount slang to plain digits
# *before* intent_parser's AMOUNT_RE (which only ever matches digits) runs,
# rather than teaching that regex slang on top of everything else it does.
# Deliberately narrow: only amount words with no other plausible reading in
# a payment command ("a fiver", "two grand") -- not general number-word
# parsing ("twenty"), which is a much bigger, much more ambiguous problem
# (is "Pay twenty to Jo" twenty dollars, or someone named "Twenty"?) that
# doesn't belong bolted onto this narrow, deterministic pass.
_UNIT_SLANG = {
    "fiver": 5, "fivers": 5,
    "tenner": 10, "tenners": 10,
    "buck": 1, "bucks": 1,
}
_MULTIPLIER_SLANG = {
    "grand": 1000,
    "ton": 100, "tons": 100,
}
_ALL_SLANG = {**_UNIT_SLANG, **_MULTIPLIER_SLANG}
_SLANG_WORDS_RE = "|".join(sorted(_ALL_SLANG, key=len, reverse=True))

# "<digit> grand" / "<digit> ton" -- a count of the multiplier unit, so the
# digit must be read out, not discarded. Has to run before the bare-word
# pass below, or "5 grand" would lose the 5 and become just "1000".
_MULTIPLIER_WITH_COUNT_RE = re.compile(
    rf"\b(?P<n>\d+(?:\.\d+)?)\s*(?P<unit>{'|'.join(_MULTIPLIER_SLANG)})\b", re.IGNORECASE
)
# "<digit> bucks" ("20 bucks") -- here the word is just naming the currency,
# same as "20 dollars"; the digit already is the amount. Also has to run
# before the bare-word pass, which would otherwise read the word on its own
# terms and turn "20 bucks" into "20 1" (two amounts instead of one).
_DIGIT_PREFIXED_UNIT_RE = re.compile(
    rf"\b(?P<n>\d+(?:\.\d+)?)\s*(?:{'|'.join(_UNIT_SLANG)})\b", re.IGNORECASE
)
# Any remaining slang word on its own -- "a fiver", "a grand", or (common in
# voice transcription, where "a" gets dropped) just "fiver"/"grand" alone.
# "a"/"an" is matched together with its trailing space as one unit (not a
# bare `\s*`), so when there's no article the match starts exactly at the
# slang word -- otherwise the preceding space gets swallowed into the
# match and the substitution glues the digit onto the word before it
# ("Pay Sam fiver" -> "Pay Sam5" instead of "Pay Sam 5").
_BARE_SLANG_RE = re.compile(rf"\b(?:(?:a|an)\s+)?({_SLANG_WORDS_RE})\b", re.IGNORECASE)


def normalize_financial_slang(text: str) -> str:
    """Returns `text` with amount slang replaced by plain digits. Idempotent
    and a no-op on text that contains none -- safe to always run."""
    if not text:
        return text

    def _multiplier_sub(m: re.Match) -> str:
        n = float(m.group("n"))
        value = n * _MULTIPLIER_SLANG[m.group("unit").lower()]
        return f"{value:g}"

    text = _MULTIPLIER_WITH_COUNT_RE.sub(_multiplier_sub, text)
    text = _DIGIT_PREFIXED_UNIT_RE.sub(lambda m: m.group("n"), text)

    def _bare_sub(m: re.Match) -> str:
        return str(_ALL_SLANG[m.group(1).lower()])

    return _BARE_SLANG_RE.sub(_bare_sub, text)
