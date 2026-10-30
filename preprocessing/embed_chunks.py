"""Embed chunk embed_text with an Ollama embedding model (resumable). Stdlib only -> runs on Mac or Linux as is.

Env: EMBED_HOST (default http://localhost:11434, e.g. http://192.168.1.42:11434 for the laptop on LAN),
     EMBED_MODEL (default nomic-embed-text), EMBED_AUTH ("user:pass", only if behind ngrok basic auth).
Usage: python embed_chunks.py --chunks chunks_100.jsonl          # test
       python embed_chunks.py                                    # mongo/mongo_data/chunks.jsonl -> embeddings_nomic-embed-text.jsonl
Output: one line per chunk {"chunk_id": ..., "embedding": [...]}; rerun skips chunk_ids already embedded.
Queries at chat time must use the same model via embed_query().
"""
import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

EMBED_HOST = os.environ.get("EMBED_HOST", "http://localhost:11434").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
EMBED_AUTH = os.environ.get("EMBED_AUTH")

# nomic-embed-text is trained with task prefixes; documents and queries must use the matching one.
PREFIXES = {"nomic-embed-text": ("search_document: ", "search_query: ")}
DOC_PREFIX, QUERY_PREFIX = PREFIXES.get(EMBED_MODEL.split(":")[0], ("", ""))

ROOT = Path(__file__).resolve().parents[1]  # dataset/ (raw data lives here)
MONGO_DATA = ROOT / "mongo" / "mongo_data"  # files loaded into MongoDB

BATCH_SIZE = 64
TIMEOUT = 120
RETRIES = 3
DECIMALS = 6  # vectors are unit-length; 6 decimals keeps cosine scores exact enough and halves file size


def default_out(model: str = EMBED_MODEL) -> Path:
    return MONGO_DATA / f"embeddings_{model.replace(':', '_').replace('/', '_')}.jsonl"


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{EMBED_HOST}{path}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    if EMBED_AUTH:
        req.add_header("Authorization", "Basic " + base64.b64encode(EMBED_AUTH.encode()).decode())
    req.add_header("ngrok-skip-browser-warning", "1")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def embed_texts(texts: list[str], prefix: str = DOC_PREFIX) -> list[list[float]]:
    """Ollama /api/embed (returns unit-length vectors). Over-long texts are truncated to the model context."""
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            out = _post("/api/embed", {"model": EMBED_MODEL, "input": [prefix + t for t in texts], "truncate": True})
            vecs = out["embeddings"]
            if len(vecs) != len(texts):
                raise ValueError(f"got {len(vecs)} vectors for {len(texts)} texts")
            return vecs
        except urllib.error.HTTPError as e:
            last_err = f"{e.code}: {e.read()[:200]!r}"
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < RETRIES:
            time.sleep(2 ** attempt)
    raise RuntimeError(last_err)


def embed_query(text: str) -> list[float]:
    """Chat-time query vector (same model, query prefix)."""
    return embed_texts([text], prefix=QUERY_PREFIX)[0]


def load_done(path: Path) -> set[str]:
    done = set()
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    done.add(json.loads(line)["chunk_id"])
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", type=Path, default=MONGO_DATA / "chunks.jsonl")
    ap.add_argument("--out", type=Path, default=None, help="default embeddings_<model>.jsonl")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = ap.parse_args()
    out_path = args.out or default_out()

    with open(args.chunks, encoding="utf-8") as f:
        chunks = [json.loads(line) for line in f if line.strip()]
    done = load_done(out_path)
    todo = [c for c in chunks if c["chunk_id"] not in done]
    print(f"model={EMBED_MODEL} host={EMBED_HOST} chunks={len(chunks)} done={len(done)} todo={len(todo)} -> {out_path}")
    if not todo:
        return

    batches = [todo[i:i + args.batch_size] for i in range(0, len(todo), args.batch_size)]
    n_done, n_failed, dims, t0 = 0, 0, set(), time.time()
    with open(out_path, "a", encoding="utf-8") as f, ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(embed_texts, [c["embed_text"] for c in b]): b for b in batches}
        for n, fut in enumerate(as_completed(futures), start=1):
            batch = futures[fut]
            try:
                vecs = fut.result()
            except RuntimeError as e:  # skipped batch -> picked up again on rerun
                n_failed += len(batch)
                print(f"batch failed ({len(batch)} chunks, first {batch[0]['chunk_id']}): {e}")
                continue
            for c, v in zip(batch, vecs):  # only the main thread writes
                dims.add(len(v))
                f.write(json.dumps({"chunk_id": c["chunk_id"], "embedding": [round(x, DECIMALS) for x in v]}) + "\n")
            f.flush()
            n_done += len(batch)
            if n % 20 == 0 or n == len(batches):
                el = time.time() - t0
                print(f"{n_done}/{len(todo)} chunks, {n_done / el:.0f} chunks/s, "
                      f"eta {(len(todo) - n_done) / max(n_done / el, 1e-9) / 60:.1f} min")

    print(f"done: embedded={n_done} failed={n_failed} dims={sorted(dims)} in {time.time() - t0:.0f}s"
          + (" (rerun to retry failed batches)" if n_failed else ""))


if __name__ == "__main__":
    main()
