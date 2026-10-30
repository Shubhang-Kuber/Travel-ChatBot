"""Load mongo_data/ into MongoDB Atlas: `posts` + `chunks` (with float32 BinData vectors) + search indexes.

Env: MONGODB_URI (or MONGO_URI) = Atlas connection string, MONGO_DB (default "travel").
Usage: python load_mongo.py              # upsert everything (safe to rerun) + create indexes
       python load_mongo.py --drop       # drop both collections first, then load
Fits Atlas M0 (~360 MB of 512 MB): embed_text is not stored, vectors are float32 BinData (not double arrays).
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from bson.binary import Binary, BinaryVectorDtype
from pymongo import MongoClient, ReplaceOne
from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

DATA_DIR = Path(__file__).with_name("mongo_data")
POSTS_PATH = DATA_DIR / "posts.json"
CHUNKS_PATH = DATA_DIR / "chunks.jsonl"
EMBEDDINGS_PATH = DATA_DIR / "embeddings_nomic-embed-text.jsonl"

DB_NAME = os.environ.get("MONGO_DB", "travel")
BATCH = 500
DIMS = 768

VECTOR_INDEX = "chunks_vector"
TEXT_INDEX = "chunks_text"
# Filter fields must be declared here to be usable in $vectorSearch pre-filters.
VECTOR_DEFINITION = {
    "fields": [
        {"type": "vector", "path": "embedding", "numDimensions": DIMS, "similarity": "cosine"},
        {"type": "filter", "path": "destination_tags"},
        {"type": "filter", "path": "topic_tags"},
        {"type": "filter", "path": "topic_mentions"},
        {"type": "filter", "path": "type"},
        {"type": "filter", "path": "post_no"},
    ]
}
# BM25 keyword search on chunk text; tag fields as token so $search can filter/boost on exact values.
TEXT_DEFINITION = {
    "mappings": {
        "dynamic": False,
        "fields": {
            "text": {"type": "string", "analyzer": "lucene.english"},
            "destination_tags": {"type": "token"},
            "topic_tags": {"type": "token"},
            "topic_mentions": {"type": "token"},
            "type": {"type": "token"},
        },
    }
}


def bulk_upsert(coll, docs, label: str):
    ops, n, t0 = [], 0, time.time()
    for doc in docs:
        ops.append(ReplaceOne({"_id": doc["_id"]}, doc, upsert=True))
        if len(ops) == BATCH:
            coll.bulk_write(ops, ordered=False)
            n += len(ops)
            ops = []
            if n % 10_000 == 0:
                print(f"  {label}: {n} ({n / (time.time() - t0):.0f} docs/s)")
    if ops:
        coll.bulk_write(ops, ordered=False)
        n += len(ops)
    print(f"  {label}: {n} upserted in {time.time() - t0:.0f}s")


def post_docs():
    for p in json.loads(POSTS_PATH.read_text(encoding="utf-8")):
        yield {"_id": p["post_no"], **p}


def load_embeddings() -> dict[str, Binary]:
    vectors = {}
    with open(EMBEDDINGS_PATH, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                e = json.loads(line)
                if len(e["embedding"]) != DIMS:
                    sys.exit(f"{e['chunk_id']}: {len(e['embedding'])} dims, expected {DIMS}")
                vectors[e["chunk_id"]] = Binary.from_vector(e["embedding"], BinaryVectorDtype.FLOAT32)
    return vectors


def chunk_docs(vectors: dict[str, Binary]):
    missing = 0
    with open(CHUNKS_PATH, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            c = json.loads(line)
            c.pop("embed_text", None)  # only needed for embedding; saves ~56 MB
            vec = vectors.get(c["chunk_id"])
            if vec is None:
                missing += 1
                continue
            yield {"_id": c["chunk_id"], **c, "embedding": vec}
    if missing:
        print(f"  warning: {missing} chunks skipped (no embedding) -> rerun embed_chunks.py then this script")


def ensure_search_index(coll, name: str, definition: dict, kind: str):
    existing = {ix["name"]: ix for ix in coll.list_search_indexes()}
    if name in existing:
        print(f"  search index {name}: exists ({existing[name].get('status')})")
        return
    coll.create_search_index(SearchIndexModel(definition=definition, name=name, type=kind))
    print(f"  search index {name}: created (builds in the background, a few minutes)")


def wait_for_indexes(coll, names: list[str], timeout: int = 900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        status = {ix["name"]: ix.get("status") for ix in coll.list_search_indexes() if ix["name"] in names}
        if all(s == "READY" for s in status.values()) and len(status) == len(names):
            print(f"  search indexes READY after {time.time() - t0:.0f}s")
            return
        if any(s == "FAILED" for s in status.values()):
            sys.exit(f"search index build failed: {status}")
        time.sleep(15)
    print(f"  still building after {timeout}s: {status} (check Atlas UI -> Search; retrieval works once READY)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", action="store_true", help="drop posts + chunks before loading")
    ap.add_argument("--no-wait", action="store_true", help="don't wait for search indexes to be READY")
    args = ap.parse_args()

    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
    if not uri:
        sys.exit("set MONGODB_URI (or MONGO_URI) to the Atlas connection string")
    client = MongoClient(uri, appname="travel-chatbot-loader")
    client.admin.command("ping")
    db = client[DB_NAME]
    print(f"connected, db={DB_NAME}, server {client.server_info()['version']}")

    if args.drop:
        db.posts.drop()
        db.chunks.drop()
        print("dropped posts + chunks")

    print("posts:")
    bulk_upsert(db.posts, post_docs(), "posts")

    print("chunks:")
    vectors = load_embeddings()
    print(f"  {len(vectors)} embeddings loaded")
    bulk_upsert(db.chunks, chunk_docs(vectors), "chunks")
    db.chunks.create_index("post_no")  # group chunks by post (B5), rebuild threads

    print("search indexes:")
    try:
        ensure_search_index(db.chunks, VECTOR_INDEX, VECTOR_DEFINITION, "vectorSearch")
        ensure_search_index(db.chunks, TEXT_INDEX, TEXT_DEFINITION, "search")
    except OperationFailure as e:
        sys.exit(f"could not create search index: {e}")
    if not args.no_wait:
        wait_for_indexes(db.chunks, [VECTOR_INDEX, TEXT_INDEX])

    stats = db.command("dbStats", scale=1024 * 1024)
    print(f"posts={db.posts.estimated_document_count()} chunks={db.chunks.estimated_document_count()} | "
          f"dataSize={stats['dataSize']:.0f} MB storageSize={stats['storageSize']:.0f} MB "
          f"indexSize={stats['indexSize']:.0f} MB (M0 limit 512 MB)")


if __name__ == "__main__":
    main()
