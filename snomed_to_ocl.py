"""Build OCL bulk-import files for one or more SNOMED CT hierarchies, keeping the hierarchy and every relationship.

What goes in:
  - every active concept under the given roots,
  - every concept their relationships point to (body structures, organisms, substances, ...),
  - and all ancestors of those, so the whole tree stays connected up to |SNOMED CT Concept|.

How SNOMED CT becomes OCL:
  - is-a relationships      -> parent_concept_urls on the concept
  - other relationships     -> OCL Mappings, map_type = the attribute's name (e.g. "Finding site"),
                               id = the SNOMED relationship id, relationship group in extras
  - concrete values (#1, …) -> the concept's extras (a mapping cannot point to a number)
  - subsets                 -> OCL Collections, defined by roots in a JSON config

Files written to --out, imported in this order (see the printed instructions):
  01_source.jsonl                 the Source
  02_concepts_partNN.jsonl        concepts, parents always in an earlier part or earlier in the same part
  03_mappings_partNN.jsonl        relationships, after all concepts exist
  04_collections.jsonl            Collections and their References
  05_versions.jsonl               Source Version, then Collection Versions (after the hierarchy root is set)
  summary.csv                     one row per concept, for review

Usage:
  python snomed_to_ocl.py <RF2 release folder> --roots 404684003 --collections collections/clinical_findings.json
"""
import argparse
import collections
import csv
import json
import os
import re
import sys

from snomed_vaccines_to_ocl import FSN, GB, IS_A, PREFERRED, SYNONYM, US, rf2, semantic_tag

SNOMED_ROOT = "138875005"


def descendants(root, children):
    found, frontier = {root}, [root]
    while frontier:
        frontier = [c for p in frontier for c in children[p] if c not in found]
        found.update(frontier)
    return found


def with_ancestors(codes, parents):
    found, stack = set(codes), list(codes)
    while stack:
        for p in parents[stack.pop()]:
            if p not in found:
                found.add(p)
                stack.append(p)
    return found


def depth_order(codes, parents):
    """Concepts ordered by their longest path from the top, so every parent comes before its children."""
    depth = {}

    def longest(code):
        if code not in depth:
            depth[code] = 0  # guards against cycles, which SNOMED CT does not have
            depth[code] = 1 + max((longest(p) for p in parents[code] if p in codes), default=-1)
        return depth[code]

    sys.setrecursionlimit(10000)
    for code in codes:
        longest(code)
    return sorted(codes, key=lambda c: (depth[c], int(c)))


def chunks(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def write_jsonl(path, lines):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("release", help="RF2 release folder (the one containing Snapshot/)")
    ap.add_argument("--roots", default="404684003", help="Comma-separated SCTIDs of the hierarchies to load")
    ap.add_argument("--collections", help="JSON file defining collections by root concepts")
    ap.add_argument("--org", default="HL7-Brasil")
    ap.add_argument("--source", default="SNOMED-CT")
    ap.add_argument("--concepts-per-file", type=int, default=25000)
    ap.add_argument("--mappings-per-file", type=int, default=100000)
    ap.add_argument("--only", help="Comma-separated SCTIDs: keep only these (plus targets and ancestors), for a test")
    ap.add_argument("--out", default="output/snomed_ct")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    info = json.load(open(os.path.join(args.release, "release_package_information.json"), encoding="utf-8"))
    release_date = info.get("effectiveTime")
    concept_rows = {r[0]: r for r in rf2(args.release, "sct2_Concept_Snapshot_*.txt")}

    # 1. Hierarchy and relationships
    parents, children = collections.defaultdict(set), collections.defaultdict(set)
    relationships = collections.defaultdict(list)
    for r in rf2(args.release, "sct2_Relationship_Snapshot_*.txt"):
        if r[7] == IS_A:
            parents[r[4]].add(r[5])
            children[r[5]].add(r[4])
        else:
            relationships[r[4]].append(r)
    concrete = collections.defaultdict(list)
    for r in rf2(args.release, "sct2_RelationshipConcreteValues_Snapshot_*.txt"):
        concrete[r[4]].append(r)

    # 2. What to load: the hierarchies, what they point to, and all ancestors
    core = set().union(*(descendants(root, children) for root in args.roots.split(",")))
    if args.only:
        core = set(args.only.split(",")) & core
    # Targets have relationships of their own (a body structure's Laterality -> Side), so keep adding
    # targets and their ancestors until every relationship of every loaded concept can be kept.
    subset = set(core)
    while True:
        grown = with_ancestors(subset | {r[5] for c in subset for r in relationships[c]}, parents)
        if grown == subset:
            break
        subset = grown
    if not subset <= concept_rows.keys():
        sys.exit("Some concepts are not active in this release")

    # 3. Names
    fsn, terms = {}, collections.defaultdict(dict)
    for r in rf2(args.release, "sct2_Description_Snapshot-en_*.txt"):
        if r[6] == FSN:
            fsn[r[4]] = r[7]
        if r[4] in subset:
            terms[r[4]][r[0]] = {"type": r[6], "term": r[7], "US": None, "GB": None}
    descriptions = {d_id: d for t in terms.values() for d_id, d in t.items()}
    for r in rf2(args.release, "der2_cRefset_LanguageSnapshot-en_*.txt"):
        d = descriptions.get(r[5])
        if d and r[4] in (US, GB):
            d["US" if r[4] == US else "GB"] = "preferred" if r[6] == PREFERRED else "acceptable"

    def preferred(code, dialect="US"):
        return next((d["term"] for d in terms[code].values() if d["type"] == SYNONYM and d[dialect] == "preferred"), None)

    def label(code):
        return re.sub(r"\s*\([^()]+\)$", "", fsn[code])

    base = f"/orgs/{args.org}/sources/{args.source}/concepts"
    source = {
        "type": "Source", "id": args.source, "short_code": args.source,
        "name": "SNOMED CT (HL7 Brasil)", "full_name": "SNOMED CT International Edition (subset loaded by HL7 Brasil)",
        "owner": args.org, "owner_type": "Organization",
        "description": f"SNOMED CT International Edition {release_date} (RF2 Snapshot): {', '.join(label(r) for r in args.roots.split(','))}, "
                       "with every concept their relationships point to and all ancestors. Relationships other than is-a are mappings.",
        "source_type": "External", "public_access": "None",
        "default_locale": "en", "supported_locales": ["en"], "custom_validation_schema": "None",
        "canonical_url": "http://snomed.info/sct", "hierarchy_meaning": "is-a",
        "copyright": "SNOMED CT content (c) SNOMED International. Use requires an appropriate SNOMED CT license.",
        "extras": {"snomed_edition": "International", "snomed_release": release_date, "loaded_roots": args.roots.split(",")},
    }

    # 4. Concepts, parents first
    order = depth_order(subset, parents)
    no_preferred = []
    concept_lines = []
    for code in order:
        us_pt = preferred(code) or label(code)
        if not preferred(code):
            no_preferred.append(code)
        names = [{"name": us_pt, "locale": "en", "locale_preferred": True, "name_type": "Synonym"},
                 {"name": fsn[code], "locale": "en", "locale_preferred": False, "name_type": "Fully Specified"}]
        names += [{"name": d["term"], "locale": "en", "locale_preferred": False, "name_type": "Synonym"}
                  for d in sorted(terms[code].values(), key=lambda d: d["term"])
                  if d["type"] == SYNONYM and d["term"] != us_pt and (d["US"] or d["GB"])]
        extras = {"snomed_effective_time": concept_rows[code][1], "snomed_module": concept_rows[code][3],
                  "snomed_definition_status": "defined" if concept_rows[code][4] == "900000000000073002" else "primitive",
                  "snomed_gb_preferred_term": preferred(code, "GB")}
        if concrete[code]:
            extras["snomed_concrete_values"] = [{"group": int(r[6]), "type_id": r[7], "type": label(r[7]),
                                                 "value": r[5].lstrip("#")} for r in sorted(concrete[code], key=lambda r: (r[6], r[7]))]
        line = {"type": "Concept", "id": code, "external_id": code, "owner": args.org, "owner_type": "Organization",
                "source": args.source, "concept_class": (semantic_tag(fsn[code]) or "misc").capitalize(),
                "datatype": "N/A", "retired": False, "names": names, "descriptions": None, "extras": extras}
        if parents[code] & subset:
            line["parent_concept_urls"] = [f"{base}/{p}/" for p in sorted(parents[code] & subset)]
        concept_lines.append(line)

    # 5. Relationships as mappings (only between loaded concepts)
    # OCL allows one mapping per (from concept, map type, to concept), but SNOMED can repeat the same
    # attribute and value in several role groups. Those become one mapping that lists every group and
    # relationship id, so each role group can still be rebuilt. The mapping keeps the first relationship's id.
    mapping_lines, skipped, merged = [], collections.Counter(), 0
    for code in order:
        by_pair = {}
        for r in sorted(relationships[code], key=lambda r: (int(r[6]), r[7], r[5])):
            if r[5] not in subset:
                skipped[label(r[7])] += 1
                continue
            if (r[7], r[5]) in by_pair:
                extras = by_pair[(r[7], r[5])]["extras"]
                extras["relationship_groups"].append(int(r[6]))
                extras["relationship_ids"].append(r[0])
                merged += 1
                continue
            line = {
                "type": "Mapping", "id": r[0], "map_type": label(r[7]),
                "from_concept_url": f"{base}/{code}/", "to_concept_url": f"{base}/{r[5]}/",
                "owner": args.org, "owner_type": "Organization", "source": args.source,
                "extras": {"attribute_id": r[7], "relationship_groups": [int(r[6])], "relationship_ids": [r[0]]}}
            by_pair[(r[7], r[5])] = line
            mapping_lines.append(line)

    # 6. Collections
    collection_lines, version_lines, members = [], [], {}
    if args.collections:
        config = json.load(open(args.collections, encoding="utf-8"))
        for c in config["collections"]:
            members[c["id"]] = (c["name"], set().union(*(descendants(r, children) for r in c["roots"])) & core)
        if config.get("rest"):
            rest = config["rest"]
            covered = set().union(*(m for _, m in members.values()))
            members[rest["id"]] = (rest["name"], (descendants(rest["within"], children) & core) - covered)
        for cid, (name, codes) in members.items():
            collection_lines.append({
                "type": "Collection", "id": cid, "short_code": cid, "name": name, "full_name": f"SNOMED CT: {name}",
                "owner": args.org, "owner_type": "Organization", "collection_type": "Value Set",
                "public_access": "None", "default_locale": "en", "supported_locales": ["en"],
                "description": f"{len(codes)} SNOMED CT concepts from {args.org}/{args.source} {release_date}.",
                "canonical_url": f"http://hl7.org.br/fhir/ValueSet/snomed-{cid.lower()}",
                "copyright": source["copyright"], "extras": {"snomed_release": release_date}})
            for part in chunks(sorted(codes, key=int), 1000):
                collection_lines.append({"type": "Reference", "collection_url": f"/orgs/{args.org}/collections/{cid}/",
                                         "data": {"expressions": [f"{base}/{code}/" for code in part]}})
            version_lines.append({"type": "Collection Version", "id": f"v{release_date}", "collection": cid,
                                  "owner": args.org, "owner_type": "Organization", "released": False,
                                  "description": f"SNOMED CT {release_date}: {name}, {len(codes)} concepts"})

    # 7. Write files
    write_jsonl(os.path.join(args.out, "01_source.jsonl"), [source])
    concept_files = chunks(concept_lines, args.concepts_per_file)
    for i, part in enumerate(concept_files, 1):
        write_jsonl(os.path.join(args.out, f"02_concepts_part{i:02d}.jsonl"), part)
    mapping_files = chunks(mapping_lines, args.mappings_per_file)
    for i, part in enumerate(mapping_files, 1):
        write_jsonl(os.path.join(args.out, f"03_mappings_part{i:02d}.jsonl"), part)
    if collection_lines:
        write_jsonl(os.path.join(args.out, "04_collections.jsonl"), collection_lines)
    version_lines.insert(0, {"type": "Source Version", "id": f"v{release_date}", "source": args.source,
                             "owner": args.org, "owner_type": "Organization", "released": False,
                             "description": f"SNOMED CT International {release_date}: {len(subset)} concepts, {len(mapping_lines)} relationships"})
    write_jsonl(os.path.join(args.out, "05_versions.jsonl"), version_lines)

    in_collections = collections.defaultdict(list)
    for cid, (_, codes) in members.items():
        for code in codes:
            in_collections[code].append(cid)
    with open(os.path.join(args.out, "summary.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sctid", "preferred_term", "semantic_tag", "loaded_as", "parents", "relationships", "collections"])
        for line in concept_lines:
            code = line["id"]
            w.writerow([code, line["names"][0]["name"], semantic_tag(fsn[code]),
                        "hierarchy" if code in core else "target/ancestor",
                        len(line.get("parent_concept_urls", [])), len(relationships[code]), " ".join(sorted(in_collections[code]))])

    # 8. Report and import instructions
    orphans = [c for c in subset if c != SNOMED_ROOT and not parents[c] & subset]
    def size(path): return os.path.getsize(path) / 2**20
    print(f"Release SNOMED CT International {release_date}")
    print(f"Concepts: {len(subset):,} ({len(core):,} in the hierarchies, {len(subset) - len(core):,} relationship targets and ancestors)")
    print(f"Semantic tags: {dict(collections.Counter(semantic_tag(fsn[c]) for c in subset).most_common(8))}")
    print(f"is-a links: {sum(len(l.get('parent_concept_urls', [])) for l in concept_lines):,} | orphans: {orphans or 'none'}")
    print(f"Relationship mappings: {len(mapping_lines):,} ({len(mapping_lines) + merged:,} relationships, {merged:,} repeats in "
          f"other role groups merged) | skipped (target not loaded): {sum(skipped.values()):,} {dict(skipped.most_common(5))}")
    print(f"Concepts without a US preferred term (FSN used): {len(no_preferred)}")
    for cid, (name, codes) in members.items():
        print(f"  collection {cid:28} {len(codes):7,}  {name}")
    print("\nImport in this order, waiting for each to finish (OCL Bulk Import, Hierarchy ticked for concept files):")
    print(f"  1. 01_source.jsonl")
    for i in range(1, len(concept_files) + 1):
        p = os.path.join(args.out, f"02_concepts_part{i:02d}.jsonl")
        print(f"  {i + 1}. {os.path.basename(p)}  ({size(p):.0f} MiB)")
    n = len(concept_files) + 2
    for i in range(1, len(mapping_files) + 1):
        p = os.path.join(args.out, f"03_mappings_part{i:02d}.jsonl")
        print(f"  {n}. {os.path.basename(p)}  ({size(p):.0f} MiB)")
        n += 1
    if collection_lines:
        print(f"  {n}. 04_collections.jsonl"); n += 1
    print(f"  {n}. Set the hierarchy root with the API:")
    print(f'     curl -s -X PATCH "https://api.openconceptlab.org/orgs/{args.org}/sources/{args.source}/" '
          f'-H "Authorization: Token $OCL_TOKEN" -H "Content-Type: application/json" '
          f"-d '{{\"hierarchy_root_url\": \"{base}/{SNOMED_ROOT}/\"}}'")
    print(f"  {n + 1}. 05_versions.jsonl")


if __name__ == "__main__":
    main()
