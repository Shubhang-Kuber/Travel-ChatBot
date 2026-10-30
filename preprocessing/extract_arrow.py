"""
Extract a HuggingFace .arrow dataset into plain CSV + JSONL files.

Usage:
    python extract_arrow.py path/to/file.arrow
    python extract_arrow.py path/to/saved_dataset_folder

Install first:
    pip install datasets pandas pyarrow
"""

import sys
from pathlib import Path

import pandas as pd

KEEP_COLUMNS = ["title", "selftext", "comments", "falcon_summary", "title_query_comments_dot"]


def load_arrow(path: Path) -> pd.DataFrame:
    """Load a .arrow file or a save_to_disk() folder into a DataFrame."""
    # Case 1: folder saved with Dataset.save_to_disk()
    if path.is_dir():
        from datasets import load_from_disk

        ds = load_from_disk(str(path))
        # DatasetDict (has splits) vs a single Dataset
        if hasattr(ds, "keys"):
            split = "train" if "train" in ds else list(ds.keys())[0]
            print(f"Using split: {split}")
            ds = ds[split]
        return ds.to_pandas()

    # Case 2: a single .arrow file
    try:
        from datasets import Dataset

        return Dataset.from_file(str(path)).to_pandas()
    except Exception as e:
        print(f"datasets loader failed ({e}), trying pyarrow directly...")

    import pyarrow as pa

    with open(path, "rb") as f:
        try:
            table = pa.ipc.open_stream(f).read_all()
        except pa.ArrowInvalid:
            f.seek(0)
            table = pa.ipc.open_file(f).read_all()
    return table.to_pandas()


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python extract_arrow.py <path-to-arrow-file-or-folder>")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"Path not found: {path}")
        sys.exit(1)

    df = load_arrow(path)

    print("\n=== Raw dataset ===")
    print(f"Rows: {len(df)}")
    print(f"Columns: {df.columns.tolist()}")

    # Drop the leftover pandas index column if present
    df = df.drop(columns=[c for c in df.columns if c.startswith("Unnamed")])

    # Keep only the columns we need (text + one thread-quality score)
    missing = [c for c in KEEP_COLUMNS if c not in df.columns]
    if missing:
        print(f"WARNING: expected columns not found: {missing}")
    present = [c for c in KEEP_COLUMNS if c in df.columns]

    # Basic cleaning: drop rows with no title or no comments
    before = len(df)
    df = df.dropna(subset=[c for c in ["title", "comments"] if c in df.columns])
    df = df[df["comments"].str.strip() != ""] if "comments" in df.columns else df
    df["selftext"] = df["selftext"].fillna("") if "selftext" in df.columns else ""
    print(f"Dropped {before - len(df)} empty rows, {len(df)} remain")

    df = df[present].reset_index(drop=True)

    # Export
    root = Path(__file__).resolve().parents[1]  # dataset/
    df.to_csv(root / "travel_posts.csv", index=False)
    df.to_json(root / "travel_posts.jsonl", orient="records", lines=True, force_ascii=False)
    print("\nSaved: travel_posts.csv, travel_posts.jsonl")

    # Quick look so you can sanity-check the data
    print("\n=== Text length stats (characters) ===")
    for col in present:
        if pd.api.types.is_string_dtype(df[col]):
            print(f"{col}: mean={int(df[col].str.len().mean())}, max={df[col].str.len().max()}")

    print("\n=== First row ===")
    row = df.iloc[0]
    for col in present:
        print(f"[{col}] {str(row[col])[:300]}\n")


if __name__ == "__main__":
    main()
