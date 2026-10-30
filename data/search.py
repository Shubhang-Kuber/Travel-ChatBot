"""Hybrid retrieval over the Atlas `chunks` collection (plan B4-B6): $vectorSearch + $search, client-side RRF,
grouped by post. Entry point for the agent: search_stored_tips(query, destination).

Env: MONGODB_URI = Atlas connection string (not MONGO_URI: that one is the backend's local Mongo),
     TIPS_DB (default "travel"), plus EMBED_HOST / EMBED_MODEL / EMBED_AUTH for the query embedding
     (see preprocessing/embed_chunks.py; must be the same model the chunks were embedded with).
Usage: python -m data.search "japan rail pass worth it?" --dest Japan
       python -m data.search "what about food?" --dest "Barcelona, Spain" --k 3 --json
"""
import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bson.binary import Binary, BinaryVectorDtype
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessing"))
from embed_chunks import embed_query  # noqa: E402
from topic_tagger import tag_topic  # noqa: E402

log = logging.getLogger(__name__)

DB_NAME = os.environ.get("TIPS_DB", "travel")
VECTOR_INDEX = "chunks_vector"
TEXT_INDEX = "chunks_text"

TOP_POSTS = 5
CHUNKS_PER_POST = 3
SEARCH_LIMIT = 60  # chunks per search list before fusion
NUM_CANDIDATES = 600  # vector ANN candidates; high because destination filters are selective
MIN_POSTS = 5  # fewer posts with the specific (state) filter -> widen to the country
RRF_K = 60
# Post score = best chunk + OTHER_CHUNK_WEIGHT * next chunks + topic bonuses (RRF scale: 1/61 ~ 0.016 per list).
OTHER_CHUNK_WEIGHT = 0.2
POST_TOPIC_BONUS = 0.004  # post topic_tags (LLM) overlap query topics
CHUNK_TOPIC_BONUS = 0.002  # chunk topic_mentions (keywords) overlap query topics
LOW_QUALITY_DOT = 0.2  # title_query_comments_dot below this -> junk thread, down-ranked
LOW_QUALITY_FACTOR = 0.5

CHUNK_FIELDS = {"_id": 0, "chunk_id": 1, "post_no": 1, "type": 1, "seq": 1, "text": 1, "topic_mentions": 1}

_lock = threading.Lock()
_db = None
_aliases: dict[str, str] | None = None
_tagger = None  # False once loading failed


def get_db():
    global _db
    with _lock:
        if _db is None:
            uri = os.environ.get("MONGODB_URI")
            if not uri:
                raise RuntimeError("set MONGODB_URI to the Atlas connection string holding the travel chunks")
            _db = MongoClient(uri, appname="travel-chatbot-search")[DB_NAME]
        return _db


# --------------------------------------------------------------------------- destination -> filter keys


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s|,]", " ", text.casefold())).strip()


def _load_aliases() -> dict[str, str]:
    """Every destination key in the data -> lookup names. "Catalonia|Spain" answers to "catalonia|spain",
    "catalonia, spain" and (unless it is also a country or shared by two countries) "catalonia"."""
    global _aliases
    with _lock:
        if _aliases is not None:
            return _aliases
    keys = get_db().posts.distinct("destination_tags")
    aliases, bare_states = {}, defaultdict(set)
    for key in keys:
        aliases[_norm(key)] = key
        if "|" in key:
            state, country = key.split("|", 1)
            aliases[_norm(f"{state}, {country}")] = key
            bare_states[_norm(state)].add(key)
    for name, state_keys in bare_states.items():
        if name not in aliases and len(state_keys) == 1:
            aliases[name] = next(iter(state_keys))
    with _lock:
        _aliases = aliases
    return aliases


def _tagger_keys(text: str) -> list[str]:
    """Cities / landmarks ("Kyoto", "Machu Picchu") via the spaCy tagger; optional (needs en_core_web_md)."""
    global _tagger
    if _tagger is None:
        try:
            from destination_tagger import DestinationTagger
            _tagger = DestinationTagger()
        except (ImportError, OSError) as e:
            log.warning("destination tagger unavailable (%s); city names only reach search as text", e)
            _tagger = False
    return _tagger.tag_keys(text) if _tagger else []


def _narrow(keys: list[str]) -> list[str]:
    """Drop a country key when a state key of that country is present (["Catalonia|Spain", "Spain"] -> state)."""
    countries_with_state = {k.split("|", 1)[1] for k in keys if "|" in k}
    return [k for k in dict.fromkeys(keys) if k not in countries_with_state]


def _widen(keys: list[str]) -> list[str]:
    return list(dict.fromkeys(k.split("|", 1)[1] if "|" in k else k for k in keys))


def resolve_destination(destination: str | None) -> list[str]:
    """Free-text destination -> most specific filter keys. "Spain" -> ["Spain"], "Catalonia, Spain" ->
    ["Catalonia|Spain"], "Spain and Portugal" -> ["Spain", "Portugal"], "Kyoto" -> ["Kyoto|Japan"]."""
    if not destination or not _norm(destination):
        return []
    aliases = _load_aliases()
    if (key := aliases.get(_norm(destination))) is not None:
        return [key]
    parts = [p for p in map(_norm, re.split(r",|/|&|;|\band\b|\bor\b|\bvs\b", destination, flags=re.I)) if p]
    found = [aliases.get(p) for p in parts]
    if found and all(found):
        return _narrow(found)
    tagged = [k for k in _tagger_keys(destination) if k in aliases.values()]
    return _narrow(tagged or [k for k in found if k])


# --------------------------------------------------------------------------- searches


def _dest_filter(keys: list[str]) -> dict:
    return {"destination_tags": {"$in": keys}} if keys else {}


def _vector_search(query_vector: list[float], keys: list[str]) -> list[dict]:
    stage = {
        "index": VECTOR_INDEX,
        "path": "embedding",
        "queryVector": Binary.from_vector(query_vector, BinaryVectorDtype.FLOAT32),
        "numCandidates": NUM_CANDIDATES,
        "limit": SEARCH_LIMIT,
    }
    if keys:
        stage["filter"] = _dest_filter(keys)
    pipeline = [{"$vectorSearch": stage}, {"$project": CHUNK_FIELDS}]
    return list(get_db().chunks.aggregate(pipeline))


def _text_search(text: str, keys: list[str], topics: list[str]) -> list[dict]:
    """BM25 on chunk text (exact names: "Hoi An", "JR pass"); destination = filter, topics = soft boost."""
    compound = {"must": [{"text": {"query": text, "path": "text"}}]}
    if keys:
        compound["filter"] = [{"in": {"path": "destination_tags", "value": keys}}]
    if topics:
        compound["should"] = [
            {"in": {"path": "topic_tags", "value": topics}},
            {"in": {"path": "topic_mentions", "value": topics}},
        ]
    pipeline = [{"$search": {"index": TEXT_INDEX, "compound": compound}}, {"$limit": SEARCH_LIMIT},
                {"$project": CHUNK_FIELDS}]
    return list(get_db().chunks.aggregate(pipeline))


def _rrf(*ranked_lists: list[dict]) -> dict[str, tuple[dict, float]]:
    """chunk_id -> (chunk, reciprocal-rank-fusion score)."""
    fused: dict[str, tuple[dict, float]] = {}
    for ranked in ranked_lists:
        for rank, chunk in enumerate(ranked, start=1):
            prev = fused.get(chunk["chunk_id"], (chunk, 0.0))
            fused[chunk["chunk_id"]] = (prev[0], prev[1] + 1 / (RRF_K + rank))
    return fused


def _group_posts(fused: dict[str, tuple[dict, float]], topics: list[str], posts: dict[str, dict]) -> list[dict]:
    """B5: post score = best chunk + small share of its other chunks + topic bonus, junk threads down-ranked."""
    query_topics = set(topics)
    by_post: dict[str, list[tuple[dict, float]]] = defaultdict(list)
    for chunk, score in fused.values():
        if query_topics & set(chunk.get("topic_mentions", [])):
            score += CHUNK_TOPIC_BONUS
        by_post[chunk["post_no"]].append((chunk, score))

    ranked = []
    for post_no, scored in by_post.items():
        post = posts.get(post_no)
        if post is None:
            continue
        scored.sort(key=lambda cs: -cs[1])
        score = scored[0][1] + OTHER_CHUNK_WEIGHT * sum(s for _, s in scored[1:CHUNKS_PER_POST])
        if query_topics & set(post.get("topic_tags", [])):
            score += POST_TOPIC_BONUS
        dot = post.get("title_query_comments_dot")
        if dot is not None and dot < LOW_QUALITY_DOT:
            score *= LOW_QUALITY_FACTOR
        ranked.append({
            "post_no": post_no,
            "title": post.get("title", ""),
            "destination_tags": post.get("destination_tags", []),
            "topic_tags": post.get("topic_tags", []),
            "score": round(score, 5),
            # Best chunks back in thread order, so the excerpt reads like the post.
            "chunks": [{"type": c["type"], "text": c["text"]}
                       for c, _ in sorted(scored[:CHUNKS_PER_POST], key=lambda cs: (cs[0]["type"] != "post", cs[0]["seq"]))],
        })
    ranked.sort(key=lambda p: -p["score"])
    return ranked


def _query_text(query: str, destination: str | None) -> str:
    """Short follow-ups ("what about food?") embed badly; prefix the destination unless already named."""
    if destination and destination.casefold() not in query.casefold():
        return f"{destination} {query}"
    return query


def search(query: str, destination: str | None = None, k: int = TOP_POSTS,
           exclude_post_nos=(), debug: dict | None = None) -> list[dict]:
    """Top-k posts for a question, each with its best-matching chunks. `exclude_post_nos` = posts already shown
    this chat (B6b); they only come back if too few others match. Pass a dict as `debug` to get timings/keys."""
    t0 = time.perf_counter()
    text = _query_text(query, destination)
    topics = tag_topic(query)
    keys = resolve_destination(destination)
    attempts = [keys] + ([_widen(keys)] if _widen(keys) != keys else [])

    query_vector = None
    with ThreadPoolExecutor(2) as pool:
        vec_future = pool.submit(embed_query, text)
        for filter_keys in attempts:
            text_future = pool.submit(_text_search, text, filter_keys, topics)
            if vec_future is not None:
                try:
                    query_vector = vec_future.result()
                except RuntimeError as e:  # embedding host down -> keyword search alone still answers
                    log.warning("query embedding failed, text search only: %s", e)
                vec_future = None
            vector_hits = _vector_search(query_vector, filter_keys) if query_vector else []
            fused = _rrf(vector_hits, text_future.result())
            if len({c["post_no"] for c, _ in fused.values()}) >= MIN_POSTS:
                break

    post_nos = list({c["post_no"] for c, _ in fused.values()})
    posts = {p["post_no"]: p for p in get_db().posts.find(
        {"post_no": {"$in": post_nos}},
        {"_id": 0, "post_no": 1, "title": 1, "destination_tags": 1, "topic_tags": 1, "title_query_comments_dot": 1},
    )}
    ranked = _group_posts(fused, topics, posts)
    excluded = set(exclude_post_nos)
    fresh = [p for p in ranked if p["post_no"] not in excluded]
    results = (fresh + [p for p in ranked if p["post_no"] in excluded])[:k]

    if debug is not None:
        debug.update(query_text=text, topics=topics, keys=keys, filter_keys=filter_keys,
                     vector=query_vector is not None, chunks=len(fused), posts=len(ranked),
                     seconds=round(time.perf_counter() - t0, 2))
    return results


def search_stored_tips(query: str, destination: str | None = None) -> list[dict]:
    """Agent contract (agent/graph.py retrieve_node)."""
    return search(query, destination)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--dest", default=None)
    ap.add_argument("--k", type=int, default=TOP_POSTS)
    ap.add_argument("--exclude", nargs="*", default=[], help="post_nos already shown")
    ap.add_argument("--json", action="store_true", help="print the raw result list")
    args = ap.parse_args()

    debug = {}
    results = search(args.query, args.dest, k=args.k, exclude_post_nos=args.exclude, debug=debug)
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return
    print(" | ".join(f"{k}={v}" for k, v in debug.items()))
    for i, p in enumerate(results, start=1):
        print(f"\n{i}. [{p['score']}] {p['post_no']} {p['title']}")
        print(f"   dest={p['destination_tags']} topics={p['topic_tags']}")
        for c in p["chunks"]:
            print(f"   - {c['type']}: {c['text'][:200].replace(chr(10), ' ')}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
