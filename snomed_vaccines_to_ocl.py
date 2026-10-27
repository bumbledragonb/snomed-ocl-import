"""Build an OCL bulk-import file for a SNOMED CT subset from an RF2 Snapshot release.

Default subset: << 787859002 |Vaccine product (medicinal product)|

Steps (same as the walkthrough):
  1. Concept file       -> which concepts are active
  2. Relationship file  -> is-a (116680003) links; walk down from the root
  3. Description file   -> FSN and synonyms (active only)
  4. Language refset    -> which synonym is Preferred in US / GB English
  5. Relationship file  -> defining attributes (active ingredient, role, ...)
  6. Write OCL JSON-lines (Source, Concepts with parent_concept_urls) + review CSV,
     and a post-import file (hierarchy root + Source Version) to import once the first has finished

Usage:
  python snomed_vaccines_to_ocl.py <path to RF2 release folder>
Only the Python standard library is needed.
"""
import argparse
import collections
import csv
import glob
import json
import os
import re
import sys

IS_A = "116680003"
FSN = "900000000000003001"
SYNONYM = "900000000000013009"
US = "900000000000509007"
GB = "900000000000508004"
PREFERRED = "900000000000548007"
HAS_ACTIVE_INGREDIENT = "127489000"


def rf2(release, pattern):
    """Yield active rows (as lists) of the single Snapshot file matching pattern."""
    files = glob.glob(os.path.join(release, "Snapshot", "**", pattern), recursive=True)
    if len(files) != 1:
        sys.exit(f"Expected one file matching {pattern}, found {files}")
    with open(files[0], encoding="utf-8") as f:
        next(f)  # header
        for line in f:
            row = line.rstrip("\n").split("\t")
            if row[2] == "1":
                yield row


def semantic_tag(fsn):
    m = re.search(r"\(([^()]+)\)\s*$", fsn or "")
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("release", help="RF2 release folder (the one containing Snapshot/)")
    ap.add_argument("--root", default="787859002", help="Root SCTID; subset is the root and all descendants")
    ap.add_argument("--org", default="HL7-Brasil", help="OCL organization ID")
    ap.add_argument("--source", default="SNOMED-CT-Vaccines", help="OCL source ID")
    ap.add_argument("--out", default="output/snomed_vaccines_ocl", help="Output file prefix")
    ap.add_argument("--only", help="Comma-separated SCTIDs to keep from the subset, for a small test import")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    info = json.load(open(os.path.join(args.release, "release_package_information.json"), encoding="utf-8"))
    release_date = info.get("effectiveTime") or re.search(r"(\d{8})T", os.path.basename(os.path.normpath(args.release))).group(1)

    # 1. Active concepts
    concept_rows = {r[0]: r for r in rf2(args.release, "sct2_Concept_Snapshot_*.txt")}
    if args.root not in concept_rows:
        sys.exit(f"Root {args.root} is not an active concept in this release")

    # 2. Walk is-a links down from the root
    relationships = list(rf2(args.release, "sct2_Relationship_Snapshot_*.txt"))
    children = collections.defaultdict(set)
    for r in relationships:
        if r[7] == IS_A:
            children[r[5]].add(r[4])  # destinationId = parent, sourceId = child
    subset, frontier = {args.root}, [args.root]
    while frontier:
        frontier = [c for p in frontier for c in children[p] if c not in subset]
        subset.update(frontier)
    if args.only:
        only = set(args.only.split(","))
        if args.root not in only or not only <= subset:
            sys.exit(f"--only must include the root and only concepts under it; not in subset: {sorted(only - subset)}")
        subset = only
    parents = collections.defaultdict(list)
    for r in relationships:
        if r[7] == IS_A and r[4] in subset and r[5] in subset:
            parents[r[4]].append(r[5])

    # 3. Descriptions (all concepts, so attribute targets can be named too)
    descriptions = {}
    fsn = {}
    for r in rf2(args.release, "sct2_Description_Snapshot-en_*.txt"):
        if r[6] == FSN:
            fsn[r[4]] = r[7]
        if r[4] in subset:
            descriptions[r[0]] = {"concept": r[4], "type": r[6], "term": r[7], "US": None, "GB": None}

    # 4. Language refset: Preferred / Acceptable per dialect
    for r in rf2(args.release, "der2_cRefset_LanguageSnapshot-en_*.txt"):
        d = descriptions.get(r[5])
        if d and r[4] in (US, GB):
            d["US" if r[4] == US else "GB"] = "preferred" if r[6] == PREFERRED else "acceptable"

    terms = collections.defaultdict(list)
    for d in descriptions.values():
        if d["US"] or d["GB"]:  # keep only descriptions used by a dialect
            terms[d["concept"]].append(d)

    def preferred(code, dialect):
        return next((d["term"] for d in terms[code] if d["type"] == SYNONYM and d[dialect] == "preferred"), None)

    # 5. Defining attributes (non is-a relationships), plus concrete numeric values
    attributes = collections.defaultdict(list)
    for r in relationships:
        if r[4] in subset and r[7] != IS_A:
            attributes[r[4]].append({"group": int(r[6]), "type_id": r[7], "type": fsn.get(r[7]),
                                     "value_id": r[5], "value": fsn.get(r[5])})
    for r in rf2(args.release, "sct2_RelationshipConcreteValues_Snapshot_*.txt"):
        if r[4] in subset:
            attributes[r[4]].append({"group": int(r[6]), "type_id": r[7], "type": fsn.get(r[7]),
                                     "value": r[5].lstrip("#")})

    # 6. OCL JSON-lines; parents are written before their children
    base = f"/orgs/{args.org}/sources/{args.source}/concepts"
    order, seen = [], set()

    def visit(code):
        if code not in seen:
            seen.add(code)
            for p in parents[code]:
                visit(p)
            order.append(code)

    for code in sorted(subset):
        visit(code)

    source = {
        "type": "Source", "id": args.source, "short_code": args.source,
        "name": "SNOMED CT Vaccines (prototype)",
        "full_name": f"SNOMED CT subset << {args.root} |{fsn[args.root]}|",
        "owner": args.org, "owner_type": "Organization",
        "description": f"Prototype subset for semantic search testing. Extracted from SNOMED CT International Edition {release_date} (RF2 Snapshot).",
        "source_type": "External", "public_access": "None",
        "default_locale": "en", "supported_locales": ["en"],
        "custom_validation_schema": "None", "canonical_url": "http://snomed.info/sct",
        "hierarchy_meaning": "is-a",
        "copyright": "SNOMED CT content (c) SNOMED International. Use requires an appropriate SNOMED CT license.",
        "extras": {"snomed_edition": "International", "snomed_release": release_date, "subset_ecl": f"<< {args.root}"},
    }
    lines = [source]
    for code in order:
        us_pt = preferred(code, "US")
        names = [{"name": us_pt, "locale": "en", "locale_preferred": True, "name_type": "Synonym"},
                 {"name": fsn[code], "locale": "en", "locale_preferred": False, "name_type": "Fully Specified"}]
        names += [{"name": d["term"], "locale": "en", "locale_preferred": False, "name_type": "Synonym"}
                  for d in sorted(terms[code], key=lambda d: d["term"])
                  if d["type"] == SYNONYM and d["term"] != us_pt]
        concept = {
            "type": "Concept", "id": code, "external_id": code,
            "owner": args.org, "owner_type": "Organization", "source": args.source,
            "concept_class": semantic_tag(fsn[code]).capitalize(), "datatype": "N/A",
            "retired": False, "names": names, "descriptions": None,
            "extras": {"snomed_effective_time": concept_rows[code][1], "snomed_module": concept_rows[code][3],
                       "snomed_gb_preferred_term": preferred(code, "GB"),
                       "snomed_attributes": sorted(attributes[code], key=lambda a: (a["group"], a["type"] or "")) or None},
        }
        if parents[code]:
            concept["parent_concept_urls"] = [f"{base}/{p}/" for p in sorted(parents[code])]
        lines.append(concept)
    # The post-import file is imported only after the main import has finished:
    # - hierarchy_root_url only takes effect once the root concept exists, so the full Source line is
    #   sent again with it (OCL replaces fields wholesale, so nothing may be left out);
    # - OCL's parallel importer links parent_concept_urls in a final reconciliation pass, after every
    #   line of the file, so a Source Version in the main file would be created before the hierarchy.
    post_import = [
        {**source, "hierarchy_root_url": f"{base}/{args.root}/"},
        {"type": "Source Version", "id": f"v{release_date}", "source": args.source,
         "owner": args.org, "owner_type": "Organization", "released": False,
         "description": f"SNOMED CT International {release_date}, << {args.root}, {len(subset)} concepts"},
    ]

    for path, content in ((f"{args.out}.jsonl", lines), (f"{args.out}_post_import.jsonl", post_import)):
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for line in content:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")

    with open(f"{args.out}.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sctid", "us_preferred_term", "gb_preferred_term", "fsn", "other_synonyms",
                    "parents", "active_ingredients"])
        for code in sorted(subset, key=lambda c: preferred(c, "US").lower()):
            us_pt = preferred(code, "US")
            w.writerow([code, us_pt, preferred(code, "GB"), fsn[code],
                        " | ".join(sorted(d["term"] for d in terms[code] if d["type"] == SYNONYM and d["term"] != us_pt)),
                        " | ".join(f"{p} {preferred(p, 'US')}" for p in sorted(parents[code])),
                        " | ".join(a["value"] for a in attributes[code] if a["type_id"] == HAS_ACTIVE_INGREDIENT)])

    # Validation report
    tags = collections.Counter(semantic_tag(fsn[c]) for c in subset)
    print(f"Release:               SNOMED CT International {release_date}", file=sys.stderr)
    print(f"Concepts:              {len(subset)}", file=sys.stderr)
    print(f"Names:                 {sum(len(l.get('names', [])) for l in lines)}", file=sys.stderr)
    print(f"Semantic tags:         {dict(tags)}", file=sys.stderr)
    print(f"Missing US pref. term: {[c for c in subset if not preferred(c, 'US')] or 'none'}", file=sys.stderr)
    print(f"Multiple parents:      {sum(len(parents[c]) > 1 for c in subset)}", file=sys.stderr)
    print(f"Orphans (no parent):   {[c for c in subset if c != args.root and not parents[c]] or 'none'}", file=sys.stderr)
    print(f"Wrote {args.out}.jsonl ({len(lines)} lines), {args.out}_post_import.jsonl and {args.out}.csv", file=sys.stderr)


if __name__ == "__main__":
    main()
