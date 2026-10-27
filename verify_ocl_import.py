"""Check that an OCL source contains exactly what the import files describe.

Reads the files written by snomed_vaccines_to_ocl.py (what OCL should contain), fetches the
source, its concepts and its version from the OCL API (what OCL does contain), and compares:

  Source   name, description, visibility, canonical URL, locales, copyright, extras,
           hierarchy meaning and hierarchy root
  Concepts every concept present and nothing extra; class, datatype, retired, external ID,
           every name (text, locale, preferred, type), extras, parent links
  Version  exists, and holds the same concepts with the same names and parent links

Usage (Git Bash; the token comes from your OCL profile and is never printed):
  export OCL_TOKEN="your token"
  python verify_ocl_import.py
  unset OCL_TOKEN

Exit code 0 when every check passes, 1 otherwise. Only the Python standard library is needed.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PAGE_SIZE = 100
SOURCE_FIELDS = ["name", "full_name", "description", "source_type", "public_access", "default_locale",
                 "supported_locales", "canonical_url", "hierarchy_meaning", "copyright", "extras"]
CONCEPT_FIELDS = ["concept_class", "datatype", "retired", "external_id"]


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_expected(prefix):
    """The Source line, the concepts by id and the Source Version line from the import files."""
    lines = load_jsonl(f"{prefix}.jsonl")
    source = next(line for line in lines if line["type"] == "Source")
    concepts = {line["id"]: line for line in lines if line["type"] == "Concept"}
    version = next((line for line in load_jsonl(f"{prefix}_post_import.jsonl") if line["type"] == "Source Version"), None)
    return source, concepts, version


def last_segment(url):
    """'/orgs/X/sources/Y/concepts/123/' -> '123', so parent links compare by concept id."""
    return url.rstrip("/").rsplit("/", 1)[-1]


def without_none(value):
    """Drop None values from dicts, recursively: OCL may omit keys that were sent as null."""
    if isinstance(value, dict):
        return {k: without_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [without_none(v) for v in value]
    return value


def name_key(name):
    return (name["name"], name["locale"], bool(name.get("locale_preferred")), name.get("name_type"))


def compare_source(expected, actual, root_url):
    problems = []
    for field in SOURCE_FIELDS:
        want, got = without_none(expected.get(field)), without_none(actual.get(field))
        if want != got:
            problems.append(f"source {field}: expected {want!r}, OCL has {got!r}")
    if actual.get("hierarchy_root_url") != root_url:
        problems.append(f"source hierarchy_root_url: expected {root_url!r}, OCL has {actual.get('hierarchy_root_url')!r}")
    return problems


def compare_concept(expected, actual):
    code, problems = expected["id"], []
    for field in CONCEPT_FIELDS:
        if expected.get(field) != actual.get(field):
            problems.append(f"{code} {field}: expected {expected.get(field)!r}, OCL has {actual.get(field)!r}")

    want_names = {name_key(n) for n in expected["names"]}
    got_names = {name_key(n) for n in actual.get("names") or []}
    for name in sorted(want_names - got_names):
        problems.append(f"{code} name missing in OCL: {name}")
    for name in sorted(got_names - want_names):
        problems.append(f"{code} unexpected name in OCL: {name}")

    if without_none(expected.get("extras") or {}) != without_none(actual.get("extras") or {}):
        problems.append(f"{code} extras differ: expected {expected.get('extras')!r}, OCL has {actual.get('extras')!r}")

    want_parents = {last_segment(u) for u in expected.get("parent_concept_urls") or []}
    got_parents = {last_segment(u) for u in actual.get("parent_concept_urls") or []}
    if want_parents != got_parents:
        problems.append(f"{code} parents: expected {sorted(want_parents)}, OCL has {sorted(got_parents)}")
    return problems


def compare_concepts(expected, actual):
    """expected and actual map concept id -> concept; returns a list of problems."""
    problems = [f"{code} missing in OCL" for code in sorted(expected.keys() - actual.keys())]
    problems += [f"{code} in OCL but not in the import file" for code in sorted(actual.keys() - expected.keys())]
    for code in sorted(expected.keys() & actual.keys()):
        problems += compare_concept(expected[code], actual[code])
    return problems


class OCL:
    def __init__(self, api, token):
        self.api, self.token = api.rstrip("/"), token

    def get(self, path, params=None):
        url = f"{self.api}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        request = urllib.request.Request(url, headers={"Authorization": f"Token {self.token}", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    def concepts(self, repo_path):
        """All concepts of a source or source version, with names, extras and parent links."""
        found, page = {}, 1
        while True:
            batch = self.get(f"{repo_path}concepts/", {
                "verbose": "true", "includeParentConceptURLs": "true", "includeRetired": "true",
                "limit": PAGE_SIZE, "page": page})
            for concept in batch:
                found[concept["id"]] = concept
            if len(batch) < PAGE_SIZE:
                return found
            page += 1


def report(title, problems, show=20):
    print(f"{'PASS' if not problems else 'FAIL'} - {title}")
    for problem in problems[:show]:
        print(f"         {problem}")
    if len(problems) > show:
        print(f"         ... and {len(problems) - show} more")
    return not problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--files", default="output/snomed_vaccines_ocl", help="Import file prefix, as passed to --out")
    ap.add_argument("--api", default="https://api.openconceptlab.org", help="OCL API base URL")
    ap.add_argument("--skip-version", action="store_true", help="Do not check the Source Version")
    args = ap.parse_args()

    token = os.environ.get("OCL_TOKEN")
    if not token:
        sys.exit('Set your OCL API token first, e.g. in Git Bash: export OCL_TOKEN="your token" (export is needed so Python can see it)')

    source, expected, version = load_expected(args.files)
    repo = f"/orgs/{source['owner']}/sources/{source['id']}/"
    root = next(code for code, c in expected.items() if not c.get("parent_concept_urls"))
    root_url = f"{repo}concepts/{root}/"
    ocl = OCL(args.api, token)
    print(f"Checking {repo} against {args.files}.jsonl ({len(expected)} concepts)\n")

    try:
        results = [report("Source settings", compare_source(source, ocl.get(repo), root_url))]
        head = ocl.concepts(repo)
        print(f"       OCL HEAD has {len(head)} concepts")
        results.append(report("HEAD: concepts, names, extras and parent links", compare_concepts(expected, head)))

        if version and not args.skip_version:
            version_path = f"{repo}{version['id']}/"
            details = ocl.get(version_path)
            results.append(report(f"Version {version['id']} exists", []))
            if details.get("hierarchy_root_url") != root_url:
                print(f"NOTE - Version {version['id']} has no hierarchy root (it was created before the root was set)")
            snapshot = ocl.concepts(version_path)
            print(f"       {version['id']} has {len(snapshot)} concepts")
            results.append(report(f"Version {version['id']}: concepts, names, extras and parent links",
                                  compare_concepts(expected, snapshot)))
    except urllib.error.HTTPError as e:
        sys.exit(f"OCL API error {e.code} for {e.url}: {e.read().decode('utf-8', 'replace')[:300]}")

    print(f"\n{'ALL CHECKS PASSED' if all(results) else 'SOME CHECKS FAILED'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
