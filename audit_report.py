"""Write a side-by-side audit of every concept and relationship: SNOMED CT release vs OCL.

Like opening each concept in OCL next to the SNOMED browser, but for all of them. One row per
concept and one row per relationship, with the SNOMED value, the OCL value, a MATCH/DIFF column
and links to the concept in OCL and in the SNOMED browser, so anyone can open a row and look.

Inputs: the RF2 release, and the concepts and mappings downloaded from OCL by
check_ocl_against_snomed.py (output/snomed_ct/ocl_concepts.jsonl and ocl_mappings.jsonl).
Outputs (in --out; they contain SNOMED content, so keep them out of git):
  audit_concepts.csv       one row per concept in the loaded hierarchy and every concept in OCL
  audit_relationships.csv  one row per SNOMED relationship of a loaded concept (is-a and attributes)
  audit_summary.csv        counts of MATCH and DIFF per check

Usage:
  python audit_report.py "<RF2 release folder>"
"""
import argparse
import collections
import csv
import json
import os

from check_ocl_against_snomed import IS_A, Snomed, as_found, last_segment, without_tag

OCL_WEB = "https://app.openconceptlab.org/#{repo}concepts/{code}/"
BROWSER = "https://browser.ihtsdotools.org/?perspective=full&conceptId1={code}&edition=MAIN/2026-10-01&release=&languages=en"


def joined(values):
    return " | ".join(str(v) for v in values)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("release")
    ap.add_argument("--root", default="404684003")
    ap.add_argument("--repo", default="/orgs/HL7-Brasil/sources/SNOMED-CT/")
    ap.add_argument("--concepts", default="output/snomed_ct/ocl_concepts.jsonl")
    ap.add_argument("--mappings", default="output/snomed_ct/ocl_mappings.jsonl")
    ap.add_argument("--out", default="output/audit")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    ocl = {c["id"]: c for c in map(json.loads, open(args.concepts, encoding="utf-8"))}
    mappings = [json.loads(line) for line in open(args.mappings, encoding="utf-8")]
    snomed = Snomed(args.release)
    core = snomed.descendants(args.root) | {args.root}
    # What should be loaded: the hierarchy plus, repeatedly, every relationship target and every ancestor
    expected = set(core)
    while True:
        grown = expected | {t for c in expected for t in snomed.targets[c]}
        grown |= snomed.ancestors(grown)
        if grown == expected:
            break
        expected = grown
    summary = collections.Counter()

    # Concepts
    fields = ["fsn", "preferred", "synonyms", "class", "parents", "definition_status", "module",
              "effective_time", "gb_preferred", "concrete"]
    with open(os.path.join(args.out, "audit_concepts.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sctid", "in_hierarchy", "in_snomed_active", "in_ocl", "result"] +
                    [f"{side}_{field}" for field in fields for side in ("snomed", "ocl")] +
                    ["differences", "ocl_link", "snomed_browser_link"])
        for code in sorted(expected | ocl.keys(), key=int):
            in_snomed, in_ocl = code in snomed.concepts, code in ocl
            want = snomed.expected(code) if in_snomed else {}
            got = as_found(ocl[code]) if in_ocl else {}
            diffs = [field for field in fields if want.get(field) != got.get(field)] if in_snomed and in_ocl else []
            if not in_ocl:
                result = "MISSING FROM OCL"
            elif not in_snomed:
                result = "NOT ACTIVE IN SNOMED"
            elif code not in expected:
                result = "EXTRA IN OCL"
            else:
                result = "DIFF" if diffs else "MATCH"
            summary[("concept", result)] += 1
            row = [code, "yes" if code in core else "no (ancestor or target)", in_snomed, in_ocl, result]
            for field in fields:
                for side in (want, got):
                    value = side.get(field, "")
                    row.append(joined(value) if isinstance(value, list) else value)
            w.writerow(row + [joined(diffs), OCL_WEB.format(repo=args.repo, code=code), BROWSER.format(code=code)])

    # Relationships: is-a from parent links, attributes from mappings
    in_mapping = {}
    for m in mappings:
        extras = m.get("extras") or {}
        for rel_id in extras.get("relationship_ids") or []:
            in_mapping[rel_id] = m
    with open(os.path.join(args.out, "audit_relationships.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["relationship_id", "source_sctid", "source_name", "attribute", "target_sctid", "target_name",
                    "snomed_role_group", "ocl_mapping_id", "ocl_map_type", "ocl_from", "ocl_to", "ocl_role_groups",
                    "target_in_ocl", "result", "ocl_link", "snomed_browser_link"])
        name = lambda c: without_tag(snomed.fsn.get(c, c))
        # is-a: one row per active parent link of every concept that should be loaded
        for code in sorted(expected | ocl.keys(), key=int):
            got_parents = set(as_found(ocl[code])["parents"]) if code in ocl else set()
            for parent in sorted(snomed.parents[code], key=int):
                ok = parent in got_parents and parent in ocl
                summary[("is-a", "MATCH" if ok else "MISSING FROM OCL")] += 1
                w.writerow(["", code, name(code), "Is a", parent, name(parent), 0, "(parent link)", "Is a",
                            code if ok else "", parent if ok else "", "", parent in ocl,
                            "MATCH" if ok else "MISSING FROM OCL",
                            OCL_WEB.format(repo=args.repo, code=code), BROWSER.format(code=code)])
            for parent in sorted(got_parents - snomed.parents[code], key=int):
                summary[("is-a", "EXTRA IN OCL")] += 1
                w.writerow(["", code, name(code), "Is a", parent, name(parent), "", "(parent link)", "Is a", code, parent,
                            "", parent in ocl, "EXTRA IN OCL", OCL_WEB.format(repo=args.repo, code=code), BROWSER.format(code=code)])
        # attributes: one row per SNOMED relationship of a concept in OCL
        for rel_id, (source, attribute, target, group) in sorted(snomed.relationships.items(), key=lambda kv: (int(kv[1][0]), kv[1][3], kv[1][1])):
            if source not in expected:
                continue
            m = in_mapping.get(rel_id)
            if m is None:
                result, cols = "MISSING FROM OCL", ["", "", "", "", ""]
            else:
                extras = m.get("extras") or {}
                same = (last_segment(m["from_concept_url"]) == source and last_segment(m["to_concept_url"]) == target
                        and extras.get("attribute_id") == attribute and m["map_type"] == name(attribute)
                        and group in (extras.get("relationship_groups") or []) and not m.get("retired"))
                result = "MATCH" if same else "DIFF"
                cols = [m["id"], m["map_type"], last_segment(m["from_concept_url"]), last_segment(m["to_concept_url"]),
                        joined(extras.get("relationship_groups") or [])]
            summary[("attribute", result)] += 1
            w.writerow([rel_id, source, name(source), name(attribute), target, name(target), group] + cols +
                       [target in ocl, result, OCL_WEB.format(repo=args.repo, code=source), BROWSER.format(code=source)])
        known = set(snomed.relationships)
        for m in mappings:
            for rel_id in (m.get("extras") or {}).get("relationship_ids") or []:
                if rel_id not in known:
                    summary[("attribute", "EXTRA IN OCL")] += 1

    with open(os.path.join(args.out, "audit_summary.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["what", "result", "count"])
        for (what, result), n in sorted(summary.items()):
            w.writerow([what, result, n])
    for (what, result), n in sorted(summary.items()):
        print(f"{what:10} {result:22} {n:>9,}")
    print(f"\nWrote {args.out}/audit_concepts.csv, audit_relationships.csv, audit_summary.csv")


if __name__ == "__main__":
    main()
