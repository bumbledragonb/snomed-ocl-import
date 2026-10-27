"""Unit tests for verify_ocl_import.py, run without OCL.

The concepts here are made up, so no SNOMED CT content is stored in the repository.
Run from the project folder:  python -m unittest discover tests
"""
import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import verify_ocl_import as v  # noqa: E402

REPO = "/orgs/TestOrg/sources/TestSource/"


def concept(code, name, parents=(), extras=None):
    return {
        "type": "Concept", "id": code, "external_id": code, "owner": "TestOrg", "owner_type": "Organization",
        "source": "TestSource", "concept_class": "Medicinal product", "datatype": "N/A", "retired": False,
        "names": [{"name": name, "locale": "en", "locale_preferred": True, "name_type": "Synonym"},
                  {"name": f"{name} (test)", "locale": "en", "locale_preferred": False, "name_type": "Fully Specified"}],
        "descriptions": None,
        "extras": extras if extras is not None else {"test_module": "1", "test_attributes": [{"group": 0, "value": "x"}]},
        **({"parent_concept_urls": [f"{REPO}concepts/{p}/" for p in parents]} if parents else {}),
    }


SOURCE = {
    "type": "Source", "id": "TestSource", "owner": "TestOrg", "owner_type": "Organization",
    "name": "Test source", "full_name": "Test source full", "description": "For tests", "source_type": "External",
    "public_access": "None", "default_locale": "en", "supported_locales": ["en"],
    "canonical_url": "http://example.org/cs", "hierarchy_meaning": "is-a", "copyright": "none",
    "custom_validation_schema": "None", "extras": {"release": "1"},
}
EXPECTED = {
    "1": concept("1", "Test root"),
    "2": concept("2", "Test child A", parents=["1"]),
    "3": concept("3", "Test child B", parents=["1"]),
    "4": concept("4", "Test grandchild", parents=["2", "3"]),
}
ROOT_URL = f"{REPO}concepts/1/"


def as_ocl(c):
    """What the OCL API returns for a concept: extra fields, and parent URLs it built itself."""
    returned = copy.deepcopy(c)
    returned.update({"uuid": "u" + c["id"], "url": f"{REPO}concepts/{c['id']}/", "display_name": c["names"][0]["name"]})
    for n in returned["names"]:
        n.update({"uuid": "n", "type": "ConceptName", "external_id": None})
    return returned


class CompareConceptsTest(unittest.TestCase):
    def setUp(self):
        self.actual = {code: as_ocl(c) for code, c in EXPECTED.items()}

    def test_identical_import_passes(self):
        self.assertEqual(v.compare_concepts(EXPECTED, self.actual), [])

    def test_missing_concept_is_reported(self):
        del self.actual["3"]
        self.assertEqual(v.compare_concepts(EXPECTED, self.actual), ["3 missing in OCL"])

    def test_concept_not_in_file_is_reported(self):
        self.actual["99"] = as_ocl(concept("99", "Stray"))
        self.assertEqual(v.compare_concepts(EXPECTED, self.actual), ["99 in OCL but not in the import file"])

    def test_missing_synonym_is_reported(self):
        self.actual["2"]["names"].pop()
        problems = v.compare_concepts(EXPECTED, self.actual)
        self.assertEqual(len(problems), 1)
        self.assertIn("2 name missing in OCL", problems[0])

    def test_changed_preferred_name_is_reported(self):
        self.actual["2"]["names"][0]["locale_preferred"] = False
        problems = v.compare_concepts(EXPECTED, self.actual)
        self.assertTrue(any("name missing" in p for p in problems))
        self.assertTrue(any("unexpected name" in p for p in problems))

    def test_missing_parent_link_is_reported(self):
        self.actual["4"]["parent_concept_urls"] = [f"{REPO}concepts/2/"]
        self.assertEqual(v.compare_concepts(EXPECTED, self.actual), ["4 parents: expected ['2', '3'], OCL has ['2']"])

    def test_parent_links_compare_by_concept_id_not_full_url(self):
        self.actual["4"]["parent_concept_urls"] = ["/orgs/TestOrg/sources/TestSource/concepts/3",
                                                   "/orgs/TestOrg/sources/TestSource/concepts/2/"]
        self.assertEqual(v.compare_concepts(EXPECTED, self.actual), [])

    def test_hierarchy_never_linked_is_reported(self):
        for c in self.actual.values():
            c.pop("parent_concept_urls", None)
        problems = v.compare_concepts(EXPECTED, self.actual)
        self.assertEqual(len(problems), 3)

    def test_changed_field_is_reported(self):
        self.actual["1"]["datatype"] = "None"
        self.assertEqual(v.compare_concepts(EXPECTED, self.actual), ["1 datatype: expected 'N/A', OCL has 'None'"])

    def test_retired_concept_is_reported(self):
        self.actual["3"]["retired"] = True
        self.assertEqual(v.compare_concepts(EXPECTED, self.actual), ["3 retired: expected False, OCL has True"])

    def test_changed_extras_are_reported(self):
        self.actual["2"]["extras"]["test_attributes"][0]["value"] = "y"
        problems = v.compare_concepts(EXPECTED, self.actual)
        self.assertEqual(len(problems), 1)
        self.assertIn("2 extras differ", problems[0])

    def test_null_extras_values_may_be_dropped_by_ocl(self):
        expected = {"5": concept("5", "Test", extras={"kept": "1", "empty": None})}
        actual = {"5": as_ocl(concept("5", "Test", extras={"kept": "1"}))}
        self.assertEqual(v.compare_concepts(expected, actual), [])


class CompareSourceTest(unittest.TestCase):
    def setUp(self):
        self.actual = {**copy.deepcopy(SOURCE), "uuid": "1", "url": REPO, "hierarchy_root_url": ROOT_URL}

    def test_identical_source_passes(self):
        self.assertEqual(v.compare_source(SOURCE, self.actual, ROOT_URL), [])

    def test_missing_hierarchy_root_is_reported(self):
        self.actual["hierarchy_root_url"] = None
        self.assertEqual(len(v.compare_source(SOURCE, self.actual, ROOT_URL)), 1)

    def test_source_made_public_is_reported(self):
        self.actual["public_access"] = "View"
        self.assertEqual(v.compare_source(SOURCE, self.actual, ROOT_URL),
                         ["source public_access: expected 'None', OCL has 'View'"])

    def test_changed_source_extras_are_reported(self):
        self.actual["extras"] = {"release": "2"}
        self.assertEqual(len(v.compare_source(SOURCE, self.actual, ROOT_URL)), 1)


class FakeOCL(v.OCL):
    """Serves concept pages from memory instead of the OCL API."""

    def __init__(self, concepts):
        super().__init__("https://example.org", "unused")
        self.all, self.requests = list(concepts), []

    def get(self, path, params=None):
        self.requests.append((path, params))
        start = (params["page"] - 1) * params["limit"]
        return self.all[start:start + params["limit"]]


class PagingTest(unittest.TestCase):
    def test_reads_every_page(self):
        many = [as_ocl(concept(str(i), f"Test {i}")) for i in range(v.PAGE_SIZE * 2 + 5)]
        ocl = FakeOCL(many)
        self.assertEqual(len(ocl.concepts(REPO)), v.PAGE_SIZE * 2 + 5)
        self.assertEqual(len(ocl.requests), 3)

    def test_asks_for_names_extras_parents_and_retired(self):
        ocl = FakeOCL([])
        ocl.concepts(REPO)
        path, params = ocl.requests[0]
        self.assertEqual(path, f"{REPO}concepts/")
        for param in ("verbose", "includeParentConceptURLs", "includeRetired"):
            self.assertEqual(params[param], "true")


class LoadExpectedTest(unittest.TestCase):
    def test_reads_source_concepts_and_version_from_import_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = os.path.join(tmp, "test")
            with open(f"{prefix}.jsonl", "w", encoding="utf-8") as f:
                for line in [SOURCE, *EXPECTED.values()]:
                    f.write(json.dumps(line) + "\n")
            with open(f"{prefix}_post_import.jsonl", "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "Source Version", "id": "v1"}) + "\n")
            source, concepts, version = v.load_expected(prefix)
        self.assertEqual(source["id"], "TestSource")
        self.assertEqual(sorted(concepts), ["1", "2", "3", "4"])
        self.assertEqual(version["id"], "v1")


if __name__ == "__main__":
    unittest.main()
