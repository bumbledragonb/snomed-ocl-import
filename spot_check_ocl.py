"""Spot-check a large OCL import by sampling concepts, instead of downloading the whole source.

Reads the concept files written by snomed_to_ocl.py and checks in the OCL API:
  - the source's concept count matches the number of concepts in the files
  - a random sample from every part file, plus the concepts with the most parents, have the
    right class, names, extras and parent links (same checks as verify_ocl_import.py)

Usage (Git Bash; the token is never printed):
  export OCL_TOKEN="your token"
  python spot_check_ocl.py
  unset OCL_TOKEN
"""
import argparse
import glob
import os
import random
import sys
import urllib.error

from verify_ocl_import import OCL, compare_concept, load_jsonl, report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--files", default="output/snomed_ct", help="Folder written by snomed_to_ocl.py")
    ap.add_argument("--per-part", type=int, default=10, help="Random concepts to check from each part file")
    ap.add_argument("--most-parents", type=int, default=20, help="Also check the concepts with the most parents")
    ap.add_argument("--seed", type=int, default=1, help="Random seed, so a run can be repeated")
    ap.add_argument("--api", default="https://api.openconceptlab.org")
    args = ap.parse_args()

    token = os.environ.get("OCL_TOKEN")
    if not token:
        sys.exit('Set your OCL API token first, in Git Bash: export OCL_TOKEN="your token"')

    source = load_jsonl(f"{args.files}/01_source.jsonl")[0]
    repo = f"/orgs/{source['owner']}/sources/{source['id']}/"
    rng = random.Random(args.seed)
    parts = sorted(glob.glob(f"{args.files}/02_concepts_part*.jsonl"))

    total, sample, everything = 0, {}, []
    for path in parts:
        concepts = [c for c in load_jsonl(path) if c["type"] == "Concept"]
        total += len(concepts)
        everything += concepts
        for c in rng.sample(concepts, min(args.per_part, len(concepts))):
            sample[c["id"]] = c
    for c in sorted(everything, key=lambda c: len(c.get("parent_concept_urls") or []), reverse=True)[:args.most_parents]:
        sample[c["id"]] = c

    ocl = OCL(args.api, token)
    print(f"Checking {repo}: {total} concepts in {len(parts)} files, sampling {len(sample)}\n")
    try:
        summary = ocl.get(repo, {"includeSummary": "true"}).get("summary") or {}
        count = summary.get("active_concepts")
        results = [report(f"Concept count (files {total}, OCL {count})",
                          [] if count == total else [f"expected {total}, OCL has {count}"])]

        problems = []
        for i, (code, expected) in enumerate(sample.items(), 1):
            try:
                actual = ocl.get(f"{repo}concepts/{code}/", {"verbose": "true", "includeParentConceptURLs": "true"})
                problems += compare_concept(expected, actual)
            except urllib.error.HTTPError as e:
                problems.append(f"{code} not found in OCL (HTTP {e.code})")
            print(f"\r       checked {i}/{len(sample)}", end="", flush=True)
        print()
        results.append(report("Sampled concepts: class, names, extras and parent links", problems))
    except urllib.error.HTTPError as e:
        sys.exit(f"OCL API error {e.code} for {e.url}: {e.read().decode('utf-8', 'replace')[:300]}")

    print(f"\n{'ALL CHECKS PASSED' if all(results) else 'SOME CHECKS FAILED'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
