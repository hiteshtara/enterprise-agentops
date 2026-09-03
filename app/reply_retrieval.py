"""Finding the historical exchanges worth showing the model.

**Why lexical and not embeddings.** The archive is a few hundred short exchanges
about a dozen recurring subjects -- check-in times, parking, wifi, amenities,
directions. At that size and topical concentration, TF-IDF over the guest
question does most of the work, and the paraphrase gap it leaves is closed by
deterministic topic tags: "can we get in before 3?" shares almost no vocabulary
with "is early check-in possible?", but both tag `early_check_in`.

Embeddings would add an API dependency, a cost per index build, a cache to
invalidate, a column to migrate and a fake to maintain in every test -- to
improve ranking over a corpus small enough to score exhaustively in
milliseconds. That is not a good trade for V1. The seam is here if it changes:
`score_example` is the only place similarity is decided, and a vector score
could be blended in without touching anything else.

Nothing here reaches the model directly. The drafting layer retrieves, labels
the results as precedent, and hands them over.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.historical_replies import HistoricalReplyStore, topics_for

DEFAULT_LIMIT = 4

MAX_LIMIT = 8

# Below this an "example" is noise -- a couple of shared common words. Better to
# return nothing and let the model draft from the rules alone.
MIN_SCORE = 0.12

# How much of the score comes from vocabulary overlap versus topic agreement.
# Topics carry real weight because they survive paraphrase, which is exactly
# where raw token overlap fails.
TOKEN_WEIGHT = 0.65

TOPIC_WEIGHT = 0.35

# A nudge, not a rule. Same-property precedent is usually more relevant, but a
# clearly better example from another property should still win -- so this is
# small enough to be outvoted by a real difference in similarity.
SAME_PROPERTY_BONUS = 0.08

TOKEN_PATTERN = re.compile(r"[a-z0-9']+")

STOPWORDS: frozenset[str] = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "doing",
        "have",
        "has",
        "had",
        "having",
        "i",
        "we",
        "you",
        "he",
        "she",
        "it",
        "they",
        "them",
        "us",
        "our",
        "your",
        "my",
        "me",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "with",
        "about",
        "from",
        "by",
        "as",
        "into",
        "over",
        "under",
        "again",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "then",
        "than",
        "so",
        "very",
        "just",
        "also",
        "too",
        "can",
        "could",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "must",
        "hi",
        "hello",
        "hey",
        "thanks",
        "thank",
        "please",
        "dear",
        "regards",
    ]
)


def tokenise(text: str) -> list[str]:
    """Comparable content words. Stopwords carry no signal at this corpus size."""
    if not isinstance(text, str):
        return []

    return [
        token
        for token in TOKEN_PATTERN.findall(text.lower())
        if len(token) > 1 and token not in STOPWORDS
    ]


def inverse_document_frequency(documents: list[list[str]]) -> dict[str, float]:
    """How rare each token is across the corpus.

    Without this, "check" and "stay" -- which appear in half the archive --
    would dominate every match.
    """
    total = len(documents)

    if total == 0:
        return {}

    appearances: Counter[str] = Counter()

    for tokens in documents:
        appearances.update(set(tokens))

    return {
        token: math.log((total + 1) / (count + 1)) + 1.0
        for token, count in appearances.items()
    }


def weighted_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    counts = Counter(tokens)

    return {
        token: count * idf.get(token, 1.0)
        for token, count in counts.items()
        if token in idf
    }


def cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0

    shared = set(left) & set(right)

    if not shared:
        return 0.0

    dot = sum(left[token] * right[token] for token in shared)

    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))

    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0

    return dot / (left_norm * right_norm)


def topic_overlap(query_topics: set[str], example_topics: set[str]) -> float:
    if not query_topics:
        return 0.0

    return len(query_topics & example_topics) / len(query_topics)


@dataclass(frozen=True)
class RetrievedExample:
    """One precedent, with the score that selected it."""

    guest_example: str
    owner_example: str
    property_slug: str | None
    similarity: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "guest_example": self.guest_example,
            "owner_example": self.owner_example,
            "property_slug": self.property_slug,
            "similarity": round(self.similarity, 4),
        }


def score_example(
    example: dict[str, Any],
    query_vector: dict[str, float],
    query_topics: set[str],
    idf: dict[str, float],
    property_slug: str | None,
) -> float:
    """How relevant one stored example is to the current question.

    The single place similarity is decided. Matching is against the *guest*
    side: we are looking for a question like this one, and the owner's answer is
    what we want to show.
    """
    example_vector = weighted_vector(tokenise(example.get("guest_text", "")), idf)

    tokens = cosine(query_vector, example_vector)
    topics = topic_overlap(query_topics, set(example.get("topics") or []))

    score = TOKEN_WEIGHT * tokens + TOPIC_WEIGHT * topics

    if score <= 0.0:
        return 0.0

    if property_slug and example.get("property_slug") == property_slug:
        score += SAME_PROPERTY_BONUS

    return score


class HistoricalReplyRetriever:
    """Finds precedents for a guest question.

    Ranking, in one sentence: vocabulary overlap and topic agreement decide
    relevance, with a small bonus for the same property. Same-property examples
    therefore win among comparable matches, but a clearly better example from
    another property still outranks a weak local one -- the bonus is smaller
    than any meaningful difference in similarity.

    Reads the corpus per call. At a few hundred rows that is a millisecond of
    SQLite and avoids a cache that could silently serve a stale index.
    """

    def __init__(self, store: HistoricalReplyStore) -> None:
        self._store = store

    def find(
        self,
        guest_message: str,
        property_slug: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[RetrievedExample]:
        """Top matches for one guest question, best first. Empty when none fit."""
        bounded = max(1, min(limit, MAX_LIMIT))

        query_tokens = tokenise(guest_message)

        if not query_tokens:
            return []

        examples = self._store.all_examples()

        if not examples:
            return []

        corpus = [tokenise(example.get("guest_text", "")) for example in examples]

        idf = inverse_document_frequency([*corpus, query_tokens])

        query_vector = weighted_vector(query_tokens, idf)
        query_topics = set(topics_for(guest_message))

        scored = [
            (
                score_example(example, query_vector, query_topics, idf, property_slug),
                example,
            )
            for example in examples
        ]

        relevant = [pair for pair in scored if pair[0] >= MIN_SCORE]

        relevant.sort(key=lambda pair: pair[0], reverse=True)

        return [
            RetrievedExample(
                guest_example=example["guest_text"],
                owner_example=example["owner_text"],
                property_slug=example.get("property_slug"),
                similarity=score,
            )
            for score, example in relevant[:bounded]
        ]
