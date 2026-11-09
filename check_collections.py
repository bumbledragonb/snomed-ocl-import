"""Check the OCL collections (value sets) against SNOMED CT: each holds exactly the right concepts.

The expected members are worked out again from the RF2 release and the collection definitions
(collections/clinical_findings.json), not taken from the import file:
  - a collection with roots holds every root and every active descendant of a root
  - the "rest" collection holds every concept in its 'within' hierarchy that no other collection holds
It also checks each collection exists in OCL as a Value Set with the right name and canonical URL.

Usage (Git Bash, from the project folder; the token is never printed):
  export OCL_TOKEN="your token"
  python check_collections.py "C:/Users/brotz/Favorites/Downloads/SnomedCT_InternationalRF2_PRODUCTION_20261001T120000Z/SnomedCT_InternationalRF2_PRODUCTION_20261001T120000Z"
  unset OCL_TOKEN

--against-files output/snomed_ct checks 04_collections.jsonl instead of OCL (no token needed).
"""
import argparse
import json
import os
import sys
import urllib.error

from check_ocl_against_snomed import Snomed, api_get, last_segment, show


def expected_members(snomed, config):
    def subtree(code):
        return snomed.descendants(code) | {code}
    members = {c["id"]: set().union(*(subtree(r) for r in c["roots"])) for c in config["collections"]}
    rest = config.get("rest")
    if rest:
        members[rest["id"]] = subtree(rest["within"]) - set().union(*members.values())
    return members


def from_file(path):
    """Collections and their members as written in 04_collections.jsonl."""
    collections, members = {}, {}
    for line in open(path, encoding="utf-8"):
        d = json.loads(line)
        if d["type"] == "Collection":
            collections[d["id"]] = d
            members.setdefault(d["id"], set())
        elif d["type"] == "Reference":
            cid = last_segment(d["collection_url"])
            members.setdefault(cid, set()).update(last_segment(e) for e in d["data"]["expressions"])
    return collections, members


def from_ocl(api, org, token, ids, page_size):
    collections, members = {}, {}
    for cid in ids:
        path = f"/orgs/{org}/collections/{cid}/"
        try:
            collections[cid], _ = api_get(api, path, token, {"includeSummary": "true"})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        found, page = set(), 1
        while True:
            batch, headers = api_get(api, f"{path}concepts/", token, {"limit": page_size, "page": page})
            found.update(c["id"] for c in batch)
            total = int(headers.get("num_found") or 0)
            print(f"\r  {cid:32} {len(found):7,} of {total:,}", end="", flush=True)
            if not batch or len(found) >= total:
                break
            page += 1
        print()
        members[cid] = found
    return collections, members


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("release", help="RF2 release folder (the one containing Snapshot/)")
    ap.add_argument("--definitions", default="collections/clinical_findings.json")
    ap.add_argument("--org", default="HL7-Brasil")
    ap.add_argument("--api", default="https://api.openconceptlab.org")
    ap.add_argument("--page-size", type=int, default=1000)
    ap.add_argument("--against-files", help="Check this folder's 04_collections.jsonl instead of OCL")
    args = ap.parse_args()

    config = json.load(open(args.definitions, encoding="utf-8"))
    names = {c["id"]: c["name"] for c in config["collections"]}
    if config.get("rest"):
        names[config["rest"]["id"]] = config["rest"]["name"]

    if args.against_files:
        collections, members = from_file(os.path.join(args.against_files, "04_collections.jsonl"))
        label = f"{args.against_files}/04_collections.jsonl"
    else:
        token = os.environ.get("OCL_TOKEN")
        if not token:
            sys.exit('Set your OCL API token first, in Git Bash: export OCL_TOKEN="your token"')
        print(f"Downloading the members of {len(names)} collections ...")
        try:
            collections, members = from_ocl(args.api, args.org, token, names, args.page_size)
        except urllib.error.HTTPError as e:
            sys.exit(f"OCL API error {e.code} for {e.url}: {e.read().decode('utf-8', 'replace')[:300]}")
        label = f"OCL /orgs/{args.org}/collections/"

    snomed = Snomed(args.release)
    want = expected_members(snomed, config)

    settings, content = {}, {}
    for cid, name in names.items():
        c = collections.get(cid)
        if c is None:
            settings.setdefault("collection missing", []).append(cid)
            continue
        canonical = f"http://hl7.org.br/fhir/ValueSet/snomed-{cid.lower()}"
        for field, value in (("name", name), ("collection_type", "Value Set"), ("canonical_url", canonical)):
            if c.get(field) != value:
                settings.setdefault(f"{field} differs", []).append(f"{cid}: expected {value!r}, found {c.get(field)!r}")
        got = members.get(cid, set())
        missing, extra = want[cid] - got, got - want[cid]
        if missing:
            content.setdefault("concepts missing from a collection", []).append(f"{cid}: {len(missing):,} e.g. {sorted(missing)[:5]}")
        if extra:
            content.setdefault("concepts that should not be in a collection", []).append(f"{cid}: {len(extra):,} e.g. {sorted(extra)[:5]}")

    print(f"\nChecking {label} against SNOMED CT")
    for cid in names:
        print(f"       {cid:32} expected {len(want[cid]):7,}   found {len(members.get(cid, ())):7,}")
    print(f"       {'total memberships':32} expected {sum(map(len, want.values())):7,}   found {sum(map(len, members.values())):7,}\n")
    results = [show("Collections exist as Value Sets with the right name and canonical URL", settings),
               show("Each collection holds exactly the SNOMED concepts it should", content)]
    print(f"\n{'ALL CHECKS PASSED' if all(results) else 'SOME CHECKS FAILED'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
