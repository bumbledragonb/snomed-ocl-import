"""Translate Portuguese vaccine names (PNI / TUSS style) into English before uploading to OCL Mapper.

Uses a fixed glossary of Brazilian vaccine terms and abbreviations, built from general vaccine
knowledge only (no SNOMED CT names or codes, and not from any answer key), so the result is
reproducible and can be reviewed term by term.

Input:  CSV with Code and Name (Portuguese)
Output: CSV with Code, Name (English, what the Mapper matches) and Synonyms (the original
        Portuguese, which the Mapper also uses), plus a report of words left untranslated.

Usage:
  python translate_pt_en.py output/mapper_test/pni_vaccines_blind.csv output/mapper_test/pni_vaccines_blind_en.csv
"""
import csv
import re
import sys
import unicodedata

# Abbreviations, matched as whole words with exact case (dTpa and DTPa mean different things).
ABBREVIATIONS = {
    "DTPa": "diphtheria, tetanus and acellular pertussis",
    "dTpa": "reduced-dose diphtheria, tetanus and acellular pertussis",
    "DTP": "diphtheria, tetanus and whole-cell pertussis",
    "VIP": "inactivated poliovirus",
    "Hib": "Haemophilus influenzae type b",
    "HepB": "hepatitis B",
    "HPV": "human papillomavirus",
    "RSV": "respiratory syncytial virus",
    "BCG": "BCG (Bacillus Calmette-Guerin)",
    "RNAm": "mRNA",
    "ID": "intradermal",
}

# Phrases and words, matched case-insensitively and without accents; longest first.
GLOSSARY = {
    # combinations and popular names
    "quadrupla viral": "measles, mumps, rubella and varicella",
    "triplice viral": "measles, mumps and rubella",
    "hepatite aeb": "hepatitis A and hepatitis B",
    "penta acelular": "pentavalent acellular",
    "tetra acelular": "tetravalent acellular",
    "penta": "pentavalent",
    "hexa": "hexavalent",
    # diseases and organisms
    "virus sincicial respiratorio": "respiratory syncytial virus",
    "febre amarela": "yellow fever",
    "febre tifoide": "typhoid",
    "herpes zoster": "herpes zoster (shingles)",
    "toxoide tetanico": "tetanus toxoid",
    "meningococica": "meningococcal",
    "pneumococica": "pneumococcal",
    "meningo": "meningococcal",
    "pneumo": "pneumococcal",
    "hepatite": "hepatitis",
    "sarampo": "measles",
    "caxumba": "mumps",
    "rubeola": "rubella",
    "varicela": "varicella (chickenpox)",
    "variola": "smallpox (variola)",
    "difteria": "diphtheria",
    "tetano": "tetanus",
    "raiva": "rabies",
    "gripe": "influenza (flu)",
    "colera": "cholera",
    "polio": "polio",
    "rotavirus": "rotavirus",
    "influenza": "influenza",
    "dengue": "dengue",
    "chikungunya": "chikungunya",
    "covid 19": "COVID-19",
    # production and formulation
    "embriao de galinha": "grown in chick embryo",
    "em cultivo celular vero": "grown in Vero cell culture",
    "cultivo celular vero": "grown in Vero cell culture",
    "cultivo celulas vero": "grown in Vero cell culture",
    "polio injetavel": "injectable inactivated polio",
    "polio oral": "oral live attenuated polio",
    "atenuada": "live attenuated",
    "inativada": "inactivated",
    "recombinante": "recombinant",
    "conjugada": "conjugate",
    "polissacaridica": "polysaccharide",
    "adsorvida": "adsorbed",
    "acelular": "acellular",
    "adjuvante": "adjuvanted",
    "fracionada": "fractional dose",
    "alta dosagem": "high dose",
    # valency
    "nonavalente": "9-valent",
    "pentavalente": "pentavalent",
    "quadrivalente": "quadrivalent",
    "tetravalente": "quadrivalent",
    "trivalente": "trivalent",
    "bivalente": "bivalent",
    "valente": "valent",
    # population
    "menor de 5 anos": "under 5 years of age",
    "pediatrica": "pediatric",
    "infantil": "pediatric",
    "uso adulto": "adult",
    "adulto": "adult",
    # connecting words
    "contra o": "",
    "vacina": "",
    " e ": " and ",
}

# Serogroup letters after meningococcal, and valency numbers after pneumococcal.
PATTERNS = [
    (r"\bmeningococcal (ACWY|AC|B/C|C|B)\b", lambda m: "meningococcal serogroup " + {
        "ACWY": "A, C, W and Y", "AC": "A and C", "B/C": "B and C"}.get(m.group(1), m.group(1))),
    (r"\bpneumococcal (\d+)(?:V|-valent)?\b", r"\1-valent pneumococcal"),
    (r"\b(\d+)V\b", r"\1-valent"),
    (r"(?i)\bcovid-19\b", "COVID-19"),
    (r"\b(COVID-19) \1\b", r"\1"),                        # "Covid-19 COVID-19" written twice
    (r"\b([\w ]+?) \(\1\)", r"\1"),                       # "X (X)" after an abbreviation was expanded
]

KNOWN_ENGLISH = set("""a and b c w y of in by type years age under vaccine grown cell culture chick embryo vero
    dose high fractional recombinant inactivated live attenuated conjugate polysaccharide adsorbed acellular
    adjuvanted valent trivalent quadrivalent bivalent pentavalent hexavalent tetravalent pediatric adult
    against mrna intradermal injectable oral serogroup whole reduced""".split())


def strip_accents(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def translate(name):
    text = re.sub(r"(?<=[^\W\d])[-‐–—](?=[^\W\d])", " ", name)  # "herpes-zóster" -> "herpes zóster"; keeps "COVID-19"
    for abbr, english in ABBREVIATIONS.items():
        text = re.sub(rf"(?<![\w]){re.escape(abbr)}(?![\w])", f"@@{english}@@", text)
    text = strip_accents(text)
    for pt in sorted(GLOSSARY, key=len, reverse=True):
        text = re.sub(rf"(?<![\w@]){re.escape(pt.strip())}(?![\w@])" if pt.strip() == pt else re.escape(pt),
                      GLOSSARY[pt] if pt.strip() == pt else GLOSSARY[pt], text, flags=re.IGNORECASE)
    text = text.replace("@@", "")
    for pattern, repl in PATTERNS:
        text = re.sub(pattern, repl, text)
    text = re.sub(r"\s+", " ", text).strip(" ,-")
    return f"{text} vaccine"


def untranslated(english, original):
    """Words that are neither known English nor kept proper names (brands, manufacturers, codes)."""
    brands = {w.lower() for w in re.findall(r"[A-Za-z0-9.]+", original) if w.isupper() or not w.islower()}
    words = re.findall(r"[A-Za-z]+", english)
    return sorted({w for w in words if w.lower() not in KNOWN_ENGLISH and w.lower() not in brands
                   and not any(w.lower() in v.lower() for v in list(GLOSSARY.values()) + list(ABBREVIATIONS.values()))})


def main():
    src, dst = sys.argv[1], sys.argv[2]
    rows = list(csv.DictReader(open(src, encoding="utf-8-sig")))
    flagged = []
    with open(dst, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Code", "Name", "Synonyms"])
        for r in rows:
            english = translate(r["Name"])
            w.writerow([r["Code"], english, r["Name"]])
            left = untranslated(english, r["Name"])
            if left:
                flagged.append((r["Name"], english, left))
    print(f"Translated {len(rows)} names -> {dst}")
    print(f"Names with words left untranslated: {len(flagged)}")
    for original, english, left in flagged:
        print(f"  {original}  ->  {english}   [check: {', '.join(left)}]")


if __name__ == "__main__":
    main()
