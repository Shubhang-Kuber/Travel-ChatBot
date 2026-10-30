# Plan: travel chatbot data pipeline (Reddit posts -> destination + topic tags -> chunked hybrid search in MongoDB)

## Context
Travel chatbot input = country/state + question. Data: 10,482 Reddit travel posts (`travel_posts.jsonl`).
Comments are ~4.3x the size of title+body (median 3,355 vs 519 chars) and hold most of the advice, so one embedding
per post buries them. Pipeline: first tag each post with real destinations + topics (spaCy + local LLM), then make
comments first-class retrieval units via chunks + hybrid search.

---

## Phase A — destination + topic extraction (spaCy + local LLM)

### A0. Prep (user)
- `extract_arrow.py`: add only `title_query_comments_dot` to `KEEP_COLUMNS` (post-level thread quality: how well the
  comments match title + question; one score per post, not per comment); restrict length-stats loop (line ~94) to
  string columns (`if df[col].dtype == object`) or `.str.len()` crashes on floats. Rerun on
  `data-00000-of-00001.arrow`; row order unchanged so `post_no` stays stable.

### A1. spaCy candidates (exists: `destination_tagger.py`)
- NER (GPE/LOC/FAC) + PhraseMatchers + gazetteer (geonamescache countries, GeoNames admin1 states, cities -> state +
  country, `LANDMARKS`). Already tested on 50 rows.
- Done: `candidate_options(texts) -> list[list[Option]]`; `Option(id, mentions, tags)`, `str()` = numbered line,
  e.g. `3. "Mexico City", "CDMX" -> Mexico City, Mexico`. Options deduped by tags (mentions merged). Ambiguous
  spans keep up to 3 non-city alternatives from other countries (`Georgia` vs `Georgia, United States`);
  same-country alternatives dropped (state "Mexico"). Superset of `tag_many` tags (checked on 50 rows, 0.44 s).
- Tag shape (checked in `DestinationTagger.__init__`): tags already carry parents, no city-level tag.
  City -> `(state, country)` (or `(country,)` if state ambiguous), state -> `(state, country)`, landmark ->
  `(state, country)` / `(country,)`. So a Barcelona post is tagged `Catalonia, Spain` -> country filter "Spain"
  already matches it. City names reach search only via `$search` text.
- Catch: `tag_many` / `_resolve` flattens tuples into bare strings, so state "Georgia" (US) and country "Georgia"
  become the same string `"Georgia"`; same for "Punjab" (India/Pakistan), "Limburg" (NL/BE). Fixed via `dest_keys` (B3).
- Done: `dest_keys(tags)`, `tag_keys(text)`, `tag_keys_many(texts)` added (`states_by_country` built in
  `__init__`). Tested: Tbilisi -> `["Tbilisi|Georgia", "Georgia"]`, Atlanta -> `["Georgia|United States",
  "United States"]`, Niagara 4 keys, all LANDMARKS tuples resolve to known names.

### A1b. Topic taxonomy + keyword tagger (new `topic_tagger.py`)
- Fixed, closed list (~20) so tags stay filterable and consistent:
  `flights, trains, buses, driving` (car rental, road trips), `local_transport` (metro, taxi, Uber, ferries),
  `accommodation, food, nightlife, sightseeing, outdoors` (hiking, beaches, diving), `itinerary` (route, how many
  days), `budget` (costs, cheap/expensive), `money` (currency, cards, ATMs, tipping), `visa` (entry rules,
  passport, customs), `safety, health` (vaccines, insurance), `weather` (season, best time), `packing` (luggage,
  gear), `connectivity` (SIM, eSIM, wifi), `language_culture` (etiquette, language, local norms). Scams go under `safety`.
- `TOPIC_KEYWORDS: dict[topic, list[str]]` + spaCy `PhraseMatcher` (LOWER, lemma-ish variants: "flight", "airline",
  "layover", "JR pass", "Shinkansen", "Eurail", "hostel", "Airbnb", "eSIM"...). `tag_topics(texts) -> list[list[str]]`.
  Include casual question words users type in chat: `sightseeing` <- "famous", "must see", "attractions",
  "landmarks", "things to do", "worth visiting"; `food` <- "eat", "dishes", "restaurants", "street food";
  `flights` <- "fly", "airport", "cheapest way to get there".
- Used three ways: (1) hint candidates for the LLM, (2) cheap chunk-level `topic_mentions` (B3), (3) query-time
  topic detection (B4). Same lexicon everywhere -> consistent.
- Done: `topic_tagger.py` — `TOPICS`, `TOPIC_KEYWORDS`, `tag_topic(text)`, `tag_topics(texts)` (blank spaCy
  tokenizer, topics ordered by hit count). Noisy words left out ("tip", "grab", "skip", "deal", "locals").
  Full set title+body: 2.8 s, 2,272 posts (22%) no keyword hit (LLM must fill). Per topic: accommodation 2,357,
  budget 2,320, flights 2,314 ... trains 448, connectivity 345, buses 342.

### A2. Local LLM filter (new `llm_filter.py`)
- Runs against Ollama on Linux laptop (RTX 4060 8 GB, 16 GB RAM). URL/model/auth via env: `OLLAMA_HOST`,
  `DEST_MODEL`, `OLLAMA_AUTH` (for ngrok basic auth). Prefer Tailscale; if ngrok:
  `ngrok http 11434 --host-header="localhost:11434" --basic-auth "user:PASS"`.
- Input per post: full title + body (trim to title + candidate sentences if > ~1,500 tokens) + candidate list.
  No comments (they add other people's trips/suggestions; 4x tokens).
- Output: JSON constrained by `format` schema, one call returns both fields:
  ```json
  {"destination_ids": [3, 5], "topic_tags": ["trains", "budget"]}
  ```
  - `destination_ids`: `enum` = candidate ids -> can't invent places. Rules: keep places visited/planned/asked
    about; drop home/origin, departure points, comparisons, passing mentions.
  - `topic_tags`: `enum` = full taxonomy (A1b), `maxItems: 3`, pick what the post is *asking/about*, not every
    word mentioned. Keyword hits from `tag_topics` passed as hints ("keyword hits: flights, budget"), LLM may
    add/drop.
- Topic tags needed for every post, so 0-candidate posts still call LLM (destination enum empty -> `[]`).
  LLM error -> spaCy destination tags + keyword topic tags fallback.
- 4 parallel workers, timeout + 3 retries, resumable cache `llm_tags.jsonl` keyed by `post_no`
  (`{post_no, destination_options, topic_tags, model}`). `destination_options` = chosen Options' tag tuples kept
  as lists (`[["Georgia", "United States"], ["Spain"]]`), not flattened, so state vs country stays known.
- Model: test `qwen3:8b` (`think: false`) vs `qwen2.5:7b-instruct` on 50 rows, keep winner. ~1-2 h for full set.
- Done: `llm_filter.py` (`/api/chat`, `format` schema, temp 0, `num_ctx` 4096, fallback rows `model: "fallback"`
  retried on rerun, `--limit/--workers/--out`); `DestinationTagger.candidates_many` returns options + spaCy
  picks (fallback). 10-row test `qwen2.5:7b` via ngrok: 0 fallbacks, 0.81 s/post wall (4 workers) -> ~2.4 h full.
  Weak spots: keeps example/home places (p_00001 Dubai/Netherlands, p_00000 Vancouver), topics mostly echo
  keyword hits (p_00009 misses `itinerary`).
- Prompt v2 (done): per-candidate `role` (destination / home_or_origin / transit / example / past_trip /
  wrong_reading) instead of picking ids, 3 synthetic few-shot turns, keyword hits labelled noisy. Code guards:
  all readings of an ambiguous name `wrong_reading` -> keep spaCy pick; country-only option promoted when a
  kept destination lies in it. Cache rows add `dropped: [[tags, role]]` for A3 audit. 10 rows: p_00001 -> [],
  p_00007 Georgia ok, p_00009 gets `itinerary`; still wrong p_00000 (keeps Vancouver, drops Mexico City).
  1.2-1.5 s/post wall -> ~3.5-4.5 h full.
- Prompt v3 (current, user design): static system prompt (rules + 3 examples inside, KV-cache friendly), letters
  D/O/X/W, plain 2-line output (`LABELS: 1D 2W` / `TOPICS: a, b`) parsed by regex, no `format` schema. Candidate
  lines carry `[A:/B: reading]` for ambiguous names, in-title flag, mention count, ~10-word context; body cut
  to 900 chars. All-W allowed (spaCy-pick guard removed); country promotion kept. 10 rows: 0.81 s/post,
  p_00000 keeps Mexico City, p_00004 keeps only Florence, p_00007 ok; regressions p_00001 (all 3 examples -> D),
  p_00008/p_00009 miss `itinerary`.
  + 4th example (lounges -> all X) + wider `itinerary` definition: 0.59 s/post; p_00001 drops 2 of 3 (Dubai
  still D), p_00008/p_00009 still miss `itinerary`, p_00002 topics now empty. 10 rows too few to tune further.
- Prompt v4 (current): D covers arrival cities, day trips, overland stops, trip end; general question/poll ->
  all X; W never for a real place the post means; topics 1-3 (never none), route/days/feasible -> itinerary;
  5th example (London origin, Lima/Bolivia/Rio/Iguazu all D). 20 rows vs v3: destination errors 4 -> 1
  (p_00001, p_00010 Gibraltar, p_00019 India fixed; p_00017 Iran/Azerbaijan/Turkmenistan still W), 0 empty
  topic lists (was 3), `itinerary` added on p_00009/p_00015/p_00017. New nits: p_00011 (textile shopping) forced
  to `accommodation` (no shopping topic), p_00019 Goa X while India D, p_00008 still no `itinerary`. 0.69 s/post.
- Wired up (done): `llm_filter.tag_posts(post_nos, titles, bodies, tagger=...)` (cache-aware, rows carry
  `prompt`, `spacy_picks`, `keyword_topics`) + `summarize(recs)`. `modify_data.py` (SAMPLE_SIZE=100) calls it
  and writes `posts_100.json` (+ `destination_options`, `destination_tags` = dest_keys, `topic_tags` = LLM,
  `topic_mentions` = keyword, `tag_source`) and `posts_100_stats.json`. v4.1 = v4 + code guard: conflicting
  readings of one name -> keep spaCy-chosen reading's label, others W (fixed p_00073 Florida -> Uruguay).
- 100-post run (v4.1): 0 fallbacks, 0.63 s/post wall (~1.8 h full). 21 posts no candidates; 1.47 kept/post;
  LLM == spaCy picks on 50 posts, removed 111 spaCy picks. Topics 1.68/post, 4 meta posts with none,
  `itinerary` on 37% (> 30% split threshold, watch on full run).

### A3. Verify
- 50 rows side-by-side spaCy vs LLM: p_00018/p_00021 drop United States (origin), p_00022 drops Mexico/Thailand,
  p_00007 Georgia sensible. Measure s/post before full run.
- Topics: hand-label ~30 posts, compare LLM vs keyword-only topic tags (precision/recall per topic). Print topic
  distribution on full run; merge topics < ~50 posts, split any > ~30% (too broad).

---

## Phase B — chunked retrieval (after destination tags exist)

### B1. Two collections
- `posts`: `post_no, title, body, comments[], summary, destination_tags, topic_tags, title_query_comments_dot` —
  full post, returned to chatbot.
- `chunks`: one embedded doc per piece (~50k total):
  - `type: post` (title + body), `type: summary` (falcon_summary), `type: comment` (consecutive comments from
    `split_comments` grouped to ~250 tokens).

### B2. Contextual prefix
- Every chunk's `embed_text` starts with post title: `"Post: Spain itinerary\nComment: ..."`.

### B3. Tag fields per chunk
- New helper `dest_keys(tags_tuple) -> list[str]` in `destination_tagger.py`: `(state, country)` where state is a
  GeoNames admin1 of that country -> `["Catalonia|Spain", "Spain"]`; any other tuple -> each element is a country
  key (`("Spain",)` -> `["Spain"]`). Georgia US -> `["Georgia|United States", "United States"]`, Georgia country
  -> `["Georgia"]`; no collision. Plus `tag_keys_many(texts)` = `tag_many` but returns keys instead of bare strings.
- `destination_tags`: inherited from post = `dest_keys` of each `destination_options` tuple (Phase A).
- `mention_tags`: cheap spaCy keys (`tag_keys_many`) of that chunk's own text, so a "go to Portugal" comment
  matches Portugal queries.
- `topic_tags`: inherited from post (LLM, A2).
- `topic_mentions`: keyword `tag_topics` of chunk's own text, so a "take the night train" comment in an
  accommodation thread still matches train questions.

- Decision (user): no `mention_tags` (comment place mentions too noisy: "I live in Vancouver"). Filter =
  `destination_tags` only; untagged posts (~25%, mostly general questions) reachable via unfiltered `$search`.
- Done: `build_chunks.py --posts posts_100.json --out chunks_100.jsonl`. Chunk fields: `chunk_id, post_no,
  type, seq, text, embed_text, destination_tags, topic_tags, topic_mentions`. ~1,000-char chunks (split on
  sentences, word windows if unpunctuated); comment pieces re-joined with ", " (undoes split_comments cuts).
  AutoModerator text removed (leading bot pieces + marker pieces; bot sentences in summaries, bot-only
  summaries dropped). 100 posts -> ~508 chunks (5.1/post); 230/298 comment chunks add a topic_mention not in
  post topic_tags.

### B4. Search — MongoDB `$rankFusion` (8.0+)
- Query key X: country input -> `"Spain"`; state input -> `"Catalonia|Spain"` (UI state dropdown built from
  GeoNames admin1, always sends state + country). State filter with < ~5 posts -> fall back to its country key.
- `$vectorSearch` on `chunks`, pre-filter `{$or: [{destination_tags: X}, {mention_tags: X}]}` (all tag fields
  declared as `filter` fields; raise `numCandidates` for selective filters) + `$search` keyword/BM25 for exact
  names ("Hoi An", "JR pass", "visa").
- Query topics: run `tag_topics` on user question (fallback: tiny LLM call with same enum if 0 hits).
  Destination = hard filter; topic = **soft boost**, not filter (query topic detection can miss; filtering would
  drop good chunks). In `$search` add `should: [{in: {path: "topic_tags", value: [...]}}, {in: {path:
  "topic_mentions", ...}}]`; or bonus in B5.
- Fallback: if destination+topic gives < ~5 posts, keep destination filter only, drop topic boost.

### B5. Group by post
- Post score = best chunk + small bonus for other matching chunks + topic bonus (post `topic_tags` ∩ query
  topics > chunk `topic_mentions` match); `title_query_comments_dot` as tie-break / junk-thread filter (e.g.
  down-rank < 0.2). Per-comment relevance comes from chunk vectors, not this score.
- Optional: rerank top ~30 chunks with `bge-reranker-v2-m3` on the 4060.

### B6. Context to chatbot
- Top ~5 posts, each = title + its 2-3 best-matching chunks (not every comment).

### Embeddings
- Ollama on Linux GPU: `nomic-embed-text` (768-d, fits Atlas M0 512 MB) or `bge-m3` (1024-d, needs quantization/M10+).
  Same model for queries at chat time.
- Phase A final: 10,450/10,482 LLM-tagged; 32 left as spaCy + keyword fallback (repeated 500s), accepted.
- Done: `embed_chunks.py` (stdlib only; `EMBED_HOST` = laptop LAN IP, no ngrok — ngrok free ~1 GB/month would not
  carry ~800 MB of vectors). `nomic-embed-text` with `search_document:` / `search_query:` prefixes, batches of 64,
  2 workers, resumable `embeddings_<model>.jsonl` (`{chunk_id, embedding}`), `embed_query()` for retrieve.py.
  Store vectors in Mongo as float32 BinData (not double arrays) so ~53k x 768 fits M0.

### Files
- `preprocessing/`: `extract_arrow.py`, `destination_tagger.py` (+ `admin1CodesASCII.txt`), `topic_tagger.py`,
  `llm_filter.py`, `modify_data.py` (-> `mongo/mongo_data/posts.json`), `build_chunks.py`, `embed_chunks.py`.
- `mongo/`: `load_mongo.py` + `mongo_data/` (`posts.json`, `chunks.jsonl`, `embeddings_nomic-embed-text.jsonl`).
  Loaded into Atlas M0, db `travel`: `posts`, `chunks` (float32 BinData `embedding`), search indexes
  `chunks_vector` + `chunks_text`, index on `chunks.post_no`.
- Raw data (`data-00000-of-00001.arrow`, `travel_posts.jsonl`) at repo root; scripts resolve paths from their own
  location, so they run from any cwd.
- Next: `retrieve.py` (vector + `$search`, client-side RRF instead of `$rankFusion`, group + optional rerank);
  backend + LangGraph built separately by teammates.

### B6b. Follow-up / cross questions (multi-turn)
Topic tags are what make follow-ups work: user asks "Japan 2 weeks itinerary?" then "what about flights?",
"famous things?", "food?". Each follow-up = same destination, new topic -> fresh retrieval.
- Session state per chat: `{dest_key, topics, last_post_nos}`.
- Turn 1: `dest_key` = UI country/state input (wins over anything tagger finds in question; question may name
  origin/home). `tag_topics` on question only.
- Later turns: run `tag_keys_many` + `tag_topics` on new question.
  - New destination found -> replace `dest_key`; none -> reuse session one. UI input changed -> UI wins.
  - Topics = this turn's topics only (don't carry old topics; "food?" after "flights?" wants food).
- Query rewrite before embedding: build standalone query `"{destination} {topic words} {question}"`
  (e.g. "what about food?" -> "Japan food what about food?"). Template first; optional small LLM rewrite with
  last 2 turns if template too weak. Short follow-ups embed badly without this.
- Topic strength by question type:
  - Topic-only follow-up (no new destination, topic hit) -> topic as **pre-filter**
    `{destination_tags: X, $or: [{topic_tags: T}, {topic_mentions: T}]}`; if < ~5 posts, drop to boost (B4).
  - Normal question -> topic boost only (B4).
- Skip posts already shown (`last_post_nos`) unless results run out, so follow-up gives new threads.
- 0 topic hits + 0 destination change (e.g. "tell me more") -> reuse previous topics + `last_post_nos` chunks.

### B7. Verify
- ~20 hand-written queries ("Portugal food", "Japan rail pass", "Georgia winter"): compare post-level-only vs
  chunk-level top-5; confirm comment chunks surface for advice questions.
- Same queries with vs without topic boost: check top-5 topic precision rises ("Japan rail pass" -> `trains`
  threads, not general itineraries) without losing good off-topic-tagged chunks.
- ~10 scripted multi-turn chats ("Portugal trip?" -> "flights?" -> "famous places?" -> "food?"): each follow-up's
  top-5 on-topic, same destination, no repeated posts.
