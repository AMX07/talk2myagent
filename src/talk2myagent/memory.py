"""What the caller knows beyond the call plan, and how the agent looks it up mid-call.

A call plan carries only what someone typed into it. The moment the other side
asks for a delivery date, an account email or what was promised on the last
call, a plan-only agent has to say it does not know. This module is the place
that answer can come from instead.

Retrieval is deliberately local and lexical (BM25 over the stored sentences).
Nothing here reaches the network, and a lookup over a few thousand records
lands in single-digit milliseconds, which is the only reason it can run inside
a live call at all. The turn budget is roughly 0.34 s; anything slower would
have to become a "let me check on that" hold instead.

Records live in ``memory/records.jsonl`` and never enter Git: they are the
customer's private information.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import ROOT, private_dir

TOKEN = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
K1, B = 1.5, 0.75
# A record must answer at least half of what was asked to count as an answer.
COVERAGE = 0.5

# On a corpus this small, function words look statistically rare and would
# otherwise outrank the actual answer ("which card was it paid on" matching a
# delivery date purely on the word "on").
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "can",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "him",
        "his",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "she",
        "that",
        "the",
        "their",
        "them",
        "there",
        "they",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "you",
        "your",
        "do",
        "get",
        "got",
        "give",
        "tell",
        "us",
        "am",
        "been",
        "being",
        "had",
        "having",
    ]
)

# Retrieval here is lexical, so "card" cannot find "Visa" on its own. These are
# the few equivalences a support call actually turns on, expanded on the query
# side only, so nothing is rewritten on the way into the store.
ALIASES: dict[str, set[str]] = {
    "card": {"visa", "mastercard", "amex", "credit", "debit"},
    "payment": {"card", "visa", "refund", "charge"},
    "refund": {"payment", "money", "charge"},
    "dob": {"birth", "born", "birthday"},
    "birthdate": {"birth", "born"},
    "birthday": {"birth", "born"},
    "email": {"mail", "address"},
    "phone": {"telephone", "mobile", "number"},
    "order": {"purchase", "invoice"},
    "delivery": {"deliver", "arrived", "shipped"},
    "promised": {"confirm", "agreed", "said"},
    "promise": {"confirm", "agreed", "said"},
}


def tokenize(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def stem(word: str) -> str:
    """Crude suffix stripping so "delivery", "delivered" and "deliver" agree."""
    if any(character.isdigit() for character in word) or len(word) < 4:
        return word
    for suffix in ("ies", "ing", "ed", "ly", "es", "s", "y"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def terms(text: str) -> list[str]:
    return [stem(word) for word in tokenize(text) if word not in STOPWORDS]


def query_groups(text: str) -> list[set[str]]:
    """One set per thing the query asks about: the term itself plus its aliases.

    Grouping is what makes coverage meaningful. "which card" is satisfied by a
    record saying "Visa", and that still counts as one of the query's ideas
    being found rather than two.
    """
    groups = []
    for word in tokenize(text):
        if word in STOPWORDS:
            continue
        groups.append({stem(word)} | {stem(alias) for alias in ALIASES.get(word, ())})
    return groups


def query_terms(text: str) -> list[str]:
    return sorted({term for group in query_groups(text) for term in group})


@dataclass
class Record:
    text: str
    kind: str = "fact"
    source: str = "note"
    tags: list[str] = field(default_factory=list)
    call_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict:
        return asdict(self)


class Memory:
    """A small, local, append-only memory with BM25 retrieval."""

    def __init__(self, root: Path | None = None):
        folder = private_dir((root or ROOT) / "memory")
        self.path = folder / "records.jsonl"
        self.lock = threading.RLock()
        self.records: list[Record] = []
        self._tokens: list[list[str]] = []
        self._counts: list[Counter] = []
        self._df: Counter = Counter()
        self._load()

    # ------------------------------------------------------------ storage ---

    def _load(self) -> None:
        with self.lock:
            if not self.path.exists():
                return
            for line in self.path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a partial write should not break the whole store
                data.pop("score", None)
                try:
                    self._index(Record(**data), persist=False)
                except TypeError:
                    continue

    def _index(self, record: Record, persist: bool = True) -> Record:
        tokens = terms(record.text)
        self.records.append(record)
        self._tokens.append(tokens)
        self._counts.append(Counter(tokens))
        self._df.update(set(tokens))
        if persist:
            with self.path.open("a") as handle:
                handle.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")
            self.path.chmod(0o600)
        return record

    # ------------------------------------------------------------ writing ---

    def add(
        self,
        text: str,
        kind: str = "fact",
        source: str = "note",
        tags: list[str] | None = None,
        call_id: str | None = None,
    ) -> Record:
        text = " ".join(text.split())
        if not text or len(text) > 2000:
            raise ValueError("A memory needs 1-2000 characters of text.")
        with self.lock:
            existing = next((r for r in self.records if r.text.lower() == text.lower()), None)
            if existing:
                return existing
            return self._index(
                Record(text=text, kind=kind, source=source, tags=tags or [], call_id=call_id)
            )

    def add_facts(self, facts: dict[str, str], source: str = "plan") -> list[Record]:
        """Turn a plan's fact map into sentences retrieval can actually match."""
        out = []
        for key, value in facts.items():
            if not str(value).strip() or key == "unknown":
                continue
            label = key.replace("_", " ")
            out.append(self.add(f"The {label} is {value}.", kind="fact", source=source))
        return out

    def add_call(self, result: dict) -> list[Record]:
        """Record what a finished call established, so the next one can use it."""
        scenario = result.get("scenario") or {}
        plan = result.get("plan") or {}
        company = plan.get("company") or scenario.get("recipient_role") or "the recipient"
        when = (result.get("created_at") or datetime.now(UTC).isoformat())[:10]
        call_id = result.get("call_id")
        out = [
            self.add(
                f"On {when} a call to {company} ended as {result.get('outcome')}. "
                f"{result.get('summary', '')}".strip(),
                kind="call",
                source=f"call {call_id}",
                call_id=call_id,
            )
        ]
        for check in result.get("evaluation") or []:
            if check.get("verdict") == "met":
                out.append(
                    self.add(
                        f"{company} confirmed on {when}: {check.get('explanation')}",
                        kind="promise",
                        source=f"call {call_id}",
                        call_id=call_id,
                    )
                )
        return out

    def forget(self, record_id: str) -> bool:
        """Remove one record. Rewrites the file, so it is rare and deliberate."""
        with self.lock:
            keep = [r for r in self.records if r.id != record_id]
            if len(keep) == len(self.records):
                return False
            self.records, self._tokens, self._counts, self._df = [], [], [], Counter()
            lines = [json.dumps(r.as_dict(), ensure_ascii=False) for r in keep]
            self.path.write_text("\n".join(lines) + ("\n" if lines else ""))
            self.path.chmod(0o600)
            for record in keep:
                self._index(record, persist=False)
            return True

    # ------------------------------------------------------------ reading ---

    def search(self, query: str, limit: int = 3, coverage: float = COVERAGE) -> list[dict]:
        """BM25 ranking, gated on how much of the question a record actually answers.

        An absolute score threshold cannot work here: BM25's idf collapses when a
        term appears in most of a small store, so a perfect one-record match can
        score below a weak match in a large one. Coverage, the share of the
        query's distinct ideas a record contains, is stable at every size, and it
        is what keeps an unrelated row from being served as an answer.
        """
        with self.lock:
            groups = query_groups(query)
            if not groups or not self.records:
                return []
            wanted = {term for group in groups for term in group}
            total = len(self.records)
            average = sum(len(t) for t in self._tokens) / total
            scored = []
            for position, counts in enumerate(self._counts):
                length = len(self._tokens[position]) or 1
                score = 0.0
                for term in set(wanted):
                    frequency = counts.get(term, 0)
                    if not frequency:
                        continue
                    df = self._df.get(term, 0)
                    idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
                    score += (
                        idf
                        * (frequency * (K1 + 1))
                        / (frequency + K1 * (1 - B + B * length / average))
                    )
                if score <= 0:
                    continue
                found = sum(1 for group in groups if any(counts.get(t) for t in group))
                if found / len(groups) >= coverage:
                    scored.append((score, found, position))
            scored.sort(key=lambda row: (row[1], row[0]), reverse=True)
            return [
                {
                    **self.records[position].as_dict(),
                    "score": round(score, 3),
                    "covered": round(found / len(groups), 2),
                }
                for score, found, position in scored[:limit]
            ]

    def recent(self, limit: int = 20) -> list[dict]:
        with self.lock:
            return [r.as_dict() for r in self.records[-limit:][::-1]]

    def stats(self) -> dict:
        with self.lock:
            return {
                "records": len(self.records),
                "kinds": dict(Counter(r.kind for r in self.records)),
                "path": str(self.path),
            }


def summarize(results: list[dict]) -> str:
    """One line the conversation model can read, or a clear statement of nothing."""
    if not results:
        return "Memory has nothing on that."
    return " ".join(r["text"] for r in results)


_shared: dict[str, Memory] = {}
_shared_lock = threading.Lock()


def shared(root: Path | None = None) -> Memory:
    key = str((root or ROOT).resolve())
    with _shared_lock:
        if key not in _shared:
            _shared[key] = Memory(root)
        return _shared[key]


def timed_search(memory: Memory, query: str, limit: int = 3) -> tuple[list[dict], float]:
    started = time.monotonic()
    results = memory.search(query, limit)
    return results, round(time.monotonic() - started, 4)
