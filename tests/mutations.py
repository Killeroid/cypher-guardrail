"""Queries that are made from the queries of the corpus by breaking them a little: a character lost, a token swapped, a
quote or a bracket added, a number turned into a zero. Most are invalid, some are valid, and which is which is for
FalkorDB to say (see ``test_oracle.py``)."""

import random
import re

_TOKENS = re.compile(r"\s+|\w+|[^\w\s]")
_ADDED = ["'", '"', "(", ")", "[", "}", "/ 0", "% 0", "/ (1-1)", "/ 0.0", "* 2", "."]
_CLAUSES = [" RETURN ", " WITH ", " MATCH ", " WHERE ", " DELETE ", " SET ", " LIMIT "]
_ZEROS = ["0", "(1-1)", "0.0", "-1"]


def mutate(query: str, rng: random.Random) -> str:
    """``query`` with one thing changed (the same query when the change has nothing to work on)."""
    tokens = _TOKENS.findall(query)
    char, token = rng.randrange(len(query)), rng.randrange(len(tokens))
    swapped = [*tokens]
    if token + 1 < len(tokens):
        swapped[token], swapped[token + 1] = tokens[token + 1], tokens[token]
    changes = {
        "drop a character": query[:char] + query[char + 1 :],
        "add a character": query[:char] + rng.choice(_ADDED) + query[char:],
        "drop a token": "".join(tokens[:token] + tokens[token + 1 :]),
        "repeat a token": "".join(tokens[: token + 1] + tokens[token:]),
        "swap two tokens": "".join(swapped),
        "make a number a zero": re.sub(
            r"\d+", lambda _: rng.choice(_ZEROS), query, count=1
        ),
        "start a clause": "".join(tokens[:token])
        + rng.choice(_CLAUSES)
        + "".join(tokens[token:]),
    }
    return changes[rng.choice(list(changes))]
