"""B1-B3: posts JSON -> chunks JSONL (post / summary / comment chunks) for embedding + MongoDB.

Each chunk inherits the post's destination_tags + topic_tags (LLM, Phase A) and gets its own keyword
topic_mentions, so a "take the night train" comment in an accommodation thread still matches train questions.
embed_text starts with the post title so short comments keep their context.

Usage: python build_chunks.py --posts posts_100.json --out chunks_100.jsonl
       python build_chunks.py                                   # mongo/mongo_data/posts.json -> chunks.jsonl
"""
import argparse
import json
import re
import statistics
from collections import Counter
from pathlib import Path

from topic_tagger import tag_topics

ROOT = Path(__file__).resolve().parents[1]  # dataset/ (raw data lives here)
MONGO_DATA = ROOT / "mongo" / "mongo_data"  # files loaded into MongoDB

MAX_CHARS = 1000   # ~250 tokens per chunk
SPLIT_OVER = 1500  # post body / single comment longer than this gets split
EMBED_LABEL = {"post": None, "summary": "Summary", "comment": "Comment"}
MIN_SUMMARY_CHARS = 40

# AutoModerator text; split_comments cuts the bot comment into several pieces at commas.
BOT_MARKERS = re.compile(
    r"I am a bot|I'?m a bot|performed automatically|contact the moderators|subredditmessagecompose"
    r"|redditors had to say in the weekly destination thread|post should be approved shortly",
    re.IGNORECASE,
)
BOT_HEAD = 8  # bot comment comes first: leading pieces up to the last marker piece in here are all bot text

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def clean_comments(comments: list[str]) -> list[str]:
    marked = [i for i, c in enumerate(comments[:BOT_HEAD]) if BOT_MARKERS.search(c)]
    start = marked[-1] + 1 if marked else 0
    return [c for c in comments[start:] if not BOT_MARKERS.search(c)]


def clean_summary(summary: str) -> str:
    """Drop bot sentences ('... I am a bot . Please contact the moderators ...'); '' if little is left."""
    sentences = _SENTENCE_SPLIT.split(summary.replace(" .", "."))
    kept = " ".join(s for s in sentences if not BOT_MARKERS.search(s)).strip()
    return kept if len(kept) >= MIN_SUMMARY_CHARS else ""


def _word_windows(text: str) -> list[str]:
    """Fallback for unpunctuated walls of text: ~MAX_CHARS windows on word boundaries."""
    pieces, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > MAX_CHARS:
            pieces.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    return pieces + [cur] if cur else pieces


def split_long(text: str) -> list[str]:
    """Text over SPLIT_OVER chars -> ~MAX_CHARS pieces on sentence boundaries (word windows if no punctuation)."""
    text = text.strip()
    if len(text) <= SPLIT_OVER:
        return [text] if text else []
    sentences = [p for s in _SENTENCE_SPLIT.split(text) for p in (_word_windows(s) if len(s) > SPLIT_OVER else [s])]
    pieces, cur = [], ""
    for s in sentences:
        if cur and len(cur) + 1 + len(s) > MAX_CHARS:
            pieces.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}" if cur else s
    return pieces + [cur] if cur else pieces


def pack_comments(comments: list[str]) -> list[str]:
    """Consecutive comment pieces greedily packed into chunks of <= ~MAX_CHARS. Joined with ', ' because
    split_comments also cut inside comments at ', ' -> this restores the original text."""
    chunks, cur = [], ""
    for piece in (p for c in comments for p in split_long(c)):
        if cur and len(cur) + 2 + len(piece) > MAX_CHARS:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur}, {piece}" if cur else piece
    return chunks + [cur] if cur else chunks


def post_chunks(post: dict) -> list[dict]:
    title = post["title"]
    pieces = {
        "post": split_long(post.get("body") or "") or [title],
        "summary": split_long(clean_summary(post.get("summary") or "")),
        "comment": pack_comments(clean_comments(post.get("comments") or [])),
    }
    chunks = []
    for ctype, texts in pieces.items():
        for seq, text in enumerate(texts):
            label = EMBED_LABEL[ctype]
            chunks.append({
                "chunk_id": f"{post['post_no']}_{ctype[0]}{seq:03d}",
                "post_no": post["post_no"],
                "type": ctype,
                "seq": seq,
                "text": text,
                "embed_text": f"Post: {title}\n{label + ': ' if label else ''}{text}",
                "destination_tags": post["destination_tags"],
                "topic_tags": post["topic_tags"],
            })
    return chunks


def build(posts: list[dict]) -> list[dict]:
    chunks = [c for post in posts for c in post_chunks(post)]
    for c, topics in zip(chunks, tag_topics([c["text"] for c in chunks])):
        c["topic_mentions"] = topics
    return chunks


def print_stats(posts: list[dict], chunks: list[dict]):
    by_type = Counter(c["type"] for c in chunks)
    lens = [len(c["text"]) for c in chunks]
    comments = [c for c in chunks if c["type"] == "comment"]
    new_topic = sum(bool(set(c["topic_mentions"]) - set(c["topic_tags"])) for c in comments)
    print(f"posts={len(posts)} chunks={len(chunks)} ({len(chunks) / len(posts):.1f}/post) by type={dict(by_type)}")
    print(f"chunk chars: median={statistics.median(lens):.0f} mean={statistics.mean(lens):.0f} max={max(lens)}")
    print(f"comment chunks with a topic_mention not in post topic_tags: {new_topic}/{len(comments)}")
    print(f"chunks with no topic_mentions: {sum(not c['topic_mentions'] for c in chunks)}")
    print(f"posts with no destination_tags: {sum(not p['destination_tags'] for p in posts)}")
    n_pieces = sum(len(p["comments"]) for p in posts)
    n_bot = n_pieces - sum(len(clean_comments(p["comments"])) for p in posts)
    print(f"bot comment pieces removed: {n_bot}/{n_pieces}; summaries dropped as bot-only: "
          f"{sum(bool(p['summary']) and not clean_summary(p['summary']) for p in posts)}")
    print(f"chunks still containing bot markers: {sum(bool(BOT_MARKERS.search(c['text'])) for c in chunks)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--posts", type=Path, default=MONGO_DATA / "posts.json")
    ap.add_argument("--out", type=Path, default=MONGO_DATA / "chunks.jsonl")
    args = ap.parse_args()

    posts = json.loads(args.posts.read_text(encoding="utf-8"))
    chunks = build(posts)
    with open(args.out, "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print_stats(posts, chunks)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
