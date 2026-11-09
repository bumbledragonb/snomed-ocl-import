"""Score an OCL Mapper run against a table of correct mappings.

Inputs:
  --gold    JSON list of rows with 'Código', 'Imunobiológico', 'SNOMED CT', 'Descrição SNOMED CT', 'Tipo'
            (made from the PNI -> SNOMED spreadsheet)
  --mapper  the Mapper project's CSV download (Code, Name, __result_<algo>_NN__, __oclai_*__, __map_*__)
  --ocl     the OCL import file, used to tell near misses (parent or child of the right answer) from misses
  --out     Excel file with a summary sheet and one row per input term

Semantic search is scored by the rank of the right answer among the candidates (hit@1/3/5/15, MRR).
The OCL AI Assistant is scored on the rows where it answered. Needs openpyxl.
"""
import argparse
import collections
import csv
import json
import re

import openpyxl
from openpyxl.styles import Font, PatternFill


def candidates(row):
    """[(code, name, unified score)] in rank order from the __result_*__ columns."""
    found = []
    for key in sorted(k for k in row if k.startswith("__result_")):
        text = row[key]
        if not text:
            continue
        first, *rest = text.split("\n")
        code, _, name = first.partition(":")
        score = next((float(m.group(1)) for line in rest if (m := re.match(r"Unified Score: ([\d.]+)", line))), None)
        found.append((code.strip(), name.strip(), score))
    return found


def load_hierarchy(path):
    parents = {}
    for line in open(path, encoding="utf-8"):
        d = json.loads(line)
        if d["type"] == "Concept":
            parents[d["id"]] = {u.rstrip("/").rsplit("/", 1)[-1] for u in d.get("parent_concept_urls") or []}
    return parents


def ancestors(code, parents):
    seen, stack = set(), [code]
    while stack:
        for p in parents.get(stack.pop(), ()):
            if p not in seen:
                seen.add(p)
                stack.append(p)
    return seen


def relation(found, gold, parents):
    """How a suggested code relates to the right answer."""
    if not found:
        return "none"
    if found == gold:
        return "exact"
    if found in ancestors(gold, parents):
        return "broader (ancestor)"
    if gold in ancestors(found, parents):
        return "narrower (descendant)"
    return "wrong"


def score(gold_rows, mapper_rows, parents):
    by_code = {str(r.get("Code") or r.get("id")): r for r in mapper_rows}  # uploaded code column, or OCL export's id
    results = []
    for g in gold_rows:
        row = by_code[str(g["Código"])]
        gold = str(g["SNOMED CT"])
        cands = candidates(row)
        codes = [c[0] for c in cands]
        rank = codes.index(gold) + 1 if gold in codes else None
        ai_status = row.get("__status_recommend__", "")
        ai_pick = row.get("__oclai_rec_concept_id__", "")
        results.append({
            "code": g["Código"], "name": g["Imunobiológico"], "map_type": g["Tipo"],
            "gold": gold, "gold_name": g["Descrição SNOMED CT"],
            "combination": " and " in g["Descrição SNOMED CT"],
            "rank": rank, "n_candidates": len(cands),
            "top1": codes[0] if codes else "", "top1_name": cands[0][1] if cands else "",
            "top1_score": cands[0][2] if cands else None,
            "top1_relation": relation(codes[0] if codes else "", gold, parents),
            "ai_status": ai_status, "ai_assessment": row.get("__oclai_assessment__", ""),
            "ai_confidence": row.get("__oclai_confidence_score__", ""),
            "ai_pick": ai_pick, "ai_pick_name": row.get("__oclai_rec_concept_name__", ""),
            "ai_relation": relation(ai_pick, gold, parents) if ai_pick else "no answer",
            "ai_rationale": row.get("__oclai_rationale__", ""),
            "proposed": row.get("__map_concept_id__", ""),
        })
    return results


def summarise(results, label):
    n = len(results)
    hit = lambda k: sum(1 for r in results if r["rank"] and r["rank"] <= k)
    mrr = sum(1 / r["rank"] for r in results if r["rank"]) / n if n else 0
    answered = [r for r in results if r["ai_pick"]]
    ai = collections.Counter(r["ai_relation"] for r in answered)
    rel = collections.Counter(r["top1_relation"] for r in results)
    return {
        "group": label, "rows": n,
        "hit@1": hit(1), "hit@3": hit(3), "hit@5": hit(5), "hit@15": hit(15), "not in top 15": n - hit(15),
        "MRR": round(mrr, 3),
        "#1 is broader": rel["broader (ancestor)"], "#1 is narrower": rel["narrower (descendant)"],
        "AI answered": len(answered), "AI exact": ai["exact"],
        "AI broader": ai["broader (ancestor)"], "AI narrower": ai["narrower (descendant)"], "AI wrong": ai["wrong"],
    }


def write_excel(results, summaries, path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    head = list(summaries[0])
    ws.append(head)
    for s in summaries:
        ws.append([s[h] for h in head])
    for cell in ws[1]:
        cell.font = Font(bold=True)

    rows = wb.create_sheet("Per term")
    cols = ["code", "name", "map_type", "gold", "gold_name", "rank", "top1", "top1_name", "top1_score",
            "top1_relation", "ai_assessment", "ai_confidence", "ai_pick", "ai_pick_name", "ai_relation", "ai_status",
            "proposed", "ai_rationale"]
    rows.append(cols)
    for cell in rows[1]:
        cell.font = Font(bold=True)
    colour = {"exact": "C6EFCE", "broader (ancestor)": "FFEB9C", "narrower (descendant)": "FFEB9C", "wrong": "FFC7CE"}
    for r in sorted(results, key=lambda r: (r["rank"] or 99, r["code"])):
        rows.append([r[c] if r[c] is not None else "" for c in cols])
        fill = colour.get(r["top1_relation"])
        if fill:
            rows.cell(rows.max_row, cols.index("top1_relation") + 1).fill = PatternFill("solid", fgColor=fill)
        fill = colour.get(r["ai_relation"])
        if fill:
            rows.cell(rows.max_row, cols.index("ai_relation") + 1).fill = PatternFill("solid", fgColor=fill)
    for sheet in (ws, rows):
        sheet.freeze_panes = "A2"
        for col in sheet.columns:
            sheet.column_dimensions[col[0].column_letter].width = min(60, max(10, *(len(str(c.value or "")) for c in col[:40])))
    wb.save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", default="output/mapper_test/gold.json")
    ap.add_argument("--mapper", default="output/mapper_test/mapper_export.csv")
    ap.add_argument("--ocl", default="output/snomed_vaccines_ocl.jsonl")
    ap.add_argument("--out", default="output/mapper_test/mapper_test_results.xlsx")
    args = ap.parse_args()

    gold = [g for g in json.load(open(args.gold, encoding="utf-8")) if g["Classe"] == "Vacina"]
    mapper = list(csv.DictReader(open(args.mapper, encoding="utf-8-sig")))
    results = score(gold, mapper, load_hierarchy(args.ocl))

    summaries = [summarise(results, "All vaccines")]
    for t in ("SAME-AS", "NARROWER-THAN"):
        summaries.append(summarise([r for r in results if r["map_type"] == t], t))
    summaries.append(summarise([r for r in results if r["combination"]], "Combination vaccines"))
    summaries.append(summarise([r for r in results if not r["combination"]], "Single-antigen vaccines"))
    write_excel(results, summaries, args.out)

    for s in summaries:
        print(json.dumps(s, ensure_ascii=False))
    print("AI status:", dict(collections.Counter(r["ai_status"] or "(none)" for r in results)))
    print("AI assessment:", dict(collections.Counter(r["ai_assessment"] or "(none)" for r in results)))
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
