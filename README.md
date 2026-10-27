# SNOMED CT semantic search prototype in OCL

Loads a subset of SNOMED CT into a private repository in [OCL Online](https://app.openconceptlab.org) so the team can test semantic and cross-lingual search on it.

Part of the plan agreed between Bruna, Jon, OCL and HL7 Brasil:

1. **[Bruna]** Model and load a SNOMED CT subset into OCL Online in a private repo (content and hierarchy) ← *this repository*
2. **[Jon]** Trigger vectorization on the SNOMED subset
3. **[OCL]** Agree on the user group / permission structure
4. **[OCL, HL7, Bruna]** Test and verify the model, including semantic and cross-lingual search

## Current subset

| | |
|---|---|
| Release | SNOMED CT International Edition, 2026-10-01 (RF2 Snapshot) |
| Subset | `<< 787859002 \|Vaccine product (medicinal product)\|` |
| Size | 249 concepts, 759 names, 60 concepts with more than one parent |
| OCL destination | `/orgs/HL7-Brasil/sources/SNOMED-CT-Vaccines/` (private) |

## Licensing

SNOMED CT is licensed content. **This repository contains code only.** The RF2 release, and everything generated from it (`output/`), stays out of git — see [.gitignore](.gitignore). The OCL source is private, and everyone with access to the HL7-Brasil org must be covered by a SNOMED CT licence.

## Requirements

- Python 3.10+ (standard library only, nothing to install)
- A SNOMED CT International Edition RF2 release, unzipped
- An OCL account that is a member of the target organization

## Usage

```bash
python snomed_vaccines_to_ocl.py "path/to/SnomedCT_InternationalRF2_PRODUCTION_20261001T120000Z"
```

Options: `--root` (another subset root SCTID), `--org`, `--source`, `--out`, and `--only` to build a small test file from a few concepts of the subset:

```bash
python snomed_vaccines_to_ocl.py "path/to/release" --source SNOMED-CT-Vaccines-TEST --out output/test_snomed_vaccines_ocl --only 787859002,836368004,836369007,863950005,836377006,1181000221105
```

Writes to `output/`:

| File | Purpose |
|---|---|
| `snomed_vaccines_ocl.jsonl` | Bulk import: the Source and all Concepts |
| `snomed_vaccines_ocl_post_import.jsonl` | Bulk import, **after** the first file has finished and the hierarchy root is set: the Source Version |
| `snomed_vaccines_ocl.csv` | One row per concept, for human review before importing |

## How RF2 becomes OCL

| Step | RF2 file (Snapshot, `active = 1` only) | Result |
|---|---|---|
| 1 | `sct2_Concept` | Confirms the root concept is active |
| 2 | `sct2_Relationship`, type `116680003 \|Is a\|` | Walk down from the root → the subset, and each concept's parents |
| 3 | `sct2_Description` | Fully specified name and synonyms |
| 4 | `der2_cRefset_Language` (US `900000000000509007`, GB `900000000000508004`) | Which synonym is Preferred → OCL display name |
| 5 | `sct2_Relationship` (other types) and `sct2_RelationshipConcreteValues` | Active ingredients, role, dose form, counts → `extras` |
| 6 | — | OCL JSON Lines, parents written before children |

Mapping to OCL:

| SNOMED CT | OCL concept field |
|---|---|
| Concept ID | `id` (unchanged) |
| US English preferred synonym | `names[]`, `locale_preferred: true` |
| Fully specified name | `names[]`, `name_type: "Fully Specified"` |
| Other active synonyms (US and GB) | `names[]` |
| Semantic tag, e.g. `(medicinal product)` | `concept_class` |
| Is a relationships inside the subset | `parent_concept_urls` |
| Attributes, GB preferred term, effective time, module | `extras` |
| — (SNOMED has no datatype) | `datatype: "N/A"`, required by OCL; a vaccine product is not a question with a recorded value |
| OWL axioms, definition status, inactive history, description IDs | not carried over |

## Importing into OCL

1. Sign in to OCL and open **Bulk Import** (App menu, top right).
2. Upload `output/snomed_vaccines_ocl.jsonl` with **Update existing off** and **Hierarchy** ticked.
3. Wait until the import, including its child tasks, has finished. `created` should be 250 (1 Source + 249 Concepts). Lines in `others` failed validation; OCL does not say why.
4. Check in OCL: concept `787859002` has 5 children; `1181000221105` has parent `836377006`.
5. Set the hierarchy root with the API (the bulk importer cannot, and the web app has no field for it). In Git Bash, with your OCL API token from your OCL profile:

   ```bash
   OCL_TOKEN="your token"
   curl -s -X PATCH "https://api.openconceptlab.org/orgs/HL7-Brasil/sources/SNOMED-CT-Vaccines/" -H "Authorization: Token $OCL_TOKEN" -H "Content-Type: application/json" -d '{"hierarchy_root_url": "/orgs/HL7-Brasil/sources/SNOMED-CT-Vaccines/concepts/787859002/"}'
   unset OCL_TOKEN
   ```

   The reply should contain `"hierarchy_root_url":"/orgs/HL7-Brasil/sources/SNOMED-CT-Vaccines/concepts/787859002/"`. Clear the terminal history afterwards (`history -c`).
6. Upload `output/snomed_vaccines_ocl_post_import.jsonl` to create the source version. A version copies the hierarchy root from HEAD when it is created, so do this after step 5.

## Verifying the import

[verify_ocl_import.py](verify_ocl_import.py) compares the import files with what OCL actually holds, through the OCL API:

| Checked | What |
|---|---|
| Source | name, description, visibility, canonical URL, locales, copyright, extras, hierarchy meaning and root |
| HEAD concepts | every concept present and none extra; class, datatype, retired, external ID; every name (text, locale, preferred, type); extras; parent links |
| Source Version | exists, and holds the same concepts, names, extras and parent links |

```bash
export OCL_TOKEN="your token"
python verify_ocl_import.py
unset OCL_TOKEN
```

It prints PASS or FAIL per check with the differences, and exits with 1 if anything fails. For the test source: `python verify_ocl_import.py --files output/test_snomed_vaccines_ocl --skip-version`.

The checker has its own unit tests, which use made-up concepts and need no OCL access:

```bash
python -m unittest discover tests
```

## OCL behaviour this relies on

- The parallel importer links `parent_concept_urls` in a final reconciliation pass, after every line of the file has been processed, so concept order does not matter — but a Source Version in the same file would be created before the hierarchy exists. Hence the separate version file. (`oclapi2` `core/importers/models.py`, `BulkImportParallelRunner`)
- Validation errors land in the `others` bucket without a message; validate before uploading.
- The bulk importer never updates an existing Source: `clean()` returns early when the Source exists, so it is reported as `exists` even with Update existing on. That setting only affects concepts and mappings.
- The bulk importer drops `hierarchy_root_url`: it is not in `SourceImporter.allowed_fields`. It can only be set with an API update of the source, which checks the root concept exists in the same source (`core/sources/serializers.py`, `Source.clean()`). [icd10cm-extended](https://github.com/filiperochalopes/icd10cm-extended) does the same with a `PATCH`.
- A Source Version copies `hierarchy_root_url` from HEAD when it is created (`Source.update_version_data`).
- OCL's own SNOMED GPS load (`/orgs/SNOMED-International/sources/SNOMED-GPS/`) is a flat list: no hierarchy, synonyms or attributes, because the GPS release has none. This source is built from the full International Edition instead, which is why it must stay private.
- The IPS FHIR guide's value set "Vaccines - IPS" is `descendent-of 787859002` (plus 787482006 |No known immunizations|), so this subset matches the SNOMED part of IPS vaccines.

## References

- [OCL Bulk Import API](https://docs.openconceptlab.org/en/latest/oclapi/apireference/bulkimporting.html)
- [filiperochalopes/ocl-skills](https://github.com/filiperochalopes/ocl-skills) — tested OCL bulk-import contract
- [filiperochalopes/icd10cm-extended](https://github.com/filiperochalopes/icd10cm-extended) — release → OCL pipeline for ICD-10-CM
- OCL's own SNOMED loads: [OpenConceptLab/ocl_issues#2912](https://github.com/OpenConceptLab/ocl_issues/issues/2912), SNOMED GPS canonical URL in [#2555](https://github.com/OpenConceptLab/ocl_issues/issues/2555)
- Semantic search test method: [OpenConceptLab/ocl_issues#2472](https://github.com/OpenConceptLab/ocl_issues/issues/2472)

## Open items

- [x] Decide the source's `canonical_url`: keep `http://snomed.info/sct` (OCL's SNOMED GPS subset uses its own)
- [x] Test import of 6 concepts into `SNOMED-CT-Vaccines-TEST`: 7 created, 0 errors, parents and two-parent concept correct
- [x] Import the full subset (2026-10-27): 249 concepts, root has 5 children, version `v20261001` created (249 concepts, hierarchy included)
- [x] Set the hierarchy root on HEAD with an API PATCH. `v20261001` was created before it, so that version has no root; recreate the version if it is needed there
- [ ] Delete `SNOMED-CT-Vaccines-TEST` in OCL when no longer needed
- [ ] Hand over to Jon for vectorization
- [ ] Cross-lingual testing needs Portuguese or Spanish terms (not in the International Edition)
