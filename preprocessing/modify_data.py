import json
import re
import time
from pathlib import Path
import pandas as pd

from destination_tagger import DestinationTagger
from llm_filter import tag_posts, summarize

SAMPLE_SIZE = None # set to None to process the full dataset
OUT_NAME = f"posts_{SAMPLE_SIZE}" if SAMPLE_SIZE else "posts"
ROOT = Path(__file__).resolve().parents[1]  # dataset/
MONGO_DIR = ROOT / "mongo" / "mongo_data"  # files that get loaded into MongoDB
STATS_DIR = Path(__file__).resolve().parent


df = pd.read_json(ROOT / 'travel_posts.jsonl',lines=True)
if SAMPLE_SIZE:
    df = df.head(SAMPLE_SIZE)


df.insert(0,"post_no",[f"p_{i:05d}" for i in range(len(df))])


patterns = re.compile(r'(?<=[.!?%)])\s*,\s+|\s*,\s+')

def split_comments(text:str) ->list[str]:
    comment = patterns.split(text)
    return [cmt.strip() for cmt in comment if(len(cmt.strip())) > 15]


df = df.rename(columns={'selftext':'body','falcon_summary':'summary'})
df['body'] = df['body'].fillna('')

df['comments'] = df['comments'].apply(split_comments)


# Phase A: spaCy candidates -> local LLM keeps real destinations + picks topics (title + body only;
# comments drift to unrelated places). Cached in llm_tags.jsonl, so reruns only redo missing posts.
tagger = DestinationTagger()
start = time.time()
llm = tag_posts(df['post_no'].tolist(), df['title'].tolist(), df['body'].tolist(), tagger=tagger)
llm_seconds = time.time() - start

# destination_tags: filter keys of the LLM-kept places, ("Catalonia", "Spain") -> "Catalonia|Spain", "Spain".
# Always a list, even [] (posts without tags stay reachable via chunk-level mention_tags in Phase B).
df['destination_tags'] = [
    list(dict.fromkeys(k for tags in llm[p]['destination_options'] for k in tagger.dest_keys(tuple(tags))))
    for p in df['post_no']
]
df['topic_tags'] = [llm[p]['topic_tags'] for p in df['post_no']]


df.to_json(f'{MONGO_DIR}/{OUT_NAME}.json', orient='records', indent=2, force_ascii=False)

stats = summarize([llm[p] for p in df['post_no']])
stats['wall_seconds'] = round(llm_seconds, 1)
stats['destination_keys'] = {
    'posts_with_state_key': int(df['destination_tags'].apply(lambda ks: any('|' in k for k in ks)).sum()),
    'distinct_keys': len({k for ks in df['destination_tags'] for k in ks}),
}
with open(STATS_DIR / f'{OUT_NAME}_stats.json', 'w', encoding='utf-8') as f:
    json.dump(stats, f, indent=2, ensure_ascii=False)


print(df.columns.to_list())
with pd.option_context('display.max_colwidth', 60, 'display.width', 220):
    print(df[['post_no', 'title', 'destination_tags', 'topic_tags']].to_string(index=False))
print(json.dumps(stats, indent=2, ensure_ascii=False))
print(f"wrote {MONGO_DIR}/{OUT_NAME}.json, {OUT_NAME}_stats.json")
