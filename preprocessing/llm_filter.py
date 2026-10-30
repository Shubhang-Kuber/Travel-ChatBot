"""A2: local LLM (Ollama) picks real destinations from spaCy candidates + topic tags per post.

Env: OLLAMA_HOST (e.g. ngrok/Tailscale URL), DEST_MODEL (default qwen2.5:7b), OLLAMA_AUTH ("user:pass", optional).
Usage: python llm_filter.py --limit 10            # first 10 posts, prints side-by-side
       python llm_filter.py                       # full set, resumable via llm_tags.jsonl
"""
import argparse
import json
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

from destination_tagger import DestinationTagger, Option, fold
from topic_tagger import TOPICS, tag_topics

ROOT = Path(__file__).resolve().parents[1]  # dataset/ (raw data lives here)
DATA_PATH = ROOT / "travel_posts.jsonl"
CACHE_PATH = Path(__file__).with_name("llm_tags.jsonl")

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("DEST_MODEL", "qwen2.5:7b")
AUTH = tuple(os.environ["OLLAMA_AUTH"].split(":", 1)) if os.environ.get("OLLAMA_AUTH") else None

PROMPT_VERSION = "v4.1"  # bump when SYSTEM_PROMPT changes -> cached rows from older prompts get redone
TIMEOUT = 180
RETRIES = 3
NUM_CTX = 4096
MAX_TOPICS = 3

TOPIC_HELP = {
    "flights": "flights, airlines, airports, layovers",
    "trains": "trains, rail passes",
    "buses": "intercity buses, coaches",
    "driving": "car rental, road trips, driving rules",
    "local_transport": "metro, taxi, Uber, ferries, getting around a city",
    "accommodation": "hotels, hostels, Airbnb, where to stay",
    "food": "food, restaurants, dishes",
    "nightlife": "bars, clubs, nightlife",
    "sightseeing": "attractions, must-sees, things to do",
    "outdoors": "hiking, beaches, diving, nature",
    "itinerary": "route, how many days, which cities, is plan realistic",
    "budget": "trip costs, cheap vs expensive",
    "money": "currency, cards, ATMs, tipping",
    "visa": "visas, entry rules, passports, customs",
    "safety": "safety, crime, scams",
    "health": "vaccines, illness, travel insurance",
    "weather": "weather, seasons, best time to go",
    "packing": "luggage, gear, what to bring",
    "connectivity": "SIM, eSIM, wifi, phone data",
    "language_culture": "language, etiquette, local norms",
}
assert set(TOPIC_HELP) == set(TOPICS), set(TOPIC_HELP) ^ set(TOPICS)

LETTERS = {"D": "destination", "O": "origin_transit", "X": "other_mention", "W": "wrong_reading"}
BODY_CHARS = 900       # body shown in full up to here; later mentions reach the model via context snippets
CONTEXT_WORDS = 10     # words either side of a candidate's first mention
NUM_PREDICT = 120      # two short lines; grows with candidate count (~5 tokens per label)
BIG_NUM_CTX = 8192     # only for prompts that would overflow NUM_CTX (posts listing 30+ places)

# Static -> first in every request so Ollama reuses its KV cache.
SYSTEM_PROMPT = """You label places in travel-forum posts. You never find places yourself.
You get a post and a numbered list of CANDIDATES from a tagger.
Label every candidate id with exactly one letter:

D = destination. The poster is going to, staying in, travelling through, or asking about it.
    Includes arrival cities ("flying into Madrid"), day trips, overland route stops,
    and where the trip ends. A stopover is D if they plan to stay there.
O = origin or transit. Where they live or start, or only change planes/trains.
X = other mention. Past trips, examples, comparisons, places suggested for others.
    If the post is a general question, poll or opinion with no trip of the poster's own,
    every place is X.
W = wrong reading. The word is not this place here (the bird "turkey",
    the word "nice", a surname), or the other reading of an ambiguous name.
    Never W for a real place the post means.

Rules:
1. Use only the ids given. Never add a place or an id.
2. Label every id once.
3. If several candidates share a name (readings A, B), label at most ONE as D.
   Pick by the nearby words (cities, currency, language). If none fits, label all W.
4. Judge only from the post text and each candidate's context snippet.
5. Topics: pick 1 to 3 from the TOPIC LIST, most important first. Never none: if nothing
   fits well, pick the closest one. Keyword hints are noisy, not evidence.
   Any question about how many days, which cities, route, order or whether a plan
   is feasible includes itinerary.

Output exactly two lines, nothing else:
LABELS: <id><letter> <id><letter> ...
TOPICS: <topic>, <topic>

TOPIC LIST:
""" + "\n".join(f"{t}: {h}" for t, h in TOPIC_HELP.items()) + """

EXAMPLES
TITLE: Can I leave my trip at the layover?
TEXT: We fly Lima to Toronto with a stop in Panama City. We live in Toronto but want to stay in Panama City for a week.
CANDIDATES: 1. Lima | 2. Toronto | 3. Panama City
LABELS: 1O 2O 3D
TOPICS: flights

TITLE: Two weeks in Georgia in May?
TEXT: Planning Tbilisi, Kazbegi and Batumi. Last year we did Vietnam and loved the food. Do I need cash lari?
CANDIDATES: 1. Georgia [A: country] | 2. Georgia [B: US state] | 3. Tbilisi | 4. Kazbegi | 5. Batumi | 6. Vietnam
LABELS: 1D 2W 3D 4D 5D 6X
TOPICS: itinerary, money

TITLE: Nice weekend in Lisbon, what to eat?
TEXT: Going with my parents. A friend says Porto is better but we only have two days.
CANDIDATES: 1. Nice [city, France] | 2. Lisbon | 3. Porto
LABELS: 1W 2D 3X
TOPICS: food

TITLE: Which airport has the best lounges?
TEXT: Changi in Singapore is great, Frankfurt felt old.
CANDIDATES: 1. Singapore | 2. Frankfurt
LABELS: 1X 2X
TOPICS: flights

TITLE: Vaccines for South America?
TEXT: I live in London. Flying into Lima, then overland through Bolivia, ending in Rio. Day trip to Iguazu. Which shots?
CANDIDATES: 1. London | 2. Lima | 3. Bolivia | 4. Rio | 5. Iguazu
LABELS: 1O 2D 3D 4D 5D
TOPICS: health, itinerary"""

_LABEL_RE = re.compile(r"(\d+)\s*([DOXW])\b")


def post_text(title: str, body: str) -> str:
    return f"{title}. {body}" if body else title


def groups_by_mention(options: list[Option]) -> list[list[Option]]:
    """Options sharing a mention = readings of one ambiguous name (only groups of 2+)."""
    groups: dict[str, list[Option]] = {}
    for o in options:
        for m in o.mentions:
            groups.setdefault(fold(m), []).append(o)
    return [g for g in groups.values() if len(g) > 1]


def mention_stats(text: str, mentions: tuple[str, ...]) -> tuple[int, str]:
    """(mention count, '...10 words | first mention | 10 words...') over the full post text."""
    hits = sorted(
        m.span() for name in mentions for m in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE)
    )
    if not hits:
        return 0, ""
    start, end = hits[0]
    before = text[:start].split()[-CONTEXT_WORDS:]
    after = text[end:].split()[:CONTEXT_WORDS]
    return len(hits), " ".join(before + [text[start:end]] + after)


def candidate_line(o: Option, reading: str | None, title: str, text: str) -> str:
    name = " / ".join(o.mentions)
    count, context = mention_stats(text, o.mentions)
    in_title = "yes" if mention_stats(title, o.mentions)[0] else "no"
    tag = f" [{reading}]" if reading else ""
    return f'{o.id}. {name}{tag} | in title: {in_title} | mentions: {count} | context: "...{context}..."'


def build_prompt(title: str, body: str, options: list[Option], keyword_topics: list[str]) -> str:
    readings = {}
    for group in groups_by_mention(options):
        for letter, o in zip("ABCDEFGH", group):
            kind = "country" if len(o.tags) == 1 and fold(o.tags[0]) in map(fold, o.mentions) else ", ".join(o.tags)
            readings.setdefault(o.id, f"{letter}: {kind}")
    text = post_text(title, body)
    cands = "\n".join(candidate_line(o, readings.get(o.id), title, text) for o in options) or "(none)"
    shown = body[:BODY_CHARS] + (" ..." if len(body) > BODY_CHARS else "")
    hits = ", ".join(keyword_topics) if keyword_topics else "none"
    return f"TITLE: {title}\nTEXT: {shown or '(empty)'}\nKEYWORD HINTS (noisy): {hits}\nCANDIDATES:\n{cands}"


def parse_output(content: str, options: list[Option]) -> tuple[dict[int, str], list[str] | None]:
    """'LABELS: 1D 2W' + 'TOPICS: food, money' -> ({1: 'D', 2: 'W'}, ['food', 'money'])."""
    labels_line = re.search(r"LABELS:(.*)", content)
    topics_line = re.search(r"TOPICS:(.*)", content)
    ids = {o.id for o in options}
    labels = {}
    for i, letter in _LABEL_RE.findall(labels_line.group(1) if labels_line else ""):
        if int(i) in ids:
            labels.setdefault(int(i), letter)
    if (options and not labels) or (not options and topics_line is None):
        raise ValueError(f"unparseable output: {content[:200]!r}")
    if topics_line is None:  # cut off after LABELS -> caller falls back to keyword topics
        return labels, None
    topics = [t.strip().lower() for t in topics_line.group(1).split(",")]
    return labels, [t for t in dict.fromkeys(topics) if t in TOPIC_HELP][:MAX_TOPICS]


def call_llm(session: requests.Session, prompt: str, n_options: int = 0) -> str:
    num_predict = max(NUM_PREDICT, 40 + 5 * n_options)
    approx_tokens = (len(SYSTEM_PROMPT) + len(prompt)) // 3 + num_predict
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0, "num_predict": num_predict,
                    "num_ctx": NUM_CTX if approx_tokens <= NUM_CTX else BIG_NUM_CTX},
    }
    if "qwen3" in MODEL:
        payload["think"] = False
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            r = session.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=TIMEOUT)
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code}: {r.text[:200]}")
            return r.json()["message"]["content"]
        except (requests.RequestException, ValueError, KeyError) as e:
            last_err = e
            if attempt < RETRIES:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"{type(last_err).__name__}: {last_err}")


def tag_post(session, post_no, title, body, options, defaults, keyword_topics) -> dict:
    by_id = {o.id: o for o in options}
    t0 = time.time()
    try:
        content = call_llm(session, build_prompt(title, body, options, keyword_topics), len(options))
        labels, topics = parse_output(content, options)
        if topics is None:
            topics = keyword_topics[:MAX_TOPICS]
        # One name = one place: if several readings got a non-W label ("Florida" US = O but "Florida" Uruguay = D),
        # trust the label on spaCy's context-chosen reading (first in group) and mark the rest W.
        for group in groups_by_mention(options):
            if sum(labels.get(o.id, "W") != "W" for o in group) > 1:
                for o in group[1:]:
                    labels[o.id] = "W"
        # Country-only option whose country holds a kept destination is the same trip (Italy + Lombardy, Italy).
        kept_countries = {by_id[i].tags[-1] for i, l in labels.items() if l == "D"}
        for o in options:
            if len(o.tags) == 1 and o.tags[0] in kept_countries:
                labels[o.id] = "D"
        rec = {
            "post_no": post_no,
            "destination_options": [list(by_id[i].tags) for i in by_id if labels.get(i) == "D"],
            "topic_tags": topics,
            "model": MODEL,
            # Audit trail for A3: why each other candidate was dropped (missing = model skipped the id).
            "dropped": [[list(by_id[i].tags), LETTERS.get(labels.get(i), "missing")]
                        for i in by_id if labels.get(i) != "D"],
        }
    except (RuntimeError, ValueError) as e:
        rec = {
            "post_no": post_no,
            "destination_options": [list(t) for t in defaults],
            "topic_tags": keyword_topics[:MAX_TOPICS],
            "model": "fallback",
            "error": str(e)[:300],
        }
    rec["prompt"] = PROMPT_VERSION
    rec["spacy_picks"] = [list(t) for t in defaults]
    rec["keyword_topics"] = keyword_topics
    rec["seconds"] = round(time.time() - t0, 2)
    return rec


def load_cache(path: Path = CACHE_PATH) -> dict[str, dict]:
    """post_no -> last record (later lines win, so retried fallbacks replace old ones)."""
    cache = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    cache[rec["post_no"]] = rec
    return cache


def is_done(rec: dict | None) -> bool:
    return bool(rec) and rec.get("model") != "fallback" and rec.get("prompt") == PROMPT_VERSION


def tag_posts(post_nos, titles, bodies, out: Path = CACHE_PATH, workers: int = 4,
              tagger: DestinationTagger | None = None) -> dict[str, dict]:
    """LLM-tag posts, reusing cached rows in `out`; returns post_no -> record for every given post."""
    bodies = ["" if not isinstance(b, str) else b for b in bodies]
    cache = load_cache(out)
    todo = [i for i, p in enumerate(post_nos) if not is_done(cache.get(p))]
    print(f"[llm_filter] model={MODEL} prompt={PROMPT_VERSION} posts={len(post_nos)} "
          f"cached={len(post_nos) - len(todo)} todo={len(todo)}")
    if todo:
        texts = [post_text(titles[i], bodies[i]) for i in todo]
        cands = (tagger or DestinationTagger()).candidates_many(texts)
        keyword = tag_topics(texts)

        session = requests.Session()
        session.auth = AUTH
        session.headers["ngrok-skip-browser-warning"] = "1"

        n_fail, t0 = 0, time.time()
        with open(out, "a", encoding="utf-8") as f, ThreadPoolExecutor(workers) as pool:
            futures = [
                pool.submit(tag_post, session, post_nos[i], titles[i], bodies[i], opts, defaults, kw)
                for i, (opts, defaults), kw in zip(todo, cands, keyword)
            ]
            for n, fut in enumerate(as_completed(futures), start=1):
                rec = fut.result()  # only the main thread writes -> no lock needed
                cache[rec["post_no"]] = rec
                n_fail += rec["model"] == "fallback"
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                if n % 50 == 0 or n == len(futures):
                    el = time.time() - t0
                    print(f"[llm_filter] {n}/{len(futures)} done, {el / n:.2f} s/post wall, fallbacks={n_fail}, "
                          f"eta {el / n * (len(futures) - n) / 60:.1f} min")
    return {p: cache[p] for p in post_nos}


def summarize(recs: list[dict]) -> dict:
    """Run statistics: speed, fallbacks, destination keep/drop vs spaCy, topic use vs keyword hits."""
    n = len(recs)
    llm = [r for r in recs if r["model"] != "fallback"]
    n_cands = [len(r["destination_options"]) + len(r.get("dropped", [])) for r in llm]
    with_cands = [r for r, c in zip(llm, n_cands) if c]
    roles = Counter(role for r in llm for _, role in r.get("dropped", []))
    roles["destination"] = sum(len(r["destination_options"]) for r in llm)

    same = removed = added = 0
    for r in llm:
        kept = {tuple(t) for t in r["destination_options"]}
        picks = {tuple(t) for t in r.get("spacy_picks", [])}
        same += kept == picks
        removed += len(picks - kept)
        added += len(kept - picks)

    topic_counts = Counter(t for r in llm for t in r["topic_tags"])
    kw_first = [r for r in llm if r.get("keyword_topics")]
    agree = sum(r["topic_tags"][:1] == r["keyword_topics"][:1] for r in kw_first if r["topic_tags"])
    llm_only = Counter(t for r in llm for t in r["topic_tags"] if t not in r.get("keyword_topics", []))
    countries = Counter(t[-1] for r in llm for t in r["destination_options"])

    def pct(a, b): return round(100 * a / b, 1) if b else 0.0

    return {
        "posts": n,
        "llm_ok": len(llm),
        "fallbacks": n - len(llm),
        "fallback_errors": [r.get("error", "") for r in recs if r["model"] == "fallback"][:5],
        "model": MODEL,
        "prompt": PROMPT_VERSION,
        "mean_request_seconds": round(sum(r["seconds"] for r in recs) / n, 2) if n else 0,
        "destinations": {
            "posts_with_0_candidates": len(llm) - len(with_cands),
            "mean_candidates_per_post": round(sum(n_cands) / len(llm), 2) if llm else 0,
            "mean_kept_per_post": round(roles["destination"] / len(llm), 2) if llm else 0,
            "posts_with_candidates_but_0_kept": sum(not r["destination_options"] for r in with_cands),
            "pct_posts_with_candidates_but_0_kept": pct(sum(not r["destination_options"] for r in with_cands), len(with_cands)),
            "labels": dict(roles.most_common()),
            "posts_llm_equals_spacy_picks": same,
            "spacy_picks_removed_by_llm": removed,
            "llm_kept_not_in_spacy_picks": added,
            "top_countries": dict(countries.most_common(15)),
        },
        "topics": {
            "mean_topics_per_post": round(sum(map(len, (r["topic_tags"] for r in llm))) / len(llm), 2) if llm else 0,
            "posts_with_0_topics": sum(not r["topic_tags"] for r in llm),
            "posts_with_0_keyword_hits": sum(not r.get("keyword_topics") for r in llm),
            "pct_top_topic_equals_top_keyword_hit": pct(agree, len(kw_first)),
            "llm_topics_not_in_keyword_hits": dict(llm_only.most_common()),
            "distribution": dict(topic_counts.most_common()),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="only first N posts")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", type=Path, default=CACHE_PATH)
    ap.add_argument("--show", action="store_true", help="print per-post results (default on with --limit)")
    args = ap.parse_args()

    df = pd.read_json(DATA_PATH, lines=True, nrows=args.limit)
    post_nos = [f"p_{i:05d}" for i in range(len(df))]
    recs = tag_posts(post_nos, df["title"].tolist(), df["selftext"].tolist(), args.out, args.workers)

    if args.show or args.limit:
        for (p, rec), title in zip(recs.items(), df["title"]):
            print(f"\n{p} ({rec['seconds']}s, {rec['model']}): {title[:100]}")
            print(f"  spaCy picks : {rec.get('spacy_picks')}")
            print(f"  LLM dests   : {rec['destination_options']}")
            print(f"  LLM dropped : {rec.get('dropped', [])}")
            print(f"  keyword hits: {rec.get('keyword_topics', [])[:6]}")
            print(f"  LLM topics  : {rec['topic_tags']}")
            if "error" in rec:
                print(f"  error       : {rec['error']}")
    print(json.dumps(summarize(list(recs.values())), indent=2))


if __name__ == "__main__":
    main()
