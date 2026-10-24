"""Exact hashes, text near-duplicates, and optional CLIP image similarity."""
import os
import re
import hashlib
from itertools import combinations
import numpy as np
from database.repository import q, stored_file_path
from services.document_processor import unblob

# ---------- Duplicates / similar ----------
def find_duplicates(uid: int, include_images: bool = True):
    # Every query is scoped to the authenticated owner. SHA-256 values are
    # calculated from uploaded bytes and are the only basis for exact matches.
    rows = q("select id,name,path,kind,category,sha,created,clip,demo_key,text from files where uid=? order by created desc", (uid,))
    by_hash = {}
    for item in rows:
        if item["sha"]:
            by_hash.setdefault(item["sha"], []).append(item)

    def public_file(item):
        try: size = os.path.getsize(stored_file_path(item["path"]))
        except OSError: size = None
        return {"id": item["id"], "name": item["name"], "kind": item["kind"],
                "category": item["category"], "created": item["created"], "size_bytes": size,
                "is_demo": bool(item["demo_key"])}

    exact = [{"files": [public_file(item) for item in group]}
             for group in by_hash.values() if len(group) > 1]

    # MinHash/LSH buckets restrict pairwise text similarity checks to likely
    # candidates; Jaccard similarity over normalized 3-word shingles supplies
    # an explainable score. This is a review hint, never an identity claim.
    text_rows = [item for item in rows if item["kind"] != "image" and item["text"] and item["sha"]]
    signatures, shingle_sets = {}, {}
    bands = 16
    rows_per_band = 4
    for item in text_rows:
        tokens = re.findall(r"[a-z0-9]+", (item["text"] or "").casefold())
        if len(tokens) < 30:
            continue
        shingles = {" ".join(tokens[i:i + 3]) for i in range(len(tokens) - 2)}
        if not shingles:
            continue
        shingle_sets[item["id"]] = shingles
        # Hash each (seed, shingle) independently; XORing one base hash with
        # seeds gives correlated minima and can miss genuinely similar pairs.
        signatures[item["id"]] = [min(
            int.from_bytes(hashlib.blake2b(f"{seed}:{value}".encode("utf-8"), digest_size=8).digest(), "big")
            for value in shingles
        ) for seed in range(bands * rows_per_band)]
    buckets = {}
    for item in text_rows:
        signature = signatures.get(item["id"])
        if signature is None:
            continue
        for band in range(bands):
            key = tuple(signature[band * rows_per_band:(band + 1) * rows_per_band])
            buckets.setdefault((band, key), []).append(item["id"])
    candidate_ids = set()
    for members in buckets.values():
        # A huge boilerplate bucket would be quadratic; omit it to keep scans bounded.
        if 1 < len(members) <= 100:
            candidate_ids.update(tuple(sorted(pair)) for pair in combinations(members, 2))
    by_id = {item["id"]: item for item in text_rows}
    near_duplicates = []
    for left_id, right_id in candidate_ids:
        left_set, right_set = shingle_sets[left_id], shingle_sets[right_id]
        score = len(left_set & right_set) / max(1, len(left_set | right_set))
        left, right = by_id[left_id], by_id[right_id]
        if score >= 0.72 and left["sha"] != right["sha"]:
            near_duplicates.append({"files": [public_file(left), public_file(right)],
                                    "score": round(score, 3), "metric": "3-word shingle Jaccard",
                                    "reason": "Normalized extracted text has substantially overlapping three-word phrases."})
    near_duplicates.sort(key=lambda item: item["score"], reverse=True)

    # Similarity is reported only for images that have stored CLIP vectors.
    # Text embeddings are deliberately excluded because they do not measure
    # visual similarity. Older uploads without CLIP data simply have no pair.
    all_images = [item for item in rows if item["kind"] == "image"]
    images = [item for item in all_images if item["clip"]]
    similar_images = []
    if include_images:
        for i, left in enumerate(images):
            for right in images[i + 1:]:
                if left["sha"] == right["sha"]:
                    continue
                lv, rv = unblob(left["clip"]), unblob(right["clip"])
                if lv.size == 0 or lv.shape != rv.shape:
                    continue
                denom = float(np.linalg.norm(lv) * np.linalg.norm(rv))
                if not denom:
                    continue
                score = float(np.dot(lv, rv) / denom)
                if score >= 0.94:
                    similar_images.append({"files": [public_file(left), public_file(right)], "score": round(score, 3)})

    similar_status = "no_images" if not all_images else "not_indexed" if not images else "available"
    near_status = "available" if shingle_sets else "insufficient_text"
    return {"exact": exact, "near_duplicates": near_duplicates,
            "near_duplicate_status": near_status,
            "similar_images": similar_images,
            "similar_image_status": similar_status}

