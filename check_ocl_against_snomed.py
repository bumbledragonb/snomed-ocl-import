"""Check every concept in the OCL source against the SNOMED CT RF2 release itself.

This does not use the import files or snomed_to_ocl.py: the expected content is worked out again,
straight from the RF2 Snapshot, with the rules below written in SNOMED terms. So it also catches
mistakes made when the import files were built, not only mistakes made by OCL.

Which concepts (membership)
  - every active descendant of the loaded root (default 404684003 |Clinical finding|) is in OCL
  - every concept in OCL is an active SNOMED concept
  - every concept outside the root is there for a reason: an ancestor of a loaded concept, or
    the target of a (non is-a) relationship of a loaded concept
  - nothing is missing: every is-a parent and every relationship target of a loaded concept is loaded

Each concept
  - not retired
  - class        = the FSN semantic tag
  - FSN          = the active Fully Specified Name
  - preferred    = the US English preferred synonym (or the FSN without its tag when there is none)
  - synonyms     = every active synonym that is acceptable or preferred in US or GB English
  - parents      = every active inferred is-a parent
  - extras       = SNOMED effective time, module, definition status, GB preferred term, concrete values

Relationships (with --mappings)
  - every active non is-a relationship of a loaded concept is in exactly one mapping; one mapping holds
    the same attribute and target repeated in several role groups (relationship_ids, relationship_groups)
  - each mapping's source, attribute (map type), target and role groups match SNOMED
  - no two mappings share from + map type + to (OCL rejects those)

Usage (Git Bash, from the project folder; the token is never printed):
  export OCL_TOKEN="your token"
  python check_ocl_against_snomed.py --mappings "C:/Users/brotz/Favorites/Downloads/SnomedCT_InternationalRF2_PRODUCTION_20261001T120000Z/SnomedCT_InternationalRF2_PRODUCTION_20261001T120000Z"
  unset OCL_TOKEN

The concepts downloaded from OCL are saved to output/snomed_ct/ocl_concepts.jsonl; add --cached to
check that copy again without downloading. --against-files checks the import files instead of OCL
(no token needed). Only the Python standard library is needed.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

IS_A = "116680003"
FSN = "900000000000003001"
SYNONYM = "900000000000013009"
US_ENGLISH = "900000000000509007"
GB_ENGLISH = "900000000000508004"
PREFERRED = "900000000000548007"
FULLY_DEFINED = "900000000000073002"
ROOT = "138875005"


# ---------- SNOMED side ----------

def active_rows(release, pattern):
    """Active rows of the one Snapshot file matching pattern."""
    files = glob.glob(os.path.join(release, "Snapshot", "**", pattern), recursive=True)
    if len(files) != 1:
        sys.exit(f"Expected one RF2 file matching {pattern} in {release}, found {len(files)}")
    with open(files[0], encoding="utf-8") as f:
        header = next(f).rstrip("\n").split("\t")
        active = header.index("active")
        for line in f:
            row = line.rstrip("\n").split("\t")
            if row[active] == "1":
                yield row


def tag(fsn):
    m = re.search(r"\(([^()]+)\)\s*$", fsn)
    return m.group(1) if m else ""


def without_tag(fsn):
    return re.sub(r"\s*\([^()]+\)$", "", fsn)


class Snomed:
    def __init__(self, release):
        print("Reading SNOMED CT RF2 ...", flush=True)
        # concept: id effectiveTime active moduleId definitionStatusId
        self.concepts = {r[0]: r for r in active_rows(release, "sct2_Concept_Snapshot_*.txt")}
        # relationship: id effectiveTime active moduleId sourceId destinationId relationshipGroup typeId ...
        self.parents = collections.defaultdict(set)
        self.children = collections.defaultdict(set)
        self.targets = collections.defaultdict(set)
        self.relationships = {}  # relationship id -> (source, type, destination, group)
        for r in active_rows(release, "sct2_Relationship_Snapshot_*.txt"):
            if r[7] == IS_A:
                self.parents[r[4]].add(r[5])
                self.children[r[5]].add(r[4])
            else:
                self.targets[r[4]].add(r[5])
                self.relationships[r[0]] = (r[4], r[7], r[5], int(r[6]))
        # concrete value: id effectiveTime active moduleId sourceId value relationshipGroup typeId ...
        self.concrete = collections.defaultdict(set)
        for r in active_rows(release, "sct2_RelationshipConcreteValues_Snapshot_*.txt"):
            self.concrete[r[4]].add((r[7], r[5].lstrip("#")))
        # description: id effectiveTime active moduleId conceptId languageCode typeId term ...
        self.fsn, self.synonyms = {}, collections.defaultdict(dict)
        for r in active_rows(release, "sct2_Description_Snapshot-en_*.txt"):
            if r[6] == FSN:
                self.fsn[r[4]] = r[7]
            elif r[6] == SYNONYM:
                self.synonyms[r[4]][r[0]] = r[7]
        # language refset: id effectiveTime active moduleId refsetId referencedComponentId acceptabilityId
        self.acceptable, self.us_preferred, self.gb_preferred = set(), {}, {}
        description_concept = {d: c for c, ds in self.synonyms.items() for d in ds}
        for r in active_rows(release, "der2_cRefset_LanguageSnapshot-en_*.txt"):
            concept = description_concept.get(r[5])
            if concept is None or r[4] not in (US_ENGLISH, GB_ENGLISH):
                continue
            self.acceptable.add(r[5])
            if r[6] == PREFERRED:
                (self.us_preferred if r[4] == US_ENGLISH else self.gb_preferred)[concept] = self.synonyms[concept][r[5]]

    def descendants(self, code):
        found, stack = set(), [code]
        while stack:
            for child in self.children[stack.pop()]:
                if child not in found:
                    found.add(child)
                    stack.append(child)
        return found

    def ancestors(self, codes):
        found, stack = set(), list(codes)
        while stack:
            for parent in self.parents[stack.pop()]:
                if parent not in found:
                    found.add(parent)
                    stack.append(parent)
        return found

    def expected(self, code):
        """What OCL should hold for one concept, in a form that compares directly with OCL's."""
        row, fsn = self.concepts[code], self.fsn[code]
        preferred = self.us_preferred.get(code) or without_tag(fsn)
        synonyms = {term for d, term in self.synonyms[code].items() if d in self.acceptable}
        return {
            "class": tag(fsn).capitalize(), "retired": False, "fsn": fsn, "preferred": preferred,
            "synonyms": sorted(synonyms | {preferred}), "parents": sorted(self.parents[code]),
            "effective_time": row[1], "module": row[3],
            "definition_status": "defined" if row[4] == FULLY_DEFINED else "primitive",
            "gb_preferred": self.gb_preferred.get(code), "concrete": sorted(self.concrete[code]),
        }


# ---------- OCL side ----------

def last_segment(url):
    return url.rstrip("/").rsplit("/", 1)[-1]


def as_found(concept):
    """An OCL concept (from the API or an import file) in the same form as Snomed.expected()."""
    names = concept.get("names") or []
    extras = concept.get("extras") or {}
    fsns = [n["name"] for n in names if n.get("name_type") == "Fully Specified"]
    preferred = [n["name"] for n in names if n.get("locale_preferred")]
    return {
        "class": concept.get("concept_class"), "retired": bool(concept.get("retired")),
        "fsn": fsns[0] if len(fsns) == 1 else fsns, "preferred": preferred[0] if len(preferred) == 1 else preferred,
        "synonyms": sorted({n["name"] for n in names if n.get("name_type") != "Fully Specified"}),
        "parents": sorted(last_segment(u) for u in concept.get("parent_concept_urls") or []),
        "effective_time": extras.get("snomed_effective_time"), "module": extras.get("snomed_module"),
        "definition_status": extras.get("snomed_definition_status"),
        "gb_preferred": extras.get("snomed_gb_preferred_term"),
        "concrete": sorted((v["type_id"], str(v["value"])) for v in extras.get("snomed_concrete_values") or []),
    }


def api_get(api, path, token, params):
    url = f"{api}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Token {token}", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response), response.headers


def download(api, repo, token, page_size, kind="concepts"):
    """Every concept (with names, extras and parent links) or every mapping in the source (HEAD)."""
    found, page, total = {}, 1, None
    params = {"verbose": "true", "includeParentConceptURLs": "true", "includeRetired": "true", "limit": page_size}
    while True:
        batch, headers = api_get(api, f"{repo}{kind}/", token, {**params, "page": page})
        total = int(headers.get("num_found") or total or 0)
        for item in batch:
            found[item["id"]] = item
        print(f"\r  downloaded {len(found):,} of {total:,} {kind}", end="", flush=True)
        if not batch or len(found) >= total:
            print()
            return found
        page += 1


def fetch_one(api, repo, token, code):
    try:
        concept, _ = api_get(api, f"{repo}concepts/{code}/", token,
                             {"verbose": "true", "includeParentConceptURLs": "true"})
        return concept
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


# ---------- comparison ----------

def check_membership(snomed, loaded_root, in_ocl):
    problems = collections.defaultdict(list)
    core = snomed.descendants(loaded_root) | {loaded_root}
    for code in sorted(core - in_ocl):
        problems["descendant of the root missing from OCL"].append(code)
    for code in sorted(in_ocl - snomed.concepts.keys()):
        problems["in OCL but not an active SNOMED concept"].append(code)
    loaded = in_ocl & snomed.concepts.keys()
    reasons = snomed.ancestors(loaded) | {t for c in loaded for t in snomed.targets[c]}
    for code in sorted(loaded - core - reasons):
        problems["in OCL with no reason (not an ancestor or relationship target)"].append(code)
    for code in sorted(loaded):
        for parent in snomed.parents[code] - in_ocl:
            problems["is-a parent not loaded"].append(f"{code} -> {parent}")
        for target in snomed.targets[code] - in_ocl:
            problems["relationship target not loaded"].append(f"{code} -> {target}")
    return core, problems


def check_concepts(snomed, ocl):
    problems = collections.defaultdict(list)
    for code in sorted(ocl.keys() & snomed.concepts.keys()):
        want, got = snomed.expected(code), as_found(ocl[code])
        for field, value in want.items():
            if got[field] != value:
                problems[field].append(f"{code}: SNOMED {value!r}, OCL {got[field]!r}")
    return problems


def check_mappings(snomed, loaded, mappings):
    """Every non is-a relationship of a loaded concept is in exactly one mapping, and every mapping is right."""
    problems = collections.defaultdict(list)
    covered, pairs = collections.Counter(), collections.Counter()
    for m in mappings:
        extras = m.get("extras") or {}
        source, target = last_segment(m["from_concept_url"]), last_segment(m["to_concept_url"])
        attribute = extras.get("attribute_id")
        ids, groups = extras.get("relationship_ids") or [], extras.get("relationship_groups") or []
        pairs[(source, m["map_type"], target)] += 1
        if m.get("retired"):
            problems["retired mapping"].append(m["id"])
        if not ids or m["id"] != ids[0]:
            problems["mapping id is not its first relationship id"].append(f"{m['id']} {ids}")
        if attribute not in snomed.fsn or m["map_type"] != without_tag(snomed.fsn[attribute]):
            problems["map type is not the attribute's name"].append(f"{m['id']}: {m['map_type']!r} / {attribute}")
        for rel_id in ids:
            covered[rel_id] += 1
            want = snomed.relationships.get(rel_id)
            if want is None:
                problems["not an active SNOMED relationship"].append(rel_id)
            elif want[:3] != (source, attribute, target):
                problems["source, attribute or target differs from SNOMED"].append(f"{rel_id}: SNOMED {want[:3]}, OCL {(source, attribute, target)}")
        if sorted(groups) != sorted(snomed.relationships[i][3] for i in ids if i in snomed.relationships):
            problems["role groups differ from SNOMED"].append(f"{m['id']}: OCL {groups}")
    for rel_id, (source, *_rest) in snomed.relationships.items():
        if source in loaded and not covered[rel_id]:
            problems["SNOMED relationship missing"].append(rel_id)
    problems["relationship in more than one mapping"] += [r for r, n in covered.items() if n > 1]
    problems["same from + map type + to twice (OCL rejects these)"] += [str(k) for k, n in pairs.items() if n > 1]
    return {k: v for k, v in problems.items() if v}


def show(title, problems, examples=5):
    count = sum(len(v) for v in problems.values())
    print(f"{'PASS' if not count else 'FAIL'} - {title}")
    for kind, items in problems.items():
        print(f"         {len(items):,} {kind}")
        for item in items[:examples]:
            print(f"             {item[:300]}")
    return not count


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("release", help="RF2 release folder (the one containing Snapshot/)")
    ap.add_argument("--root", default="404684003", help="The hierarchy that was loaded")
    ap.add_argument("--repo", default="/orgs/HL7-Brasil/sources/SNOMED-CT/")
    ap.add_argument("--api", default="https://api.openconceptlab.org")
    ap.add_argument("--page-size", type=int, default=1000)
    ap.add_argument("--save", default="output/snomed_ct/ocl_concepts.jsonl", help="Where to keep the downloaded concepts")
    ap.add_argument("--cached", action="store_true", help="Check the concepts (and mappings) downloaded last time")
    ap.add_argument("--mappings", action="store_true", help="Also check the relationships (OCL mappings)")
    ap.add_argument("--save-mappings", default="output/snomed_ct/ocl_mappings.jsonl")
    ap.add_argument("--against-files", help="Check this folder's 02_concepts_part*.jsonl instead of OCL")
    args = ap.parse_args()

    token = os.environ.get("OCL_TOKEN")
    if args.against_files:
        ocl = {}
        for path in sorted(glob.glob(os.path.join(args.against_files, "02_concepts_part*.jsonl"))):
            for line in open(path, encoding="utf-8"):
                c = json.loads(line)
                ocl[c["id"]] = c
        label = f"import files in {args.against_files}"
    elif args.cached:
        ocl = {c["id"]: c for c in map(json.loads, open(args.save, encoding="utf-8"))}
        label = f"OCL concepts saved in {args.save}"
    else:
        if not token:
            sys.exit('Set your OCL API token first, in Git Bash: export OCL_TOKEN="your token"')
        print(f"Downloading every concept in {args.repo} ...")
        try:
            ocl = download(args.api, args.repo, token, args.page_size)
        except urllib.error.HTTPError as e:
            sys.exit(f"OCL API error {e.code} for {e.url}: {e.read().decode('utf-8', 'replace')[:300]}")
        os.makedirs(os.path.dirname(args.save), exist_ok=True)
        with open(args.save, "w", encoding="utf-8") as f:
            for c in ocl.values():
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        label = f"OCL {args.repo} (saved to {args.save})"

    mappings = None
    if args.mappings and args.against_files:
        mappings = [json.loads(line) for path in sorted(glob.glob(os.path.join(args.against_files, "03_mappings_part*.jsonl")))
                    for line in open(path, encoding="utf-8")]
    elif args.mappings and args.cached:
        mappings = [json.loads(line) for line in open(args.save_mappings, encoding="utf-8")]
    elif args.mappings:
        print(f"Downloading every mapping in {args.repo} ...")
        try:
            mappings = list(download(args.api, args.repo, token, args.page_size, "mappings").values())
        except urllib.error.HTTPError as e:
            sys.exit(f"OCL API error {e.code} for {e.url}: {e.read().decode('utf-8', 'replace')[:300]}")
        with open(args.save_mappings, "w", encoding="utf-8") as f:
            for m in mappings:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

    snomed = Snomed(args.release)
    core, membership = check_membership(snomed, args.root, set(ocl))

    # A concept the list did not return may still be in OCL: ask for it on its own before calling it missing.
    missing = membership.get("descendant of the root missing from OCL", [])
    if missing and token and not args.against_files and len(missing) <= 500:
        print(f"Asking OCL for the {len(missing)} concepts the list did not return ...")
        for code in missing:
            concept = fetch_one(args.api, args.repo, token, code)
            if concept:
                ocl[code] = concept
        core, membership = check_membership(snomed, args.root, set(ocl))

    print(f"\nChecking {label} against SNOMED CT")
    print(f"       {len(ocl):,} concepts: {len(core & set(ocl)):,} in the {args.root} hierarchy, "
          f"{len(set(ocl) - core):,} ancestors and relationship targets outside it\n")
    results = [show("Membership: the right concepts, none missing, none extra", membership),
               show("Every concept: class, names, parents and extras match SNOMED", check_concepts(snomed, ocl)),
               show(f"Top concept {ROOT} is loaded", {} if ROOT in ocl else {"missing": [ROOT]})]
    if mappings is not None:
        print(f"\n       {len(mappings):,} mappings holding {sum(len((m.get('extras') or {}).get('relationship_ids') or []) for m in mappings):,} "
              f"SNOMED relationships")
        results.append(show("Relationships: every SNOMED relationship in exactly one mapping, all correct",
                            check_mappings(snomed, set(ocl), mappings)))
    print(f"\n{'ALL CHECKS PASSED' if all(results) else 'SOME CHECKS FAILED'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
