"""Does a reworded answer contain any number that wasn't in the original?"""
import re

PERIOD = re.compile(r"\b20\d\d-(?:Q[1-4]|\d{2})\b")
NUMBER = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")


def numbers_in(text):
    periods = set(PERIOD.findall(text))
    rest = PERIOD.sub(" ", text)
    return periods | {n.replace(",", "") for n in NUMBER.findall(rest)}


def unverified(rewrite, original):
    return sorted(numbers_in(rewrite) - numbers_in(original))


original = "A Margarita cost $3.40 to make in 2025-Q3 and $3.51 in 2026-Q2 (+3.1%)."

honest = "Your Margarita now costs $3.51 to make, up from $3.40 in 2025-Q3 (+3.1%)."
sloppy = "Your Margarita now costs about $3.50 to make, up 3% since 2025-Q3."

print("honest:", unverified(honest, original))
print("sloppy:", unverified(sloppy, original))