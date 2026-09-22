"""
Measure the assistant, not demo it.

Two evaluations:

1. Planning - does English turn into the right plan? Scored on answerable
   questions, questions that must be refused, and questions the semantic
   layer must stop.

2. Number grounding - does the checker catch a rewrite that invents a figure,
   without rejecting honest rewrites? Tested with hand-written good and bad
   rewrites, so it runs with no API key.

    python -m evals.run_evals
    python -m evals.run_evals --planner gemini
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from askinv.answer import ask, verify_numbers
from askinv.layer import SemanticError, SemanticLayer
from askinv.planner import Vocabulary, plan

HERE = Path(__file__).resolve().parent


def _norm_filters(fs):
    out = {}
    for f in fs or []:
        v = f["value"]
        out[f["dimension"]] = sorted(v) if isinstance(v, list) else v
    return out


def _want_filters(d):
    return {k: sorted(v) if isinstance(v, list) else v for k, v in (d or {}).items()}


def score_planning(planner="rules", verbose=True, suite_name="questions"):
    suite = yaml.safe_load((HERE / f"{suite_name}.yml").read_text(encoding="utf-8"))
    if not any(suite.get(k) for k in ("answer", "refuse", "guard")):
        print(f"{suite_name}.yml is empty - see the note at the top of the file.")
        return {}, {}
    sl = SemanticLayer()
    vocab = Vocabulary(sl)
    misses, ok = [], {"answer": 0, "refuse": 0, "guard": 0}

    for case in suite.get("answer") or []:
        try:
            p = plan(case["q"], sl, planner, vocab)
        except SemanticError as e:
            misses.append((case["q"], f"refused: {str(e)[:70]}"))
            continue
        want_intent = case["intent"]
        good = p["intent"] == want_intent
        if good and want_intent == "query":
            q = p["query"]
            good = (set(q.get("metrics", [])) == set(case["metrics"])
                    and set(q.get("dimensions", [])) == set(case.get("dimensions", []))
                    and _norm_filters(q.get("filters")) == _want_filters(case.get("filters")))
        elif good:
            good = (p.get("before") == str(case["before"]) and p.get("after") == str(case["after"])
                    and p.get("drink", None) == case.get("drink", None)
                    and _norm_filters(p.get("filters")) == _want_filters(case.get("filters")))
        if good:
            ok["answer"] += 1
        else:
            misses.append((case["q"], f"got {p}"))

    for kind in ("refuse", "guard"):
        for case in suite.get(kind) or []:
            a = ask(case["q"], sl, planner, vocab)
            if a.refused:
                ok[kind] += 1
            else:
                misses.append((case["q"], f"answered, should have been stopped: {a.text[:80]}"))

    n = {k: len(suite.get(k) or []) for k in ok}
    labels = {"answer": "answerable, right plan  ", "refuse": "out of scope, refused   ",
              "guard": "unsafe, stopped by layer "}
    if verbose:
        print(f"planner                 {planner}")
        for k, label in labels.items():
            if n[k]:                      # skip empty sections instead of printing 0/1
                print(f"{label}{ok[k]}/{n[k]}  ({ok[k]/n[k]:.0%})")
        for q, why in misses:
            print(f"  miss: {q}\n        {why}")
    return ok, n


GROUNDING = [
    # draft, rewrite, should_pass
    ("A Margarita cost $3.40 to make in 2025-Q3 and $3.51 in 2026-Q2 (+3.1%).",
     "Your Margarita now costs $3.51 to make, up from $3.40 in 2025-Q3 (+3.1%).", True),
    ("A Margarita cost $3.40 to make in 2025-Q3 and $3.51 in 2026-Q2 (+3.1%).",
     "Your Margarita now costs about $3.50 to make, up 3% since 2025-Q3.", False),
    ("Spend with Ember went from $9,931 in 2026-Q1 to $11,012 in 2026-Q2 (+$1,081, +10.9%).",
     "Ember spend rose $1,081 (+10.9%) to $11,012 in 2026-Q2.", True),
    ("Spend with Ember went from $9,931 in 2026-Q1 to $11,012 in 2026-Q2 (+$1,081, +10.9%).",
     "Ember spend rose roughly 11% - about $1,100 - to $11,012.", False),
    ("Pour cost %: Coffee from 19.6% (2025-Q3) to 22.1% (2026-Q2).",
     "Coffee pour cost climbed from 19.6% to 22.1% between 2025-Q3 and 2026-Q2.", True),
    ("Pour cost %: Coffee from 19.6% (2025-Q3) to 22.1% (2026-Q2).",
     "Coffee pour cost climbed 2.5 points to 22.1% in 2026-Q2.", False),
    ("Cost per serve for Espresso Martini: $3.37.",
     "An Espresso Martini costs $3.37 to make, which leaves a healthy 85% margin.", False),
    ("A Margarita cost $3.40 to make in 2025-Q3 and $3.51 in 2026-Q2 (+3.1%).",
     "The Margarita is up 3% since 2025-Q3.", False),
]


def score_grounding(verbose=True):
    right = 0
    for draft, rewrite, should_pass in GROUNDING:
        passed = not verify_numbers(rewrite, draft)
        right += passed == should_pass
        if verbose and passed != should_pass:
            print(f"  grounding miss: {rewrite}")
    caught = sum(1 for d, r, s in GROUNDING if not s and verify_numbers(r, d))
    bad = sum(1 for _d, _r, s in GROUNDING if not s)
    kept = sum(1 for d, r, s in GROUNDING if s and not verify_numbers(r, d))
    good = sum(1 for _d, _r, s in GROUNDING if s)
    if verbose:
        print(f"invented numbers caught {caught}/{bad}")
        print(f"honest rewrites kept    {kept}/{good}")
    return right, len(GROUNDING)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--planner", default="rules", choices=["rules", "gemini"])
    ap.add_argument("--suite", default="questions", choices=["questions", "heldout"])
    a = ap.parse_args()
    score_planning(a.planner, suite_name=a.suite)
    print()
    score_grounding()
