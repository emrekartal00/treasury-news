"""exec_summary.py - one Turkish executive summary on top of the daily research mail.

Owner decision D17 (2026-10-20): the research EXECUTIVE SUMMARY goes into the live mail now,
i.e. here, in treasury-news. It is a port of the design in treasury-report (branch
mks-exec-summary, PRs #176/#179, pr-review pass 6 b2dfb139: ``platform_kernel/research_summary.py``
with its readers ``number_gate`` / ``claim_gate`` / ``number_slots``), folded into ONE file so
the office copy stays small. Keep the two in step when the design changes. On top of pass 6,
treasury-news PR #3 reviews add: unknown proper nouns before a value (``unknown_names``), label
slots never as values (``VALUE_AFTER``), the direction read over the whole stretch between
slots, suffixed number words, and a final re-read of every value against its own
normalised source (``reread``).

"Code picks the facts, the model only writes from them":

  pick_reports()  the first ``MAX_REPORTS`` (30): the mail's reports in mail order, then the
                  rest of the day's digest; NO_CONTENT / empty summaries left out.
  build_spec()    one fact line per report ([R1]..[Rn], in that order) and, for every
                  instrument several banks wrote about, one line per STANCE ([K1]..): the
                  banks holding it and their reports (code counts).
  NUMBER SLOTS    every digit-bearing span of a fact line becomes a TYPED slot ({R3.n1} =
                  "%36", bank, series, role); the model sees and writes only slot ids, and
                  ``fill_slots`` puts the report's exact value in. ``check_slots`` refuses a
                  number the model wrote itself, anything glued to a slot, an uncited slot, a
                  bank without its cited report, a value under another series, a label used
                  as a value, a contradicting direction, a negated verb; ``verify_filled`` and
                  ``reread`` read the FILLED sentence again.
  write()         ONE model call writes a headline + 3-5 bullets; EVERY sentence ends with the
                  report(s) it comes from, agreeing reports under ONE claim ("... [R3][R7]"),
                  dissent on its own line. Two attempts with the errors fed back, then the
                  code's TEMPLATE. A [K#] is expanded to its reports' [R#]s, so a reader only
                  ever sees report citations, each linked to its report.
  to_html() / to_text()  the mail-ready block (inline-styled, Outlook-safe) and plain text.

``ask(system, user) -> str | dict`` is passed in by mailer.py (it wraps ``ai.chat``, the
existing client, exactly as curate.py / summarize.py call it). This module calls no model
itself, does no I/O and imports nothing from the pipeline. Turkish only.

Report shape (mailer.load_report)::

    {"id", "title", "headline", "source", "date": "YYYY-MM-DD", "source_path",
     "summary": {"headline", "key_points": [{"point", "evidence"}], "instruments": [...],
                 "stance": "bullish|bearish|neutral|mixed|n/a", "one_paragraph"}}

Pure Python, standard library only.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field


# ##########################################################################################
# 1. number reader (treasury-report platform_kernel/number_gate.py)
# ##########################################################################################


# ════════════════════════════════════════════════════════════════════════════ SETTINGS
MAX_NUMBERS = 2               # evds-analysis EN_COK_SAYI (dates and citations not counted)
MAX_WORDS = 25                # evds-analysis EN_COK_KELIME

#: Cause words (evds-analysis YASAK + English). Allowed only when a cited claim's quote has one.
CAUSE_WORDS = ("çünkü", "nedeniyle", "sayesinde", "dolayısıyla", "yol açt", "neden ol",
               "kaynaklan", "etkisiyle", "yüzünden", "because", "due to", "driven by",
               "owing to", "caused by", "thanks to", "as a result of")
#: Forecast words (evds-analysis YASAK). Allowed only in a sentence citing a research claim.
FORECAST_WORDS = ("tahmin", "bekleniyor", "beklenmekte", "muhtemel", "olası", "gelecek hafta",
                  "önümüzdeki", "forecast", "is expected", "likely")
#: The desk never speaks with its own voice (plan v4 §5d) and never names how text is made.
BANNED_WORDS = ("öneririz", "alınmalı", "satılmalı", "fırsat", "cazip", "tavsiye ederiz",
                "we recommend", "opportunity", "attractive", "yapay zeka", "yapay zekâ",
                "artificial intelligence", " llm", "chatgpt")
#: "the market prices" is only for these fact kinds (market_brief_app: a survey is not pricing).
PRICING_WORDS = ("piyasa fiyatl", "fiyatlıyor", "fiyatlaması", "market prices", "market is pricing",
                 "priced in", "pricing in")
PRICING_KINDS = ("ois", "futures", "yield", "forward")
#: Superlatives: these need a rank; the "never" ones are refused outright.
SUPERLATIVE_WORDS = ("en yüksek", "en düşük", "en sert", "en büyük", "en küçük", "highest",
                     "lowest", "largest", "smallest", "biggest", "steepest")
NEVER_SUPERLATIVE = ("rekor", "ilk kez", "tarihi zirve", "tüm zamanlar", "record", "all-time",
                     "first time ever", "unprecedented")
#: evds-analysis GRUP_ADI: a number after one of these comes from that group's fact.
GROUP_NAMES = ("kamu", "yerli özel", "yabancı", "mevduat bankaları")
# ═════════════════════════════════════════════════════════════════════════════════════

CITATION = re.compile(r"\[([KR]\d+)\]")
_NUMBER = re.compile(r"\d[\d.,]*")
_DATE_DOTTED = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")
_DATE_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_SCALES = (
    (re.compile(r"^\s*(baz puan|bps?\b|basis point)", re.I), (100.0,)),
    (re.compile(r"^\s*(trilyon|trillion|tn\b)", re.I), (1e-12,)),
    (re.compile(r"^\s*(milyar|billion|bn\b)", re.I), (1e-9,)),
    (re.compile(r"^\s*(milyon|million|mn\b|mio\b)", re.I), (1e-6,)),
)
_MONTH = re.compile(r"^\s*(oca|şub|mar|nis|may|haz|tem|ağu|eyl|eki|kas|ara|jan|feb|apr|jun|jul|"
                    r"aug|sep|oct|nov|dec)", re.I)
_GROUP = re.compile(r"(" + "|".join(re.escape(g) for g in GROUP_NAMES) + r")", re.I)


@dataclass(frozen=True)
class Number:
    raw: str            # as written, separators stripped of trailing punctuation
    norm: str           # dot-decimal, no thousands separator ("7722.72")
    value: float
    decimals: int
    start: int
    end: int
    alt: float | None = None   # "41,205" / "1.250": one separator + 3 digits reads both ways


def _normalise(tok):
    """market_brief_app writer._numbers: one token -> dot-decimal string, or None for a date."""
    tok = tok.rstrip(".,")
    if not tok or _DATE_DOTTED.match(tok):
        return None
    if re.fullmatch(r"\d{1,3}(\.\d{3})+\.\d{1,2}", tok):        # '171.203.8'
        head, _, dec = tok.rpartition(".")
        return head.replace(".", "") + "." + dec
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", tok):          # TR thousands
        return tok.replace(".", "").replace(",", ".")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", tok):          # EN thousands
        return tok.replace(",", "")
    return tok.replace(",", ".")


def numbers(text):
    """Every number written in ``text`` (citations and ISO/dotted dates left out)."""
    text = CITATION.sub(lambda m: " " * len(m.group(0)), text or "")
    text = _DATE_ISO.sub(lambda m: " " * len(m.group(0)), text)
    out = []
    for m in _NUMBER.finditer(text):
        norm = _normalise(m.group(0))
        if norm is None:
            continue
        try:
            value = float(norm)
        except ValueError:
            continue
        decimals = len(norm.split(".", 1)[1]) if "." in norm else 0
        raw = m.group(0).rstrip(".,")
        alt = None
        if re.fullmatch(r"\d{1,3}[.,]\d{3}", raw):          # thousands (EN/TR) or 3 decimals
            alt = float(raw.replace(",", ".")) if "," in raw else float(raw.replace(".", ""))
        out.append(Number(raw, norm, value, decimals, m.start(), m.start() + len(raw), alt))
    return out


def evidence_values(*items):
    """Float pool of everything numeric in ``items`` (nested dict/list/str/number), plus the
    day, month and year of every ISO or dotted date found in strings."""
    pool = []

    def walk(x):
        if isinstance(x, bool) or x is None:
            return
        if isinstance(x, (int, float)):
            pool.append(abs(float(x)))
        elif isinstance(x, str):
            for y, mo, d in _DATE_ISO.findall(x):
                pool.extend((float(y), float(mo), float(d)))
            for tok in re.findall(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b", x):
                pool.extend(float(p) for p in tok.split("."))
            pool.extend(n.value for n in numbers(x))
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple, set)):
            for v in x:
                walk(v)

    for item in items:
        walk(item)
    return pool


def has_number(num, pool, after=""):
    """market_brief_app has_number + balance-sheet-analysis scale: True when some pool value,
    scaled by the unit written after the number and rounded to the number's decimals,
    equals it. Signs are not compared (a fall written "25 bp" against -0.25)."""
    scales = [1.0]
    for pattern, extra in _SCALES:
        if pattern.match(after or ""):
            scales += list(extra)
            break
    readings = [(abs(num.value), num.decimals)]
    if num.alt is not None:
        readings.append((abs(num.alt), 3 if num.alt != int(num.alt) or "," in num.raw else 0))
    for p in pool:
        for s in scales:
            for target, decimals in readings:
                if abs(round(p * s, decimals) - target) < 1e-9:
                    return True
    return False


def _has_any(text, words):
    low = (text or "").lower()
    return [w for w in words if w in low]


def counted(sentence):
    """The numbers that count toward MAX_NUMBERS: a year and a day before a month name are
    dates, not values (evds-analysis SAYILMAZ); they are still CHECKED against the evidence."""
    clean = CITATION.sub("", sentence or "")
    return [n for n in numbers(clean)
            if not (n.decimals == 0 and re.fullmatch(r"(19|20)\d\d", n.norm))
            and not (n.decimals == 0 and n.value <= 31 and _MONTH.match(clean[n.end:n.end + 6]))]


def word_count(sentence):
    return len(CITATION.sub("", sentence or "").split())


def check_sentence(sentence, facts=None, claims=None, *, limits=True):
    """Problems with one sentence (Turkish reasons, empty = passes). ``facts``/``claims`` are
    the whole evidence; only what the sentence cites counts."""
    facts, claims = facts or {}, claims or {}
    problems = []
    cited = list(dict.fromkeys(CITATION.findall(sentence or "")))
    unknown = [c for c in cited if c not in facts and c not in claims]
    if unknown:
        problems.append("bilinmeyen atıf: " + ", ".join(unknown))
    cited_facts = [facts[c] for c in cited if c in facts]
    cited_claims = [claims[c] for c in cited if c in claims]
    nums = numbers(sentence)
    if nums and not cited_facts and not cited_claims:
        problems.append("sayı var ama atıf yok")
    pool = evidence_values(*cited_facts, *cited_claims)
    clean = CITATION.sub("", sentence or "")
    for n in nums:
        if not has_number(n, pool, clean[n.end:n.end + 14]):
            problems.append(f"atıf yapılan kanıtta olmayan sayı: {n.raw}")
            continue
        before = [g.group(1).lower() for g in _GROUP.finditer(clean[:n.start])]
        grouped = [f for f in cited_facts if f.get("group")]
        if before and grouped:
            own = [f for f in grouped if before[-1] in str(f.get("group")).lower()]
            if not any(has_number(n, evidence_values(f), clean[n.end:n.end + 14]) for f in own):
                problems.append(f"{n.raw} sayısı '{before[-1]}' grubuna yanlış bağlanmış")
    for w in _has_any(sentence, BANNED_WORDS):
        problems.append(f"yasak ifade: {w.strip()}")
    quotes = " ".join(str(c.get("quote") or "") for c in cited_claims).lower()
    for w in _has_any(sentence, CAUSE_WORDS):
        if w not in quotes:
            problems.append(f"neden-sonuç ifadesi kaynakta yok: {w}")
    if not cited_claims:
        for w in _has_any(sentence, FORECAST_WORDS):
            problems.append(f"tahmin ifadesi yalnız atıflı bir araştırma görüşünde olur: {w}")
    if _has_any(sentence, PRICING_WORDS) and not any(
            str(f.get("kind") or "").lower() in PRICING_KINDS for f in cited_facts):
        problems.append("'piyasa fiyatlıyor' yalnız OIS/vadeli/getiri olgusuyla yazılır")
    for w in _has_any(sentence, NEVER_SUPERLATIVE):
        problems.append(f"yasak üstünlük sözcüğü: {w}")
    if _has_any(sentence, SUPERLATIVE_WORDS) and not any(
            f.get("rank") for f in cited_facts):
        problems.append("üstünlük sözcüğü bir sıralamayla desteklenmiyor")
    if limits:
        values = counted(sentence)
        if len(values) > MAX_NUMBERS:
            problems.append(f"cümlede {len(values)} sayı var (en çok {MAX_NUMBERS})")
        if word_count(sentence) > MAX_WORDS:
            problems.append(f"cümle {word_count(sentence)} kelime (en çok {MAX_WORDS})")
    return problems


# ##########################################################################################
# 2. sentence readers (treasury-report platform_kernel/claim_gate.py)
# ##########################################################################################



# ════════════════════════════════════════════════════════════════════════════ SETTINGS
MAX_VALUES = 2
#: Canonical bank name -> how a sentence may write it. Short upper-case codes match case-
#: sensitively as whole words; the rest case-insensitively (pr-review pass 4, owner list).
BANK_ALIASES = {
    "Goldman Sachs": ("Goldman Sachs", "Goldman", "GS"),
    "JPMorgan": ("JPMorgan Chase", "J.P. Morgan", "J. P. Morgan", "J.P.Morgan", "JP Morgan", "JPMorgan", "JPM"),
    "Morgan Stanley": ("Morgan Stanley", "MS"),
    "Deutsche Bank": ("Deutsche Bank", "Deutsche", "DB"),
    "Citi": ("Citigroup", "Citibank", "Citi"),
    "Barclays": ("Barclays", "BARC"),
    # not research sources today: named only so a sentence that names them is REFUSED (no cited
    # report of theirs can exist) - pr-review pass 6
    "HSBC": ("HSBC",),
    "Nomura": ("Nomura",),
    "Bank of America": ("Bank of America", "BofA", "Merrill Lynch", "Merrill", "BAML"),
    "UBS": ("UBS",),
    "BNP Paribas": ("BNP Paribas", "BNP"),
    "Société Générale": ("Société Générale", "Societe Generale", "SocGen"),
    "Credit Suisse": ("Credit Suisse",),
    "Commerzbank": ("Commerzbank",),
    "Wells Fargo": ("Wells Fargo",),
    "Standard Chartered": ("Standard Chartered", "StanChart"),
    "ING": ("ING",),
    "Natixis": ("Natixis",),
    "Santander": ("Santander",),
    "Mizuho": ("Mizuho",),
    "Bloomberg Economics": ("Bloomberg Economics",),
    "Fitch": ("Fitch",),
    "Moody's": ("Moody's",),
    "S&P": ("S&P Global", "Standard & Poor's"),
}
#: Direction words (token prefixes, lower case) near a number: a source's direction and the
#: sentence's must not contradict.
DOWN_WORDS = ("indirim", "indir", "düşüş", "düşür", "düştü", "geriledi", "geriley", "azal",
              "kesinti", "gevşe", "cut", "lower", "decline", "fall", "fell", "drop", "reduc",
              "eas", "down")
UP_WORDS = ("artırım", "artış", "artır", "arttı", "artacak", "yüksel", "zam", "sıkılaş",
            "hike", "raise", "increase", "rise", "rose", "higher", "tighten", "up")
# ═════════════════════════════════════════════════════════════════════════════════════

_TR_ONES = {"sıfır": 0, "bir": 1, "iki": 2, "üç": 3, "dört": 4, "beş": 5, "altı": 6,
            "yedi": 7, "sekiz": 8, "dokuz": 9}
_TR_TENS = {"on": 10, "yirmi": 20, "otuz": 30, "kırk": 40, "elli": 50, "altmış": 60,
            "yetmiş": 70, "seksen": 80, "doksan": 90}
_EN_SMALL = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_TR_WORD = "|".join(sorted(list(_TR_ONES) + list(_TR_TENS) + ["yüz"], key=len, reverse=True))
_TR_NUM = re.compile(r"(?<!\w)((?:(?:%s)\s+)*(?:%s))(?=\s+(?:baz puan|puan|kurum|banka|rapor))"
                     % (_TR_WORD, _TR_WORD), re.I)
_YUZDE = re.compile(r"\byüzde\s+(?=(?:\d|(?:%s)\b))" % _TR_WORD, re.I)
_YUZDE_WORDS = re.compile(r"%((?:(?:" + _TR_WORD + r")\s+)*(?:" + _TR_WORD + r"))\b", re.I)
_EN_TENOR = re.compile(r"\b(%s)(?=[\s-]+(?:years?|months?|weeks?|days?)\b)" % "|".join(_EN_SMALL), re.I)

_DIGITS = re.compile(r"\d")
_CG_CITE = re.compile(r"\[[A-ZÇ]\d+\]")
_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\.\d{1,2}\.\d{4}\b")
_MONTHS = ("ocak|şubat|mart|nisan|mayıs|haziran|temmuz|ağustos|eylül|ekim|kasım|aralık|"
           "jan\\w*|feb\\w*|mar\\w*|apr\\w*|may|jun\\w*|jul\\w*|aug\\w*|sep\\w*|oct\\w*|nov\\w*|dec\\w*")
_DAY_MONTH = re.compile(r"\b(\d{1,2})\s+(?:%s)\b" % _MONTHS, re.I)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_TENOR = re.compile(r"(?<![%\d.,])(?<!%\s)\b(\d{1,3})\s?-?\s?(?:iş\s+)?(?:y|m|w|d|yr|yrs|years?|months?|"
                    r"weeks?|days?|yıl\w*|ay\w*|hafta\w*|gün\w*)\b", re.I)
_PERIOD = re.compile(r"\b(?:[QH]([1-4])|([1-4])Q)\b|\b([1-4])\.\s?(?:çeyrek|yarı)\w*", re.I)
_COUNT_AFTER = re.compile(r"^\s*(?:'?\w{0,3}\s)?(?:kurum|banka|rapor)", re.I)
_POSSESSIVE = re.compile(r"^'(?:s?[ıiuü])\b", re.I)
_BP = re.compile(r"^\s*(?:bps?\b|baz puan|basis point)", re.I)
_PCT_AFTER = re.compile(r"^\s*(?:%|percent|pct\b)", re.I)
_RANGE = re.compile(r"^\s*[–-]\s*%?\s*$")
_JOIN = re.compile(r"^\s*(?:,|ve|ile|and|&)\s*$", re.I)


# ───────────────────────────────────────────── normalising
def _tr_words_to_int(words: str) -> int | None:
    total = 0
    for w in words.lower().split():
        if w == "yüz":
            total = (total or 1) * 100
        elif w in _TR_TENS:
            total += _TR_TENS[w]
        elif w in _TR_ONES:
            total += _TR_ONES[w]
        else:
            return None
    return total


def normalise(text: str) -> str:
    """Written forms -> digits: "yüzde 35" -> "%35", "otuz beş baz puan" -> "35 baz puan",
    "iki kurum" -> "2 kurum", "one month" -> "1 month"."""
    t = _YUZDE.sub("%", text or "")
    t = _YUZDE_WORDS.sub(lambda m: "%" + str(_tr_words_to_int(m.group(1))), t)
    t = _TR_NUM.sub(lambda m: str(_tr_words_to_int(m.group(1))), t)
    return _EN_TENOR.sub(lambda m: str(_EN_SMALL[m.group(1).lower()]), t)


# ───────────────────────────────────────────── banks
def _alias_patterns():
    out = []
    for bank, aliases in BANK_ALIASES.items():
        for a in aliases:
            flags = 0 if (a.isupper() and len(a) <= 4) else re.I
            out.append((len(a), re.compile(r"(?<![\w.])%s(?![\w])" % re.escape(a), flags), bank))
    return [(p, b) for _, p, b in sorted(out, key=lambda x: -x[0])]


_ALIASES = _alias_patterns()


def bank_mentions(text: str) -> list[tuple[int, int, str]]:
    """``[(start, end, canonical bank)]`` in text order; longest alias first, no overlap."""
    taken: list[tuple[int, int]] = []
    hits = []
    for pat, bank in _ALIASES:
        for m in pat.finditer(text or ""):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            hits.append((m.start(), m.end(), bank))
    return sorted(hits)


def canonical_bank(name: str) -> str:
    hits = bank_mentions(name or "")
    return hits[0][2] if hits else (name or "")


# ───────────────────────────────────────────── reading a text
def _direction(masked: str, start: int, end: int) -> int:
    """-1 / +1 from an explicit sign before the number or a direction word within a few
    words around it; 0 when neither or both."""
    before = masked[max(0, start - 3):start]
    if re.search(r"(?:^|[\s(])[−-]\s?%?$", before):
        return -1
    if re.search(r"(?:^|[\s(])\+\s?%?$", before):
        return +1
    after = re.findall(r"[^\W\d_]+", masked[end:end + 40].lower())[:3]
    prior = re.findall(r"[^\W\d_]+", masked[max(0, start - 30):start].lower())[-2:]
    words = after + prior
    down = any(w.startswith(DOWN_WORDS) for w in words)
    up = any(w.startswith(UP_WORDS) for w in words)
    return -1 if down and not up else +1 if up and not down else 0


def read_values(masked: str) -> list[tuple[Number, str, int]]:
    """The numbers of a masked text with unit and direction (a range's bare end takes the
    unit of its other end: "%4,10–4,40", "4.10-4.40%")."""
    out = []
    for n in numbers(masked):
        before, after = masked[max(0, n.start - 2):n.start], masked[n.end:n.end + 16]
        unit = "pct" if ("%" in before or _PCT_AFTER.match(after)) else "bp" if _BP.match(after) else "num"
        out.append([n, unit, _direction(masked, n.start, n.end)])
    for i in range(len(out)):
        for j in (i - 1, i + 1):
            if 0 <= j < len(out) and out[i][1] == "num" and out[j][1] != "num":
                a, b = sorted((out[i][0], out[j][0]), key=lambda x: x.start)
                if _RANGE.match(masked[a.end:b.start]):
                    out[i][1] = out[j][1]
    return [tuple(x) for x in out]


def _subject(masked: str, mentions, pos: int) -> list[str]:
    """The bank(s) a number at ``pos`` belongs to, in mention order: the joint group ending
    last before it, else the first group starting after it."""
    if not mentions:
        return []
    groups, cur = [], [mentions[0]]
    for prev, m in zip(mentions, mentions[1:]):
        if _JOIN.match(masked[prev[1]:m[0]]):
            cur.append(m)
        else:
            groups.append(cur)
            cur = [m]
    groups.append(cur)
    before = [g for g in groups if g[-1][1] <= pos]
    if before:
        g = before[-1]
    else:
        after = [g for g in groups if g[0][0] >= pos]
        if not after:
            return []
        g = after[0]
    names: list[str] = []
    for _, _, b in g:
        if b not in names:
            names.append(b)
    return names


def mask_digits(text: str) -> str:
    """Digits outside citations -> "#" (for ``number_gate``'s word checks only)."""
    parts = re.split(r"(\[[A-ZÇ]\d+\])", text or "")
    return "".join(p if _CG_CITE.fullmatch(p) else _DIGITS.sub("#", p) for p in parts)


# ───────────────────────────────────────────── sentences
_BOUNDARY = re.compile(r"[.!?;:](?=\s|[A-ZÇĞİÖŞÜa-zçğıöşü\[])|\n+")
_NO_END = re.compile(r"(?:(?:^|\s)\d{1,2}\.$)|(?:(?:^|[\s(])(?:[A-ZÇĞİÖŞÜ]\.\s?)*[A-ZÇĞİÖŞÜ]\.$)|"
                     r"(?:\b(?:vb|vs|örn|bkz|yak|dr|no|st|inc|co|ltd|corp)\.$)", re.I)


def split_sentences(text: str, colon: bool = True) -> list[str]:
    """Sentences of a bullet (see the module docstring). ``colon=False``: a colon does not
    split (code-made lines such as the template's "USD/TRY: ...")."""
    text = (text or "").strip()
    out, start = [], 0
    for m in _BOUNDARY.finditer(text):
        if m.group(0) == ":" and not colon:
            continue
        head = text[start:m.end()]
        if text.rfind("{", 0, m.start()) > text.rfind("}", 0, m.start()):
            continue                       # inside a slot id ({R1.n1})
        if m.group(0) == "." and _NO_END.search(head):
            continue
        if m.group(0) == ":" and re.search(r"\d$", head[:-1]):
            continue                       # "10:30"
        rest = text[m.end():]
        if not _CG_CITE.sub("", rest.split(".")[0] if rest.strip().startswith("[") else "x").strip(" "):
            continue                       # "... yaptı. [R2]": the citation stays with it
        out.append(head)
        start = m.end()
    out.append(text[start:])
    merged: list[str] = []
    for piece in (x.strip() for x in out):
        if not piece:
            continue
        if merged and not _CG_CITE.sub("", piece).strip(" .;"):
            merged[-1] += " " + piece
        else:
            merged.append(piece)
    return merged


# ##########################################################################################
# 3. number slots (treasury-report platform_kernel/number_slots.py)
# ##########################################################################################



SLOT = re.compile(r"\{([A-ZÇ]+\d+)\.([a-z]+\d*)\}")
_NS_CITE = re.compile(r"\[[A-ZÇ]+\d+\]")

# ════════════════════════════════════════════════════════════════════════════ SETTINGS
#: Number words: a token made of these stems (+ a numeral suffix) is a number the model wrote
#: itself ("iki", "otuzaltı", "ikinci", "üçte", "beşer"). "bir" stays out (it is also "a").
NUMBER_STEMS = ("sıfır", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz", "on",
                "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan", "yüz",
                "bin", "milyon", "milyar", "trilyon")
NUMBER_SUFFIXES = ("", "nci", "ncı", "ncu", "ncü", "inci", "ıncı", "uncu", "üncü", "si", "sı",
                   "su", "sü", "i", "ı", "u", "ü", "te", "ta", "de", "da", "er", "ar", "şer",
                   "şar", "lik", "lık", "luk", "lük", "li", "lı", "lu", "lü")
NUMBER_TOKENS = ("çeyrek", "yarım", "buçuk", "düzine", "yarısı", "yüzde", "binde", "eksi", "artı",
                 "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                 "eleven", "twelve", "twenty", "thirty", "forty", "fifty", "hundred", "half",
                 "quarter", "dozen", "percent")
#: Words that may not stand next to a slot (the slot renders its own unit).
UNIT_WORDS = ("baz", "puan", "puanlık", "bp", "bps", "kat", "katı", "katına", "katlık", "kez",
              "yüzde", "percent", "pct", "eksi", "artı", "minus", "plus")
#: Words a value is the object of: a LABEL slot right after them is used as a value.
LEVEL_WORDS = ("faiz", "faizi", "faizini", "oran", "oranı", "oranını", "kur", "kuru",
               "seviye", "seviyesi", "düzey", "düzeyi", "değer", "değeri", "hedef", "hedefi",
               "beklenti", "beklentisi", "tahmin", "tahmini", "getiri", "getirisi", "fiyat", "fiyatı")
#: Words right after a date / year / period / tenor slot that make it a value (PR #3 review:
#: a label slot is never a value): "{l1} seviyesinde", "{l1} olarak", "{l1} öngörüyor".
VALUE_AFTER = ("seviye", "düzey", "olarak", "olacağ", "olduğ", "oranında", "civarında", "arasında",
               "üzerinde", "altında", "öngör", "bekl", "görüyor", "tahmin", "hedefl", "yüksel",
               "düşe", "düşü", "geril", "artı", "artacağ")
#: English source series -> how a Turkish sentence may write them (lower case). A value slot
#: written under a DIFFERENT series name than its own is refused.
SERIES_ALIASES = {
    "policy rate": ("politika faizi", "politika faizini", "policy rate", "repo faizi"),
    "deposit rate": ("mevduat faizi", "deposit rate"),
    "ecb deposit rate": ("ecb mevduat faizi", "ecb deposit rate"),
    "usd/try": ("usd/try", "dolar/tl", "dolar/türk lirası", "dolar kuru"),
    "eur/try": ("eur/try", "euro/tl", "euro kuru"),
    "eur/usd": ("eur/usd", "euro/dolar", "parite"),
}
#: Negated verbs: a negated forecast would invert the source's claim.
_NEGATION = re.compile(r"\b\w+m[ıiuü]yor\w*|\b\w+(?:ma|me)(?:z|dı|di|dığı|diği|mış|miş|yacak|yecek"
                       r"|yacağ|yeceğ)\w*|\bdeğil\w*|\bdışl[ıi]\w*|\bolmayaca\w*|\bolmadığ\w*")
# ═════════════════════════════════════════════════════════════════════════════════════

_TENOR_EN = re.compile(r"(\d{1,3})\s?-?\s?(y|yr|yrs|years?|m|months?|w|weeks?|d|days?)$", re.I)
_TENOR_TR = {"y": "yıllık", "m": "aylık", "w": "haftalık", "d": "günlük"}
_UNIT_TAIL_TR = re.compile(r"^(\s*(?:(?:milyar|milyon|bin)\s+)?(?:TL|USD|EUR|ABD doları|baz puan|puan)\b)")
_SIGN_BEFORE = re.compile(r"(?:^|[\s(])([−+-])\s?%?$")     # a range dash ("4.10%-4.40%") is not a sign
_BAND = re.compile(r"\(\s*p10\s*[–-]\s*p90\s*:\s*([^)]*)\)", re.I)
_PCTL_AFTER = re.compile(r"^\s*yüzdeli", re.I)
_NUMWORD = re.compile(r"^(?:%s)+(?:%s)$" % ("|".join(sorted(NUMBER_STEMS, key=len, reverse=True)),
                                            "|".join(sorted(NUMBER_SUFFIXES, key=len, reverse=True))))
_ROMAN = re.compile(r"\b[IVXLCDM]{2,}\b")


@dataclass(frozen=True)
class Slot:
    id: str              # "R3.n1"
    source: str          # "R3"
    display: str         # what the reader sees: "%36", "150 baz puan", "UST 10Y", "3 aylık"
    kind: str            # "value" | "label" | "count"
    bank: str | None = None
    sign: int = 0        # -1 down / +1 up / 0 unknown (values)
    role: str = "level"  # level | band | pctl | count | date | year | period | tenor | name
    series: str | None = None


# ───────────────────────────────────────────── formatting (code, Turkish)
def tr_number(n: Number) -> str:
    """The source number in Turkish notation, its own decimals (no rounding)."""
    whole, _, frac = n.norm.partition(".")
    whole = "{:,}".format(int(whole)).replace(",", ".") if whole.isdigit() else whole
    return whole + ("," + frac if frac else "")


def _tenor_tr(span: str) -> str:
    m = _TENOR_EN.search(span.strip())
    if not m:
        return span.strip()
    return "%s %s" % (m.group(1), _TENOR_TR[m.group(2)[0].lower()])


def _period_tr(span: str) -> str:
    m = re.search(r"([QH])([1-4])|([1-4])Q", span, re.I)
    if not m:
        return span.strip()
    if m.group(3):
        return "%s. çeyrek" % m.group(3)
    return "%s. %s" % (m.group(2), "çeyrek" if m.group(1).upper() == "Q" else "yarıyıl")


def _tr_lower(text: str) -> str:
    return (text or "").replace("İ", "i").replace("I", "ı").lower()


def series_key(name: str | None) -> str | None:
    """A series name -> its canonical key (``SERIES_ALIASES``), else its own lower case."""
    if not name:
        return None
    low = _tr_lower(name).strip()
    for key, aliases in SERIES_ALIASES.items():
        if low == key or low in aliases:
            return key
    return low


def _series_mentions(text: str, names) -> list[tuple[int, int, str]]:
    """``[(start, end, series key)]`` of the series names a text writes, longest first."""
    low = _tr_lower(text)
    cands = {}
    for n in names or ():
        k = series_key(n)
        for form in {_tr_lower(n)} | set(SERIES_ALIASES.get(k, ())):
            if form:
                cands[form] = k
    hits, taken = [], []
    for form in sorted(cands, key=len, reverse=True):
        for m in re.finditer(r"(?<!\w)%s(?!\w)" % re.escape(form), low):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            hits.append((m.start(), m.end(), cands[form]))
    return sorted(hits)


# ───────────────────────────────────────────── reading a source into slots
def _spans(text: str, names) -> list[tuple[int, int, str, str]]:
    """``[(start, end, role, display)]`` of every label/band span, in text order, no overlap."""
    taken: list[tuple[int, int, str, str]] = []

    def add(s, e, role, display):
        if not any(s < e2 and s2 < e for s2, e2, _, _ in taken):
            taken.append((s, e, role, display))
    for m in _BAND.finditer(text):
        add(m.start(), m.end(), "band", "p10–p90 bandı %s" % m.group(1).strip())
    for m in re.finditer(r"\b\d{1,2}\s+(?:%s)(?:\s+(?:19|20)\d{2})?\b" % _MONTHS, text, re.I):
        add(m.start(), m.end(), "date", m.group(0))
    for m in _ISO.finditer(text):
        add(m.start(), m.end(), "date", m.group(0))
    for n in names:
        for m in re.finditer(re.escape(n), text, re.I):
            add(m.start(), m.end(), "name", m.group(0))
    for m in _YEAR.finditer(text):
        add(m.start(), m.end(), "year", m.group(0))
    for m in _PERIOD.finditer(text):
        add(m.start(), m.end(), "period", _period_tr(m.group(0)))
    for m in _TENOR.finditer(text):
        add(m.start(), m.end(), "tenor", _tenor_tr(m.group(0)) if not re.search(
            r"[çğıöşü]|iş|gün|ay|yıl|hafta", m.group(0), re.I) else m.group(0))
    return sorted(taken)


def _owners_in(masked: str, values) -> list[str | None]:
    """The bank each value of a text belongs to, as the text itself writes it: the bank
    named before it (or the first after it); a joint subject "A ve B ... X ve Y" respectively."""
    mentions = bank_mentions(masked)
    subjects = [_subject(masked, mentions, n.start) for n, _, _ in values]
    owner = [s[0] if len(s) == 1 else None for s in subjects]
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and subjects[j + 1] == subjects[i] and re.match(
                r"^\s*%?\s*(?:'\w+)?\s*(?:,|ve|ile|and|&)\s*%?\s*$",
                masked[values[j][0].end:values[j + 1][0].start], re.I):
            j += 1
        if len(subjects[i]) > 1 and j - i + 1 == len(subjects[i]):
            for k in range(len(subjects[i])):
                owner[i + k] = subjects[i][k]
        i = j + 1
    return owner


def slots_for(source_id: str, text: str, names=(), bank: str | None = None,
              turkish: bool = False, banks_from_text: bool = False, series_names=(),
              default_series: str | None = None) -> tuple[str, list[Slot]]:
    """``(text with slot ids, slots)`` of one source.

    ``bank`` owns its value and tenor/period slots, or with ``banks_from_text`` each value
    belongs to the bank the text writes next to it; ``series_names`` are the series this
    source may name (a value's series = the last one written before it, else
    ``default_series``); ``turkish`` keeps the source's own number text and unit."""
    t = normalise(text or "")
    spans = _spans(t, names)
    masked = list(t)
    for s, e, _, _ in spans:
        masked[s:e] = " " * (e - s)
    masked = "".join(masked)
    series_hits = _series_mentions(t, series_names)

    def series_at(pos):
        before = [k for s, e, k in series_hits if e <= pos]
        return before[-1] if before else series_key(default_series)
    items = []
    for s, e, role, disp in spans:
        own = bank if role in ("tenor", "period") else None
        items.append((s, e, "value" if role == "band" else "label", disp, 0, own, role,
                      series_at(s) if role == "band" else None))
    values = read_values(masked)
    owners = _owners_in(masked, values) if banks_from_text else [bank] * len(values)
    for (n, unit, sign), own in zip(values, owners):
        s, e = n.start, n.end
        before = masked[max(0, s - 4):s]
        sign_m = _SIGN_BEFORE.search(before)
        if turkish:
            if s > 0 and t[s - 1] == "%":
                s -= 1
            tail = _UNIT_TAIL_TR.match(t[e:])
            if tail:
                e += len(tail.group(1))
            disp = t[s:e]
        else:
            num = tr_number(n)
            disp = "%" + num if unit == "pct" else num + " baz puan" if unit == "bp" else num
            if unit == "pct":                          # swallow "%" / "percent" next to it
                if s > 0 and t[s - 1] == "%":
                    s -= 1
                m = re.match(r"\s*(?:%|percent|pct)", t[e:], re.I)
                if m:
                    e += m.end()
            elif unit == "bp":
                e += re.match(r"\s*(?:bps?|baz puan|basis points?)", t[e:], re.I).end()
        if sign_m:
            s = min(s, n.start - len(before) + sign_m.start(1))
            disp = ("−" if sign_m.group(1) in "−-" else "+") + disp.lstrip("+-−")
        count = bool(_COUNT_AFTER.match(masked[n.end:n.end + 12]))
        role = "count" if count else "pctl" if _PCTL_AFTER.match(t[e:e + 14]) else "level"
        items.append((s, e, "count" if count else "value", disp,
                      (-1 if sign_m and sign_m.group(1) in "−-" else +1 if sign_m else sign),
                      own, role, None if count else series_at(s)))
    items.sort()
    out, slots, pos, k = [], [], 0, {"value": 0, "label": 0, "count": 0}
    for s, e, kind, disp, sgn, own, role, series in items:
        if s < pos:
            continue
        k[kind] += 1
        sid = "%s.%s%d" % (source_id, {"value": "n", "count": "c", "label": "l"}[kind], k[kind])
        slots.append(Slot(sid, source_id, disp.strip(), kind, own, sgn, role, series))
        out.append(t[pos:s] + "{%s}" % sid)
        pos = e
    out.append(t[pos:])
    return "".join(out), slots


def count_slot(source_id: str, name: str, value: int) -> Slot:
    """A code-made count (a count line's bank count): ``{K1.c1}`` = "2"."""
    return Slot("%s.%s" % (source_id, name), source_id, str(int(value)), "count", role="count")


def slot_table(slots) -> str:
    """The prompt's slot table: ``{R3.n1} = %36 (JPMorgan, usd/try)``."""
    def note(s):
        bits = [s.bank] if s.bank else []
        if s.series:
            bits.append(s.series)
        if s.role == "pctl":
            bits.append("yüzdelik: '... yüzdeliğinde' diye yaz")
        if s.sign:
            bits.append("düşüş/indirim yönünde" if s.sign < 0 else "artış yönünde")
        return " (%s)" % ", ".join(bits) if bits else ""
    return "\n".join("{%s} = %s%s" % (s.id, s.display, note(s)) for s in slots)


# ───────────────────────────────────────────── gate 1: the model's raw text
def _plain(text: str) -> str:
    return _NS_CITE.sub(" ", SLOT.sub(" ", text or ""))


#: treasury-news PR #3 review: suffixed number words too ("ikincisi", "yarısını", "üçünü",
#: "çeyreği"), every ordinal ("birinci...", any ending), English number words; common words
#: that only LOOK like a stem + suffix are kept.
_CASE = ("n", "in", "ın", "un", "ün", "nin", "nın", "nun", "nün", "ni", "nı", "nu", "nü", "e", "a",
         "ye", "ya", "ne", "na", "de", "da", "nde", "nda", "den", "dan", "nden", "ndan", "ten", "tan",
         "le", "la", "yle", "yla", "ler", "lar", "si", "sı", "su", "sü", "sin", "sın", "sun", "sün")
_NUMWORD_CHAIN = re.compile(r"^(?:%s)+(?:%s)(?:%s){0,2}$" % (
    "|".join(sorted(NUMBER_STEMS, key=len, reverse=True)),
    "|".join(sorted([x for x in NUMBER_SUFFIXES if x], key=len, reverse=True)),
    "|".join(sorted(_CASE, key=len, reverse=True))))
_ORDINAL = re.compile(r"^(?:bir|%s)+(?:inci|ıncı|uncu|üncü|nci|ncı|ncu|ncü)\w*$"
                      % "|".join(sorted(NUMBER_STEMS + ("dörd",), key=len, reverse=True)))
NUMBER_PREFIXES = ("çeyre", "yarım", "yarıs", "yarıy", "buçu", "düzine")
NUMBER_TOKENS_EN = ("halves", "quarters", "double", "twice", "triple", "dozens", "thousand",
                    "million", "billion", "trillion", "first", "second", "third", "fourth", "fifth",
                    "sixth", "seventh", "eighth", "ninth", "tenth", "hundredth", "pct")
NOT_NUMBERS = ("altında", "altına", "altından", "altındaki", "ona", "onu", "onun", "onlar", "onları",
               "onların", "onlara", "onda", "ondan", "yüzü", "yüzünden", "bine", "ikide")
_ROMAN_FULL = re.compile(r"(?<!\w)(?=[MDCLXVI])M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})(?!\w)")


def raw_numbers(text: str) -> list[str]:
    """Digits, numerals and number words the model wrote itself (outside slot ids/citations)."""
    plain = _plain(text)
    found = [ch for ch in plain if ch.isdigit() or ch.isnumeric()]
    found += _ROMAN.findall(plain)
    found += [m.group(0) for m in _ROMAN_FULL.finditer(plain) if len(m.group(0)) >= 1
              and m.group(0) not in found and m.group(0) not in ("I",)]
    for w in re.findall(r"[^\W\d_]+", _tr_lower(plain)):
        if w in NOT_NUMBERS:
            continue
        if (w in NUMBER_TOKENS or w in NUMBER_TOKENS_EN or _NUMWORD.match(w) or _NUMWORD_CHAIN.match(w)
                or _ORDINAL.match(w) or w.startswith(NUMBER_PREFIXES)):
            found.append(w)
    return found


def stray_braces(text: str) -> list[str]:
    """A brace that is not part of a valid slot id ("{{R4.n1}}", "{hedef}", "{R4.n1")."""
    rest = SLOT.sub("", text or "")
    return [ch for ch in rest if ch in "{}｛｝"]


def negations(text: str) -> list[str]:
    return _NEGATION.findall(_tr_lower(_plain(text)))


#: treasury-news: more unit / scale / currency words that the slot renders itself.
UNIT_PREFIXES = ("puan", "yüzde", "milyon", "milyar", "trilyon", "lira", "dolar", "avro", "kuruş",
                 "binde", "mislin", "katın", "katl")
UNIT_WORDS_MORE = ("bin", "mn", "bn", "tn", "tl", "x", "percentage", "point", "points", "basis",
                   "euro", "sent", "misli", "negatif", "pozitif", "katını", "katında")


def _unit_word(w: str) -> bool:
    return w in UNIT_WORDS or w in UNIT_WORDS_MORE or w.startswith(UNIT_PREFIXES)


def _adjacent(text: str, used, slots) -> list[str]:
    """Nothing glued to a slot: the slot renders value + unit (+ sign) itself."""
    problems = []
    for idx, (s, e, sid) in enumerate(used):
        before, after = text[:s], text[e:]
        prev_ch = before[-1:] if before else ""
        next_ch = after[:1]
        if prev_ch and not (prev_ch.isspace() or prev_ch in "(\"“'"):
            same_range = (prev_ch in "–-" and idx > 0 and used[idx - 1][1] == s - 1
                          and sid in slots and used[idx - 1][2] in slots
                          and slots[used[idx - 1][2]].source == slots[sid].source)
            if not same_range:                         # "{a}–{b}": a range of one source only
                problems.append("yuvanın önüne yapışık işaret: '%s{%s}'" % (prev_ch, sid))
        if next_ch and not (next_ch.isspace() or next_ch in ".,;:)!?'’\"”"):
            if not (next_ch in "–-" and idx + 1 < len(used) and used[idx + 1][0] == e + 1):
                problems.append("yuvanın ardına yapışık metin: '{%s}%s'" % (sid, after[:4]))
        nxt = re.match(r"(?:'[^\W\d_]+)?[,;/]?\s+(\S+)", after)
        if nxt:
            w = _tr_lower(nxt.group(1)).strip(".,;:!?()\"'")
            if _unit_word(w) or SLOT.match(nxt.group(1)) or _NUMWORD.match(w) or w in NUMBER_TOKENS:
                problems.append("yuvanın yanında birim/sayı sözcüğü ya da ikinci yuva: '{%s} %s'" % (sid, w))
        prv = re.search(r"(\S+)\s+$", before)
        if prv:
            w = _tr_lower(prv.group(1)).strip(".,;:!?()\"'")
            if _unit_word(w) or _NUMWORD.match(w) or w in NUMBER_TOKENS:
                problems.append("yuvanın önünde birim/sayı sözcüğü: '%s {%s}'" % (w, sid))
    return problems


def check_slots(sentence: str, slots: dict, cited: set, owners: set, require_bank: bool = False,
          series_names=()) -> list[str]:
    """Problems of one model sentence (Turkish, empty = passes). ``slots`` id -> Slot of the
    whole spec; ``cited`` the source ids the sentence cites (count lines with their reports);
    ``owners`` the canonical banks of the cited sources; ``require_bank``: a bank's value
    must always have its bank named next to it (slide 1); ``series_names`` every series name
    the sources use (a slot under another series' name is refused)."""
    problems = ["kendi yazdığın sayı: %s — yalnız {…} yuvalarını kullan" % x for x in raw_numbers(sentence)]
    problems += ["yuva dışında süslü parantez: %s" % x for x in stray_braces(sentence)]
    problems += ["olumsuz fiil (kaynağın iddiasını tersine çevirir): %s" % x for x in negations(sentence)]
    used = [(m.start(), m.end(), "%s.%s" % (m.group(1), m.group(2))) for m in SLOT.finditer(sentence)]
    for _, _, sid in used:
        if sid not in slots:
            problems.append("bilinmeyen yuva: {%s}" % sid)
        elif slots[sid].source not in cited:
            problems.append("yuvanın kaynağına atıf yok: {%s} (%s)" % (sid, slots[sid].source))
    problems += _adjacent(sentence, used, slots)
    used = [u for u in used if u[2] in slots]
    text = SLOT.sub(lambda m: "#" * len(m.group(0)), _NS_CITE.sub(lambda m: " " * len(m.group(0)), sentence))
    mentions = bank_mentions(text)
    for b in dict.fromkeys(b for _, _, b in mentions):
        if b not in owners:
            problems.append("atıf yapılmayan kurum adı geçiyor: %s" % b)
    owned = [(s, e, sid) for s, e, sid in used if slots[sid].bank]
    subjects = [_subject(text, mentions, s) for s, _, _ in owned]
    owner = [list(x) for x in subjects]
    i = 0
    while i < len(owned):
        j = i
        while j + 1 < len(owned) and subjects[j + 1] == subjects[i] and re.match(
                r"^(?:'\w+)?\s*(?:,|ve|ile|and|&)\s*$", text[owned[j][1]:owned[j + 1][0]], re.I):
            j += 1
        if len(subjects[i]) > 1 and j - i + 1 == len(subjects[i]):
            for k in range(len(subjects[i])):
                owner[i + k] = [subjects[i][k]]
        i = j + 1
    for (s, e, sid), own in zip(owned, owner):
        slot = slots[sid]
        if own and slot.bank not in own:
            problems.append("{%s} %s'ın; cümlede %s'ın yanında" % (sid, slot.bank, ", ".join(own)))
        if not own and slot.kind == "value" and (require_bank or len(owners) > 1):
            problems.append("{%s} hangi kurumun olduğu yazılmamış (%s)" % (sid, slot.bank))
    series_hits = _series_mentions(text, series_names)
    low = _tr_lower(text)
    for s, e, sid in used:
        slot = slots[sid]
        if slot.kind == "value" and slot.series:
            before = [k for hs, he, k in series_hits if he <= s]
            if before and before[-1] != slot.series:
                problems.append("{%s} %s serisinin değeri; cümlede %s altında" % (sid, slot.series, before[-1]))
        if slot.kind == "label" and slot.role in ("date", "year", "period", "tenor"):
            prv = re.search(r"(\S+)\s+$", low[:s])
            if prv and prv.group(1).strip(".,;:") in LEVEL_WORDS:
                problems.append("{%s} bir etiket (%s); değer yerine kullanılmış" % (sid, slot.role))
        if slot.role == "pctl" and not _PCTL_AFTER.match(low[e:e + 14]):
            problems.append("{%s} bir yüzdelik; '{%s} yüzdeliğinde' diye yazılmalı" % (sid, sid))
        if slot.kind == "label" and slot.role in ("date", "year", "period", "tenor"):
            nxt_w = re.match(r"(?:'[^\W\d_]+)?\s*([^\W\d_]+)", low[e:])
            if nxt_w and nxt_w.group(1).startswith(VALUE_AFTER):
                problems.append("{%s} bir etiket (%s); değer yerine kullanılmış ('%s')" % (
                    sid, slot.role, nxt_w.group(1)))
            for s2, e2, sid2 in used:            # "{label} ile {value}": a label in a value range
                if sid2 != sid and slots[sid2].kind == "value" and re.fullmatch(
                        r"\s*(?:ile|ve|–|-|,)\s*", low[min(e, e2):max(s, s2)]):
                    problems.append("{%s} bir etiket; {%s} değeriyle aralık kurulmuş" % (sid, sid2))
        if slot.sign:
            # PR #3 review: the whole stretch up to the neighbouring slots, not 2-3 words
            prev_end = max([e2 for s2, e2, _ in used if e2 <= s] or [0])
            next_start = min([s2 for s2, e2, _ in used if s2 >= e] or [len(low)])
            near = re.findall(r"[^\W\d_]+", low[prev_end:s]) + re.findall(r"[^\W\d_]+", low[e:next_start])
            down = any(w.startswith(DOWN_WORDS) for w in near)
            up = any(w.startswith(UP_WORDS) for w in near)
            if (slot.sign < 0 and up and not down) or (slot.sign > 0 and down and not up):
                problems.append("{%s} yönü kaynakla çelişiyor" % sid)
    return problems


def check_headline(headline: str) -> list[str]:
    """The headline cites nothing: no slot, number, brace, bank name or negation."""
    problems = []
    if SLOT.search(headline or "") or raw_numbers(headline):
        problems.append("başlıkta sayı var")
    if stray_braces(headline):
        problems.append("başlıkta süslü parantez")
    for b in dict.fromkeys(b for _, _, b in bank_mentions(headline or "")):
        problems.append("başlıkta kurum adı (atıfsız iddia): %s" % b)
    problems += ["başlıkta olumsuz fiil: %s" % x for x in negations(headline)]
    return problems


# ───────────────────────────────────────────── fill + gate 2: read the final sentence again
def fill_spans(text: str, slots: dict) -> tuple[str, list[tuple[int, int, str]]]:
    """``(filled text, [(start, end, slot id)])`` - where each display landed."""
    out, spans, pos, length = [], [], 0, 0
    for m in SLOT.finditer(text or ""):
        sid = "%s.%s" % m.groups()
        chunk = text[pos:m.start()]
        out.append(chunk)
        length += len(chunk)
        disp = slots[sid].display if sid in slots else m.group(0)
        spans.append((length, length + len(disp), sid))
        out.append(disp)
        length += len(disp)
        pos = m.end()
    out.append((text or "")[pos:])
    return "".join(out), spans


def fill_slots(text: str, slots: dict) -> str:
    """Slot ids -> the source's displays."""
    return fill_spans(text, slots)[0]


def _reading(text: str):
    """(start, end, value, unit, sign) of every number of a text, labels masked."""
    masked = list(text)
    for s, e, _, _ in _spans(text, ()):
        masked[s:e] = " " * (e - s)
    masked = "".join(masked)
    out = []
    for n, unit, _ in read_values(masked):
        sign_m = _SIGN_BEFORE.search(masked[max(0, n.start - 4):n.start])
        out.append((n.start, n.end, round(n.value, 9), unit,
                    -1 if sign_m and sign_m.group(1) in "−-" else +1 if sign_m else 0))
    return out


def verify_filled(sentence: str, slots: dict) -> list[str]:
    """The FINAL sentence read again (owner, pass 6): every number lies inside a filled slot
    and reads with the same value, unit and sign as that slot's own display; no brace left."""
    filled, spans = fill_spans(sentence, slots)
    problems = []
    if stray_braces(filled) or SLOT.search(filled):
        problems.append("doldurulmuş cümlede süslü parantez kaldı: %s" % filled[:60])
    starts = {sp[0] for sp in spans}
    ends = {sp[1] for sp in spans}
    for s, e, sid in spans:                    # the filled value's borders (%%36, −%36, 15,20lik)
        prev = filled[s - 1:s]
        nxt = filled[e:e + 1]
        if prev and (prev.isalnum() or prev in "%−+" or (prev in "-–" and s - 1 not in ends)):
            problems.append("{%s} doldurulunca önüne yapışık: '%s'" % (sid, filled[max(0, s - 3):e]))
        if nxt and (nxt.isalnum() or nxt == "%" or (nxt in "-–" and e + 1 not in starts)):
            problems.append("{%s} doldurulunca ardına yapışık: '%s'" % (sid, filled[s:e + 4]))
    for s, e, value, unit, sign in _reading(_NS_CITE.sub(lambda m: " " * len(m.group(0)), filled)):
        inside = [sp for sp in spans if sp[0] <= s and e <= sp[1]]
        if not inside:
            problems.append("doldurulmuş cümlede yuva dışı sayı: %s" % filled[s:e])
            continue
        sid = inside[0][2]
        if sid not in slots or slots[sid].kind == "label":
            continue
        own = [(v, u, g) for _, _, v, u, g in _reading(slots[sid].display)]
        if (value, unit, sign) not in own:
            problems.append("{%s} cümlede '%s' okunuyor; kaynakta '%s'" % (
                sid, filled[max(0, s - 2):e + 9].strip(), slots[sid].display))
    return problems

# ───────────────────────────────────────────── instrument key (treasury-report link_gate)
_FOLD = str.maketrans("İIıĞğÜüŞşÖöÇç", "IIIGGUUSSOOCC")


def instrument_key(text):
    """The map key of an instrument as printed: Turkish letters folded, accents dropped,
    upper case, everything but letters and digits removed ("USD/TRY" -> "USDTRY")."""
    s = (text or "").translate(_FOLD)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]", "", s.upper())



# ##########################################################################################
# 4. the summary (treasury-report platform_kernel/research_summary.py)
# ##########################################################################################



# ════════════════════════════════════════════════════════════════════════════ SETTINGS
#: At most this many reports go into one summary (owner, 2026-10-20: "the first 30").
MAX_REPORTS = 30
#: Agreement/divergence lines: instruments at least this many distinct banks wrote about,
#: most-covered first, at most MAX_GROUPS of them.
MIN_GROUP_BANKS = 2
MAX_GROUPS = 6
#: Bullets of the summary (the model's range; the template writes at most MAX_BULLETS).
MIN_BULLETS, MAX_BULLETS = 3, 5
HEADLINE_MAX_WORDS = 8
#: A report's fact line carries at most this many key points (those with a number first).
KEY_POINTS_PER_REPORT = 2
#: Primary subjects that never enter the summary (treasury-report's rule; treasury-news
#: summaries carry no ``subjects`` yet, so today this only acts if that field appears).
DROP_SUBJECTS = ("equity", "non_research")
PROMPT_VER = "rs-6-tn4"     # PRISMA pass 6 + PR #3 reviews (series per slot, labels, names, negation)
NAME = "Araştırma özeti"
TEMPLATE_HEADLINE = "Kurumların bugünkü görüşleri"

#: Equity reports among the digest-only ones (beyond the mail's curated reports; owner, PR #3
#: review). Curation keeps no exclude reasons, so three marks that ARE stored are read:
#: the summary headline's asset class ("Equity, TR: ..."), the bank's own report types
#: (REPORTS.REPORT_TYPES: Citi asset classes, JPM product category) and equity-only title words.
EQUITY_WORDS = re.compile(r"\b(?:equit(?:y|ies)|stocks?|shares|single[- ]stocks?|earnings)\b", re.I)
#: A report-type list that also names one of these is not equity-only (cross-asset etc.).
NON_EQUITY_TYPES = re.compile(r"\b(?:rates?|fx|currenc\w*|foreign exchange|credit|fixed income|"
                              r"macro\w*|econom\w*|commodit\w*|cross[- ]asset|multi[- ]asset|"
                              r"sovereign\w*|bonds?|treasur\w*)\b", re.I)
EQUITY_TITLE = re.compile(r"\b(?:earnings (?:preview|review|recap|results)|price targets?|"
                          r"initiat\w* (?:of )?coverage|IPOs?|buybacks?|top (?:stock )?picks|"
                          r"(?:upgrade|downgrade)s? to (?:buy|sell|hold|neutral|outperform|"
                          r"underperform|equal[- ]weight))\b", re.I)

#: Display names of the source keys (sources/*.py ``key``).
SOURCE_NAMES = {"gs": "Goldman Sachs", "jpm": "JPMorgan", "citi": "Citi",
                "ms": "Morgan Stanley", "db": "Deutsche Bank", "barc": "Barclays"}
#: REPORT_SUMMARY.stance in Turkish ("n/a" never prints).
STANCE_TR = {"bullish": "olumlu", "bearish": "olumsuz", "neutral": "nötr", "mixed": "karışık"}
#: The template's verb per stance (owner, PR #3 review): what the bank expects for the
#: instrument, without "olumlu/olumsuz görüşte". Bond-like instruments keep a verb with no
#: direction (bullish on a bond is a price view, the label may name its yield).
STANCE_VERB = {"olumlu": "yükseliş bekliyor", "olumsuz": "düşüş bekliyor",
               "nötr": "yatay seyir bekliyor", "karışık": "karışık bir görüş bildiriyor"}
STANCE_VERB_BOND = {"olumlu": "olumlu bakıyor", "olumsuz": "olumsuz bakıyor"}
NO_STANCE_VERB = "yön belirtmiyor"
BOND_LIKE = re.compile(r"\b(?:UST|Bunds?|JGBs?|gilts?|BTPs?|OATs?|treasur\w*|bonds?|tahvil\w*)\b", re.I)
#: English instrument / asset-class words -> Turkish, for the template's labels only (the
#: model's facts keep the report's own words). Longest phrase first; tickers and pairs stay.
LABEL_TR = (
    ("fed funds rate", "Fed fon faizi"), ("fed funds", "Fed fon faizi"),
    ("policy rate", "politika faizi"), ("deposit rate", "mevduat faizi"),
    ("real rates", "reel faiz"), ("interest rates", "faiz"), ("rates", "faiz"), ("rate", "faiz"),
    ("yields", "getiri"), ("yield", "getiri"), ("bonds", "tahvil"), ("bond", "tahvil"),
    ("yield curve", "getiri eğrisi"), ("curve", "eğri"), ("spreads", "makas"), ("spread", "makas"),
    ("breakevens", "başabaş enflasyon"), ("inflation", "enflasyon"), ("CPI", "TÜFE"),
    ("GDP", "GSYH"), ("growth", "büyüme"), ("unemployment", "işsizlik"),
    ("labor market", "işgücü piyasası"), ("labour market", "işgücü piyasası"),
    ("FX", "döviz"), ("currencies", "döviz"), ("credit", "kredi"), ("commodities", "emtia"),
    ("oil", "petrol"), ("gold", "altın"), ("equities", "hisse senetleri"), ("macro", "makro"),
    ("emerging markets", "gelişen piyasalar"), ("EM", "gelişen piyasalar"),
    ("United States", "ABD"), ("US", "ABD"), ("UK", "Birleşik Krallık"), ("euro area", "Euro Bölgesi"),
    ("Turkey", "Türkiye"), ("Turkiye", "Türkiye"), ("Japan", "Japonya"), ("China", "Çin"),
)
# ═════════════════════════════════════════════════════════════════════════════════════

_TR_CHARS = re.compile(r"[çğıİöşüÇĞÖŞÜ]")
_CITE = re.compile(r"\[([KR])(\d+)\]")
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)

SYSTEM = """Sen bir hazine biriminin günlük araştırma bülteninin yazarısın. Görevin, bankaların bugünkü araştırma raporlarından çıkarılmış numaralı olgulardan bir yönetici özeti yazmak: kısa bir başlık ve kısa, sade Türkçe maddeler. Okuyucu raporların kendisini ayrıca görür; özet bugün kurumların ne söylediğini, nerede birleştiklerini ve nerede ayrıştıklarını söyler.

Kurallar:
1. Yalnızca verilen olguları kullan. Olgularda olmayan bilgi ya da yorum ekleme.
2. HİÇBİR RAKAMI ya da sayı sözcüğünü kendin yazma. Olgulardaki her sayı, tarih, vade ve rakamlı ad süslü parantezli bir YUVADIR, örneğin {R3.n1}; sayıya ihtiyacın varsa yuvayı aynen yaz ({R3.n1}), değerini kod yerleştirir. Yuva tablosu her yuvanın değerini ve kurumunu gösterir. Bir yuvayı kullandığın cümle, yuvanın raporuna atıf yapar ({R3.n1} → [R3]). Bir kurumun yuvasını o kurumun adının yanında yaz; iki kurumu "A ve B" diye yazarsan yuvaları da aynı sırayla yaz. Yuvanın yönünü değiştirme (indirim yuvası "artış" olmaz). Bir cümlede en fazla 2 değer yuvası olsun. Bir kurumun adını yalnız o kurumun raporuna atıf yapıyorsan yaz. Yuvanın yanına birim, işaret ya da ölçek YAZMA (%, eksi, artı, "baz puan", "puan", "kat", "milyar", "TL" yasak): yuva değeri birimiyle birlikte kendisi yazar. İki yuvayı yan yana ya da yalnız tire/virgülle yazma; aralık için "{R5.n1} ile {R5.n2} arasında" yaz. "Çeyrek", "yarım", "ikinci", "üçte bir" gibi sayı sözcükleri ve Roma rakamları da yazma. Bir değer yuvasını yuva tablosunda yazan serinin (örneğin "policy rate" → politika faizi) değeri olarak yaz; bir serinin sayısını başka bir serinin adının altında yazma. Tarih, yıl, dönem ve vade yuvaları etikettir, değer gibi kullanma ("{R7.l1} seviyesinde" yasak). Olgularda geçmeyen kurum adı yazma. Hiçbir fiili olumsuz yazma ("beklemiyor", "yapmadı", "değil" yasak).
3. Her cümlenin sonuna dayandığı raporların numaralarını köşeli parantezle yaz, örneğin [R2] ya da [R3][R7][R12]. Atıfsız cümle yazma; bir maddede iki cümle varsa ikisi de atıf taşır. İki nokta (:) ve satır sonu kullanma.
4. Özet bütün raporları saymak zorunda değildir; günün genel resmini ver. Aynı görüşteki raporları TEK maddede topla ve hepsine atıf yap (örneğin "USD/TRY için yıl sonu beklentisi {R3.n1} ile {R7.n1} arasında [R3][R7]."); ayrışan görüşü ayrı bir maddede, kendi atfıyla yaz.
4a. [K] olguları bir enstrüman için bir görüşü taşıyan kurumları ve raporlarını söyler; [K] numarası o raporların yerine geçer. Kurum sayısını [K] yuvasıyla yaz ("{K1.c1} kurum").
5. Bir kurumun görüşünü kurumun adıyla ve kurumun fiiliyle yaz ("öngörüyor", "bekliyor", "öneriyor"); görüşü kendi ağzından yazma, "piyasa bekliyor" diye genelleme. Tahmin fiilini olumsuz yazma ("beklemiyor", "öngörmüyor" yasak); raporun görüşünü olumlu fiille aktar.
6. Neden-sonuç kurma ("çünkü", "nedeniyle", "sayesinde" yasak). Yatırım tavsiyesi verme; "fırsat", "cazip", "alınmalı" gibi sözler yazma. "Rekor", "ilk kez", "en yüksek" yazma.
7. {count} madde yaz. Önemli olanı önce yaz: önce birden çok kurumun yazdığı konular, uzlaşı ve ayrışma. Her madde tek fikirli, en fazla 25 kelimelik bir cümledir. Aynı konuyu iki maddede tekrarlama.
8. Başlık en fazla 8 kelime olsun, hiçbir sayı ve kurum adı içermesin ve günün ana mesajını yansıtsın.
9. Olgular İngilizce olabilir; sen yalnız Türkçe yaz. Rakamsız enstrüman adlarını (EUR/USD gibi) olduğu gibi bırak; rakamlı adlar da yuvadır.
10. Yanıtı yalnızca şu JSON biçiminde ver: {{"baslik": "...", "maddeler": ["...", "..."]}}. Metni Türkçe karakterlerle (ç, ğ, ı, İ, ö, ş, ü) yaz."""

#: Made-up banks and slots: the shape only.
EXAMPLE = (
    "Gün: örnek gün\nRapor olguları:\n"
    "[R1] Örnek Banka A: EUR rates: ECB on hold into year end. Görüş: nötr. "
    "Önemli nokta: Deposit rate stays at {R1.n1} through {R1.l1}.\n"
    "[R2] Örnek Banka B: EUR rates: ECB to cut in December. Görüş: olumlu. "
    "Önemli nokta: One {R2.n1} cut expected in December.\n"
    "[R3] Örnek Banka C: EUR rates: ECB on hold. Görüş: nötr. "
    "Önemli nokta: Deposit rate at {R3.n1} into next year.\n"
    "Uzlaşı ve ayrışma olguları:\n"
    "[K1] ECB: nötr görüş — Örnek Banka A, Örnek Banka C ({K1.c1} kurum); ECB için toplam {K1.c2} "
    "kurum görüş bildirdi, kurumlar ayrışıyor. Raporlar: [R1], [R3]\n"
    "[K2] ECB: olumlu görüş — Örnek Banka B ({K2.c1} kurum); ECB için toplam {K2.c2} kurum görüş "
    "bildirdi, kurumlar ayrışıyor. Raporlar: [R2]\n"
    "Yuvalar:\n{R1.n1} = %2,00 (Örnek Banka A)\n{R1.l1} = 4. çeyrek\n"
    "{R2.n1} = 25 baz puan (Örnek Banka B)\n{R3.n1} = %2,00 (Örnek Banka C)\n"
    "{K1.c1} = 2\n{K1.c2} = 3\n{K2.c1} = 1\n{K2.c2} = 3",
    '{"baslik": "ECB yolunda kurumlar ayrışıyor", "maddeler": ["Örnek Banka A ve Örnek Banka C '
    'mevduat faizinin {R1.n1} ve {R3.n1} düzeyinde kalacağını öngörüyor [R1][R3].", '
    '"Örnek Banka B ise aralıkta {R2.n1} indirim bekliyor [R2].", '
    '"ECB için {K1.c2} kurumdan {K1.c1}\'si sabit faiz görüşünde birleşiyor [K1]."]}')

RETRY = ("Yanıt kurallara uymuyor: %s. Rakam yazma; sayılar için yalnız {…} yuvalarını kullan, "
         "her cümleye yuvanın raporunun atfını ekleyerek yeniden yaz.")

VERIFIED, TEMPLATE, NO_MODEL = "verified", "template", "no_model"


# ───────────────────────────────────────────── seçim
def source_name(source: str | None) -> str:
    key = (source or "").strip().lower()
    return SOURCE_NAMES.get(key, key.upper() or "?")


def _summary(r: dict) -> dict:
    s = r.get("summary")
    return s if isinstance(s, dict) else {}


def usable(r: dict) -> bool:
    """A report enters the summary: it has a real summary and its primary subject is not
    equity / non-research (an untagged summary is kept, as in the mail's own rule)."""
    s = _summary(r)
    headline = (s.get("headline") or "").strip()
    if not headline or headline == "NO_CONTENT":
        return False
    subjects = s.get("subjects") or []
    return not (subjects and subjects[0] in DROP_SUBJECTS)


def is_equity(r: dict) -> bool:
    """True when a stored mark says the report is equity research (see ``EQUITY_WORDS``):
    its summary headline's asset class, its equity-only report types, or its title."""
    head = (_summary(r).get("headline") or "").split(":", 1)[0].split(",", 1)[0]
    if EQUITY_WORDS.search(head):
        return True
    types = [str(x) for x in r.get("report_types") or () if x]
    if any(EQUITY_WORDS.search(x) for x in types) and not any(NON_EQUITY_TYPES.search(x) for x in types):
        return True
    return bool(EQUITY_TITLE.search(r.get("title") or ""))


def pick_reports(mail_reports: list[dict], digest_reports: list[dict] = (),
                 max_reports: int = MAX_REPORTS) -> list[dict]:
    """The mail's reports in mail order, then the day's digest in digest order (owner D16),
    each report once, ``usable`` only, at most ``max_reports``. The mail's reports passed
    curation; a digest-only one is also left out when ``is_equity``."""
    out, seen = [], set()
    mail_ids = {r.get("id") for r in mail_reports}
    for r in list(mail_reports) + list(digest_reports or ()):
        rid = r.get("id")
        if rid in seen or not usable(r) or (rid not in mail_ids and is_equity(r)):
            continue
        seen.add(rid)
        out.append(r)
        if len(out) >= max_reports:
            break
    return out


# ───────────────────────────────────────────── olgular
def _key_points(s: dict) -> list[str]:
    points = []
    for kp in s.get("key_points") or ():
        p = (kp.get("point") if isinstance(kp, dict) else str(kp or "")) or ""
        p = p.strip()
        if p:
            points.append(p)
    with_numbers = [p for p in points if numbers(p)]
    rest = [p for p in points if p not in with_numbers]
    return (with_numbers + rest)[:KEY_POINTS_PER_REPORT]


def report_fact(r: dict) -> str:
    """The report's grounding line: bank: headline. Görüş: <stance>. Önemli nokta: ...
    No date: the report date is not a fact a bullet may cite (review 2026-10-20, its digits
    used to ground invented numbers); the date stays in the source list."""
    s = _summary(r)
    parts = ["%s: %s" % (source_name(r.get("source")),
                         (s.get("headline") or r.get("title") or "").strip().rstrip("."))]
    stance = STANCE_TR.get((s.get("stance") or "").strip().lower())
    if stance:
        parts.append("Görüş: %s" % stance)
    for p in _key_points(s):
        parts.append("Önemli nokta: %s" % p.rstrip("."))
    return ". ".join(parts) + "."


def _instrument(r: dict) -> tuple[str, str] | None:
    """(key, label) of the report's main instrument: its first named instrument, else the
    headline's "<asset class>, <country>" part (the summary prompt's headline shape)."""
    s = _summary(r)
    for inst in s.get("instruments") or ():
        label = str(inst or "").strip()
        if instrument_key(label):
            return instrument_key(label), label
    head = (s.get("headline") or "").split(":", 1)
    if len(head) == 2 and instrument_key(head[0]):
        return instrument_key(head[0]), head[0].strip()
    return None


def groups(reports: list[dict]) -> list[dict]:
    """Agreement/divergence per main instrument: the distinct banks per stance, the reports'
    R numbers. Only instruments ``MIN_GROUP_BANKS``+ banks wrote about; most banks first,
    then the first report's position (mail order)."""
    by: dict[str, dict] = {}
    for i, r in enumerate(reports, 1):
        inst = _instrument(r)
        if inst is None:
            continue
        key, label = inst
        g = by.setdefault(key, {"key": key, "label": label, "reports": [], "stances": {},
                                "stance_reports": {}, "banks": set(), "first": i})
        g["reports"].append("R%d" % i)
        bank = source_name(r.get("source"))
        g["banks"].add(bank)
        stance = STANCE_TR.get((_summary(r).get("stance") or "").strip().lower())
        if stance:
            g["stances"].setdefault(stance, set()).add(bank)
            g["stance_reports"].setdefault(stance, []).append("R%d" % i)
    out = []
    for g in by.values():
        if len(g["banks"]) < MIN_GROUP_BANKS:
            continue
        out.append({"key": g["key"], "label": g["label"], "reports": g["reports"],
                    "n_banks": len(g["banks"]), "stances": {k: sorted(v) for k, v in g["stances"].items()},
                    "stance_reports": dict(g["stance_reports"]),
                    "diverges": len(g["stances"]) > 1, "first": g["first"]})
    out.sort(key=lambda g: (-g["n_banks"], g["first"]))
    return out[:MAX_GROUPS]


def stance_lines(g: dict) -> list[tuple[str, list[str], list[str]]]:
    """``[(stance, banks, reports)]`` of one instrument, most banks first."""
    return sorted(((st, g["stances"][st], g["stance_reports"][st]) for st in g["stances"]),
                  key=lambda x: (-len(x[1]), x[0]))


def stance_fact(g: dict, stance: str, banks: list[str]) -> str:
    """"<instrument>: <stance> görüş — A, B (2 kurum); <instrument> için toplam <n> kurum ..." """
    tail = "kurumlar ayrışıyor" if g["diverges"] else "kurumlar aynı yönde"
    return "%s: %s görüş — %s (%d kurum); %s için toplam %d kurum görüş bildirdi, %s." % (
        g["label"], stance, ", ".join(banks), len(banks), g["label"], g["n_banks"], tail)


@dataclass
class Spec:
    """What the model is shown and what the gate checks against."""

    reports: list[dict]
    facts: dict = field(default_factory=dict)        # "K1" -> number_gate fact
    claims: dict = field(default_factory=dict)       # "R1" -> number_gate claim
    members: dict = field(default_factory=dict)      # "K1" -> ["R1", "R4"]
    groups: list = field(default_factory=list)
    slots: dict = field(default_factory=dict)        # "R1.n1" -> number_slots.Slot
    banks: dict = field(default_factory=dict)        # "R1" -> "Goldman Sachs"
    fact_banks: dict = field(default_factory=dict)   # "R1"/"K1" -> {canonical banks}
    names: list = field(default_factory=list)        # instrument labels of every report
    series_names: list = field(default_factory=list)  # series a slot may belong to
    user: str = ""
    label: str = ""

    @property
    def key(self) -> str:
        h = hashlib.sha256()
        for part in (PROMPT_VER, self.user):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    @property
    def n_bullets(self) -> tuple[int, int]:
        n = len(self.reports)
        return min(MIN_BULLETS, n), min(MAX_BULLETS, max(n, 1))


def build_spec(reports: list[dict], label: str = "") -> Spec | None:
    """The numbered facts of ``reports`` (already picked and ordered); None when empty."""
    if not reports:
        return None
    spec = Spec(reports=list(reports), label=label)
    lines = ["Gün: %s" % (label or reports[0].get("date") or ""), "Rapor olguları:"]
    spec.names = _instrument_labels(reports)
    spec.series_names = sorted({str(i).strip() for r in reports
                                for i in _summary(r).get("instruments") or () if str(i).strip()}
                               | {x[1] for x in (_instrument(r) for r in reports) if x}
                               | set(SERIES_ALIASES))
    for i, r in enumerate(reports, 1):
        text = report_fact(r)
        rid = "R%d" % i
        spec.claims[rid] = {"text": text, "numbers": [], "quote": ""}
        spec.banks[rid] = canonical_bank(source_name(r.get("source")))
        spec.fact_banks[rid] = {spec.banks[rid]}
        main = _instrument(r)          # each NUMBER gets its own series (the last one named before it)
        slotted, slots = slots_for(rid, text, spec.names, bank=spec.banks[rid],
                                   series_names=spec.series_names,
                                   default_series=main[1] if main else None)
        spec.slots.update({x.id: x for x in slots})
        lines.append("[%s] %s" % (rid, slotted))
    spec.groups = groups(reports)
    if spec.groups:
        lines.append("Uzlaşı ve ayrışma olguları:")
    j = 0
    for g in spec.groups:
        for stance, banks, members in stance_lines(g):
            j += 1
            kid = "K%d" % j
            text = stance_fact(g, stance, banks)
            spec.facts[kid] = {"text": text, "numbers": []}
            spec.fact_banks[kid] = {canonical_bank(b) for b in banks}
            c1, c2 = count_slot(kid, "c1", len(banks)), count_slot(kid, "c2", g["n_banks"])
            spec.slots.update({c1.id: c1, c2.id: c2})
            slotted = text.replace("(%d kurum)" % len(banks), "({%s} kurum)" % c1.id, 1).replace(
                "toplam %d kurum" % g["n_banks"], "toplam {%s} kurum" % c2.id, 1)
            spec.members[kid] = list(members)
            lines.append("[%s] %s Raporlar: %s" % (kid, slotted, ", ".join("[%s]" % m for m in members)))
    lines.append("Yuvalar:")
    lines.append(slot_table(spec.slots.values()))
    lo, hi = spec.n_bullets
    lines.append("Madde sayısı: %s" % ("%d" % lo if lo == hi else "%d ile %d" % (lo, hi)))
    spec.user = "\n".join(lines)
    return spec


# ───────────────────────────────────────────── yazım
def _parse(reply) -> dict:
    if isinstance(reply, dict):
        return reply
    text = _THINK.sub("", str(reply or ""))
    if "</think>" in text.lower():
        text = text[text.lower().rfind("</think>") + len("</think>"):]
    for m in re.finditer(r"\{", text):        # the first complete object; trailing text is ignored
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("yanıtta JSON yok")


def validate(out, spec: Spec) -> list[str]:
    """Problems with a model answer (empty = publish)."""
    if not isinstance(out, dict):
        return ["JSON biçimi beklenen gibi değil"]
    headline = str(out.get("baslik") or "").strip()
    bullets = out.get("maddeler")
    if not isinstance(bullets, list) or not all(isinstance(b, str) for b in bullets):
        return ["'maddeler' bir metin listesi değil"]
    bullets = [b.strip() for b in bullets if b.strip()]
    if not _TR_CHARS.search(headline + " ".join(bullets)):
        return ["metinde hiç Türkçe karakter yok; ı, ş, ğ, ü, ö, ç ve İ harfleriyle Türkçe yaz"]
    errors = []
    if not headline:
        errors.append("başlık yok")
    elif len(headline.split()) > HEADLINE_MAX_WORDS:
        errors.append("başlık %d kelimeden uzun" % HEADLINE_MAX_WORDS)
    else:                            # the headline cites nothing: no number, brace, bank, negation
        errors += check_headline(headline)
        errors += ["başlık: %s" % p for p in unknown_names(headline, spec)]
        errors += ["başlık: %s" % p for p in check_sentence(mask_digits(headline), {}, {}, limits=False)]
    lo, hi = spec.n_bullets
    if not lo <= len(bullets) <= hi:
        errors.append("%d madde yazıldı (%d–%d olmalı)" % (len(bullets), lo, hi))
    if len(set(bullets)) != len(bullets):
        errors.append("aynı madde iki kez yazıldı")
    for b in bullets:
        sentences = split_claims(b)
        uncited = [x for x in sentences if not _CITE.search(x)]
        if uncited or not sentences:
            errors.append("atıfsız cümle: %s" % (uncited[0] if uncited else b)[:60])
            continue
        for x in sentences:          # each sentence against ITS OWN cited reports (D16)
            errors += ["%s (%s)" % (p, x[:50]) for p in check_numbers(x, spec)]
            errors += ["%s (%s)" % (p, x[:50]) for p in claim_problems(x, spec)]
            errors += ["%s (%s)" % (p, x[:50]) for p in verify_filled(x, spec.slots)]
            errors += ["%s (%s)" % (p, x[:50]) for p in check_sentence(
                mask_digits(SLOT.sub("#", with_members(x, spec))), spec.facts,
                spec.claims, limits=False)]
            n_values = sum(1 for m in SLOT.finditer(x)
                           if spec.slots.get("%s.%s" % m.groups(), Slot("", "", "", "")).kind == "value")
            if n_values > MAX_VALUES:
                errors.append("cümlede %d değer yuvası var (en çok %d) (%s)" % (
                    n_values, MAX_VALUES, x[:50]))
            if word_count(x) > MAX_WORDS:
                errors.append("cümle %d kelime (en çok %d) (%s)" % (
                    word_count(x), MAX_WORDS, x[:50]))
    return errors


def with_members(text: str, spec: Spec) -> str:
    """For the number gate: a [K#] also cites its own reports ([K1] → [K1][R3][R7]), so a
    number in a grouped claim is checked against the facts of the reports it stands for."""
    return re.sub(r"\[K(\d+)\]", lambda m: m.group(0) + "".join(
        "[%s]" % r for r in spec.members.get("K" + m.group(1), [])), text)


# ───────────────────────────────────────────── sayı yuvaları (section 3)
# pr-review pass 5 (owner): the model never writes a number - it writes slot ids, code fills
# the source's exact values. The gate only checks: no raw digit / number word, every slot
# exists and its report is cited in the sentence, a named bank owns a cited report, a value
# slot sits next to its own bank (joint subjects respectively), no contradicting direction.
def _instrument_labels(reports: list[dict]) -> list[str]:
    names = {str(i).strip() for r in reports for i in _summary(r).get("instruments") or ()}
    return sorted((n for n in names if n and _DIGITS.search(n)), key=len, reverse=True)


def _cited_ids(sentence: str, spec: Spec) -> set:
    ids = set()
    for kind, n in _CITE.findall(sentence):
        cid = kind + n
        ids.add(cid)
        ids.update(spec.members.get(cid, []))
    return ids


def check_numbers(sentence: str, spec: Spec) -> list[str]:
    """``number_slots.check`` against what this sentence cites ([K#] brings its reports)."""
    cited = _cited_ids(sentence, spec)
    owners = set().union(*(spec.fact_banks.get(c, set()) for c in cited)) if cited else set()
    return check_slots(sentence, spec.slots, cited, owners, series_names=spec.series_names)




# ───────────────────────────────────────────── PR #3 reviews: names, English negation, re-read
#: Research houses not in BANK_ALIASES (which already lists HSBC, UBS, Nomura, BofA, BNP ...):
#: named in a summary = refused (none of them owns a cited report).
OTHER_BANKS = ("MUFG", "SMBC", "BBVA", "Jefferies", "RBC", "Scotiabank", "TD Securities", "Danske",
               "SEB", "Nordea", "Rabobank", "ABN AMRO", "Macquarie", "UniCredit", "Intesa",
               "Crédit Agricole", "Credit Agricole", "Bernstein", "Evercore", "Lazard", "Pictet",
               "Capital Economics", "Oxford Economics", "İş Yatırım", "Örnek Banka")
#: Public institutions and other proper names a summary may write (lower case, folded words).
PUBLIC_BANKS = ("merkez bankası", "central bank", "reserve bank", "world bank", "dünya bankası",
                "yatırım bankası", "kalkınma bankası", "investment bank", "development bank",
                "bank of england", "bank of japan", "bank of canada", "people's bank")
PROPER_OK = ("fed", "fomc", "ecb", "amb", "tcmb", "boe", "boj", "boc", "snb", "rba", "rbnz", "pboc",
             "merkez", "bankası", "avrupa", "euro", "bölgesi", "abd", "türkiye", "türk", "japonya",
             "çin", "almanya", "ingiltere", "birleşik", "krallık", "hazine", "imf", "opec", "tüfe",
             "üfe", "gsyh", "brent", "wti", "bist", "eur", "usd", "try", "tl", "jpy", "gbp", "chf",
             "cny", "bund", "ust", "jgb", "gilt", "hazine", "ocak", "şubat", "mart", "nisan", "mayıs",
             "haziran", "temmuz", "ağustos", "eylül", "ekim", "kasım", "aralık", "politika", "faizi")
#: The model's own sentence openers that are ordinary words, not names.
COMMON_STARTS = ("kurum", "banka", "rapor", "bu", "bun", "ayrıca", "öte", "diğer", "her", "tüm",
                 "faiz", "kur", "enflasyon", "getiri", "uzlaşı", "ayrışma", "yıl", "kısa", "orta",
                 "uzun", "vade", "dolar", "euro", "lira", "piyasa", "büyüme", "politika", "mevduat",
                 "tahvil", "emtia", "petrol", "altın", "hisse", "döviz", "makas", "eğri", "reel",
                 "işsizlik", "konut", "ise", "buna", "ancak", "aynı", "farklı", "görüş")
_OTHER_BANKS = [re.compile(r"(?<![\w.])%s(?![\w])" % re.escape(b), 0 if b.isupper() else re.I)
                for b in OTHER_BANKS]
_GENERIC_BANK = re.compile(r"\b(?:[A-ZÇĞİÖŞÜ][\w&.'-]*\s+){1,3}(?:Bank|Banka|Bankası|Securities|Capital|"
                           r"Markets|Research|Partners|Asset Management|Yatırım|Menkul)\b")
_NEGATED_EN = re.compile(r"\b(?:do|does|did)(?:\s+not|n't)\s+\w+|\bno\s+longer\b|\bnot\s+expected\b"
                         r"|\bnever\b|\bunlikely\b", re.I)
_CAPWORD = re.compile(r"[^\s\[\](){}\"“”,;:!?]+")


def unknown_banks(text: str) -> list[str]:
    """Bank / research-house names that are not one of the sources."""
    plain = _NS_CITE.sub(" ", SLOT.sub(" ", text or ""))
    found = [m.group(0) for p in _OTHER_BANKS for m in p.finditer(plain)]
    for m in _GENERIC_BANK.finditer(plain):
        name = m.group(0)
        if bank_mentions(name) or any(x in _tr_lower(name) for x in PUBLIC_BANKS):
            continue
        found.append(name)
    return ["tanınmayan kurum adı: %s" % n for n in dict.fromkeys(found)]


def _known_names(spec: Spec) -> set:
    """Proper names the facts give the model (folded lower case): every word of the fact
    lines and the slot table, the Turkish instrument labels, ``PROPER_OK``."""
    words = set(PROPER_OK)
    for src in [spec.user] + [label_tr(n) for n in spec.series_names]:
        words.update(_tr_lower(w) for w in re.findall(r"[^\W\d_]+", src or ""))
    return words


def unknown_names(text: str, spec: Spec, values_only: bool = False) -> list[str]:
    """PR #3 review: a bank the alias list does not know ("Pictet", "İş Yatırım", "J. P.
    Morgan" spelled another way) is caught as an UNKNOWN PROPER NOUN: a capitalised word that
    is neither a source bank (``bank_mentions``) nor a word of the facts / a public
    institution. Sentence-initial words are names only when they are not ordinary openers.
    ``values_only``: only words before the last value slot count (a bullet)."""
    plain = _NS_CITE.sub(lambda m: " " * len(m.group(0)), text or "")
    if values_only:
        last = [m.start() for m in SLOT.finditer(plain) if spec.slots.get("%s.%s" % m.groups())
                and spec.slots["%s.%s" % m.groups()].kind == "value"]
        if not last:
            return []
        plain = plain[:max(last)]
    plain = SLOT.sub(lambda m: " " * len(m.group(0)), plain)
    for s, e, _ in bank_mentions(plain):
        plain = plain[:s] + " " * (e - s) + plain[e:]
    known = _known_names(spec)
    found = []
    for i, m in enumerate(_CAPWORD.finditer(plain)):
        tok = m.group(0).split("'")[0].split("’")[0].strip(".-–")
        if not tok or not tok[0].isupper():
            continue
        parts = [_tr_lower(x) for x in re.findall(r"[^\W\d_]+", tok)]
        if parts and all(x in known for x in parts):
            continue
        initial = not plain[:m.start()].strip()
        if initial and parts and all(x.startswith(COMMON_STARTS) for x in parts):
            continue
        found.append(tok)
    return ["tanınmayan özel ad (kurum olabilir): %s — yalnız olgulardaki kurumları yaz" % x
            for x in dict.fromkeys(found)]


def claim_problems(claim: str, spec: Spec) -> list[str]:
    """Unknown banks / proper nouns before a value, English negation (Turkish negation is in
    ``check_slots``)."""
    return (unknown_banks(claim) + unknown_names(claim, spec, values_only=True)
            + ["olumsuz ifade: %s" % m.group(0) for m in _NEGATED_EN.finditer(_NS_CITE.sub(" ", claim))])


def reread(out: dict, spec: Spec) -> list[str]:
    """The final re-read before publishing: ``verify_filled`` on every claim (each filled
    number re-read with its slot's own value, unit and sign, nothing glued, no brace), and every
    value slot's reading found in its OWN cited source - the source read the way the slots
    were made (``normalise``d: "one month" -> "1 month", "yüzde 35" -> "%35")."""
    problems = []
    for b in out.get("maddeler") or ():
        for claim in split_claims(str(b)):
            problems += verify_filled(claim, spec.slots)
            cited = _cited_ids(claim, spec)
            for m in SLOT.finditer(claim):
                s = spec.slots.get("%s.%s" % m.groups())
                if s is None:
                    problems.append("bilinmeyen yuva: {%s.%s}" % m.groups())
                    continue
                if s.source not in cited:
                    problems.append("{%s} kaynağına atıf yok" % s.id)
                if s.kind != "value" or s.role == "band":
                    continue
                source = (spec.claims.get(s.source) or spec.facts.get(s.source) or {}).get("text", "")
                pool = {(v, u) for _, _, v, u, _ in _reading(normalise(source))}
                for _, _, v, u, _ in _reading(s.display):
                    if (v, u) not in pool:
                        problems.append("{%s} = %s kaynağında yok (%s)" % (s.id, s.display, s.source))
    return problems


def split_claims(text: str) -> list[str]:
    """The claims of a model bullet: its sentences, a colon and a line break included
    (``split_sentences`` with ``colon=True``): each claim needs its own citation."""
    return split_sentences(text)


def expand_claims(text: str, spec: Spec) -> str:
    """``expand`` for a verified model bullet: claim by claim (see ``split_claims``)."""
    return " ".join(_expand_one(x, spec) for x in split_claims(text))


def expand(text: str, spec: Spec) -> str:
    """[K#] → its reports' [R#]s, sentence by sentence (each sentence keeps its OWN citations
    at its end, D16); repeated [R#]s collapse; in report order."""
    return " ".join(_expand_one(x, spec) for x in split_sentences(text, colon=False))


def _expand_one(text: str, spec: Spec) -> str:
    ids = []
    for kind, n in _CITE.findall(text):
        for rid in (spec.members.get("K" + n, []) if kind == "K" else ["R" + n]):
            if rid not in ids:
                ids.append(rid)
    body = re.sub(r"\s*(\[[KR]\d+\])+", "", text).strip()
    body = body.rstrip(" .:;")           # a claim cut at ":" / ";" ends as a sentence
    ids.sort(key=lambda x: int(x[1:]))
    return "%s %s." % (body, "".join("[%s]" % i for i in ids)) if ids else body + "."


_LABEL_MAP = {en.lower(): tr for en, tr in LABEL_TR}
_LABEL_RE = re.compile(r"(?<![\w/])(%s)(?![\w/])" % "|".join(
    re.escape(en) if en.isupper() else "(?i:%s)" % re.escape(en)
    for en, _ in sorted(LABEL_TR, key=lambda x: -len(x[0]))))


def label_tr(label: str) -> str:
    """An instrument label in Turkish for the template ("policy rate" -> "Politika faizi",
    "ECB deposit rate" -> "ECB mevduat faizi", "Rates, US" -> "Faiz, ABD"); tickers, pairs
    and words it does not know stay as written. One pass, longest phrase first."""
    out = _LABEL_RE.sub(lambda m: _LABEL_MAP[m.group(1).lower()], label or "").strip()
    if out[:1] == "i":
        return "İ" + out[1:]
    return out[:1].upper() + out[1:]


def stance_verb(label: str, stance: str | None) -> str:
    """The template's verb: what the bank(s) expect for ``label`` under ``stance`` (Turkish)."""
    if not stance:
        return NO_STANCE_VERB
    if BOND_LIKE.search(label or "") and stance in STANCE_VERB_BOND:
        return STANCE_VERB_BOND[stance]
    return STANCE_VERB.get(stance, NO_STANCE_VERB)


def report_line(r: dict) -> str:
    """The code's Turkish line for one report: "<instrument>: <bank> <verb>"."""
    inst = _instrument(r)
    stance = STANCE_TR.get((_summary(r).get("stance") or "").strip().lower())
    what = inst[1] if inst else ((r.get("title") or "").strip() or "Rapor")
    return "%s: %s %s" % (label_tr(what), source_name(r.get("source")), stance_verb(what, stance))


def template(spec: Spec) -> dict:
    """The code's own bullets (D16): per instrument one line per stance - the agreeing
    reports under one claim with all their citations, a dissenting stance on its own line -
    then one line per report not in a group, until ``MAX_BULLETS``."""
    bullets, j = [], 0
    for g in spec.groups:
        for stance, banks, _ in stance_lines(g):
            j += 1
            who = banks[0] if len(banks) == 1 else "%s ve %s" % (", ".join(banks[:-1]), banks[-1])
            bullets.append("%s: %s %s [K%d]" % (label_tr(g["label"]), who,
                                                stance_verb(g["label"], stance), j))
    covered = {m for g in spec.groups for m in g["reports"]}
    for i, r in enumerate(spec.reports, 1):
        if "R%d" % i not in covered:
            bullets.append("%s [R%d]" % (report_line(r), i))
    return {"headline": TEMPLATE_HEADLINE, "bullets": [expand(b, spec) for b in bullets[:MAX_BULLETS]]}


def write(spec: Spec, ask) -> dict:
    """``{"headline", "bullets", "status", "attempts", "errors"}`` - bullets carry only [R#]."""
    def fallback(status, calls, errors):
        return dict(template(spec), status=status, attempts=calls, errors=errors)

    if ask is None:
        return fallback(NO_MODEL, 0, [])
    lo, hi = spec.n_bullets
    system = SYSTEM.replace("{count}", "%d" % lo if lo == hi else "%d ile %d" % (lo, hi)) \
        .replace("{{", "{").replace("}}", "}")             # slot ids in the rules: no str.format
    user = "Örnek girdi:\n%s\n\nÖrnek yanıt:\n%s\n\nGirdi:\n%s" % (EXAMPLE[0], EXAMPLE[1], spec.user)
    errors, calls, tr_retry, attempt, prompt, failed = [], 0, False, 0, user, 0
    while attempt < 2:
        attempt += 1
        try:
            reply = ask(system, prompt)
            calls += 1
        except Exception as exc:                                        # noqa: BLE001 — ağ/uç hatası
            # a failed call uses up this attempt only; the second still runs (PRISMA pass 6)
            failed += 1
            errors = ["model çağrısı başarısız: %s" % str(exc)[:200]]
            continue
        try:
            out = _parse(reply)
            errors = validate(out, spec)
        except ValueError as exc:
            out, errors = None, [str(exc)]
        if not errors:
            errors = reread(out, spec)       # last look at the FILLED text before it is published
        if not errors:
            res = {"headline": out["baslik"].strip(),
                   "bullets": [expand_claims(fill_slots(b.strip(), spec.slots), spec)
                               for b in out["maddeler"] if b.strip()],
                   "status": VERIFIED, "attempts": calls, "errors": []}
            if not any(ch in "{}｛｝" for ch in res["headline"] + "".join(res["bullets"])):
                return res
            errors = ["yayımlanacak metinde süslü parantez kaldı"]     # never published
        if errors[0].startswith("metinde hiç Türkçe") and not tr_retry:
            tr_retry, attempt, prompt = True, attempt - 1, user
            continue
        prompt = "%s\n\nÖnceki yanıtın:\n%s\n\n%s" % (
            user, reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False),
            RETRY % "; ".join(errors[:5]))
    return fallback(NO_MODEL if failed == attempt else TEMPLATE, calls, errors)


def summarize(reports: list[dict], ask, label: str = "") -> dict | None:
    """``pick_reports`` output → the summary: ``write``'s dict + ``key`` + ``refs``
    (``[{"id": "R1", "report_id", "source", "title", "date"}]`` for the [R#]s it cites)."""
    spec = build_spec(reports, label)
    if spec is None:
        return None
    res = write(spec, ask)
    return dict(res, key=spec.key, refs=refs_of(spec, res["bullets"]))


def refs_of(spec: Spec, bullets: list[str]) -> list[dict]:
    cited = []
    for b in bullets:
        for kind, n in _CITE.findall(b):
            rid = kind + n
            if kind == "R" and rid not in cited:
                cited.append(rid)
    out = []
    for rid in sorted(cited, key=lambda x: int(x[1:])):
        r = spec.reports[int(rid[1:]) - 1]
        out.append({"id": rid, "report_id": r.get("id"), "source": source_name(r.get("source")),
                    "title": (r.get("title") or _summary(r).get("headline") or "").strip(),
                    "date": r.get("date")})
    return out


# ───────────────────────────────────────────── e-postaya hazır blok
def _esc(s) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_html(summary: dict, href=None) -> str:
    """The mail-ready block: email-safe tables with inline styles (no CSS file, no script,
    no list or superscript markup Outlook mangles), marked ``lang="tr"``. ``href(report_id)
    -> url | None`` links each [R#]; without one the mark stays text."""
    links = {r["id"]: (href(r["report_id"]) if href else None) for r in summary["refs"]}

    def cite(m):
        rid = m.group(1) + m.group(2)
        url = links.get(rid)
        mark = "[%s]" % rid
        if url:
            return ('<a href="%s" style="color:#0b5f57;text-decoration:none;font-size:12px;">%s</a>'
                    % (_esc(url), mark))
        return '<span style="color:#0b5f57;font-size:12px;">%s</span>' % mark

    def rows(lines, size, color, gap):
        return "".join(
            '<tr><td valign="top" width="16" style="font-size:%dpx;line-height:1.55;color:#0b5f57;">'
            '&#8226;</td><td style="font-size:%dpx;line-height:1.55;color:%s;padding-bottom:%dpx;">'
            '%s</td></tr>' % (size, size, color, gap, line) for line in lines)

    items = rows([_CITE.sub(cite, _esc(b)) for b in summary["bullets"]], 14, "#2b2a27", 6)
    sources = rows(["%s %s · %s · %s" % (_CITE.sub(cite, "[%s]" % r["id"]), _esc(r["source"]),
                                          _esc(r["date"] or ""), _esc(r["title"]))
                    for r in summary["refs"]], 12, "#6f6b64", 2)
    badge = ("" if summary["status"] == VERIFIED else
             ' <span style="font-size:11px;color:#6f6b64;">(otomatik şablon)</span>')
    table = '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%">%s</table>'
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%" '
        'style="background:#ffffff;border:1px solid #e6e3dc;border-radius:6px;">'
        '<tr><td lang="tr" style="padding:20px 28px;font-family:Arial,Helvetica,sans-serif;color:#2b2a27;">'
        '<div style="font-size:11px;font-weight:bold;letter-spacing:.18em;color:#0b5f57;">'
        'GÜNÜN ARAŞTIRMA ÖZETİ</div>'
        '<div style="font-size:18px;font-weight:bold;color:#1c1c1a;margin:6px 0 10px;">%s%s</div>'
        '%s'
        '<div style="font-size:11px;color:#6f6b64;margin-top:12px;">Kaynaklar</div>'
        '%s'
        '</td></tr></table>' % (_esc(summary["headline"]), badge, table % items, table % sources))


def to_text(summary: dict, href=None) -> str:
    """The same block as plain text (the mail's text part; also for pasting)."""
    lines = ["GÜNÜN ARAŞTIRMA ÖZETİ", summary["headline"], ""]
    lines += ["- %s" % b for b in summary["bullets"]]
    lines += ["", "Kaynaklar"]
    for r in summary["refs"]:
        url = href(r["report_id"]) if href else None
        lines.append("[%s] %s · %s · %s%s" % (r["id"], r["source"], r["date"] or "", r["title"],
                                              (" - %s" % url) if url else ""))
    return "\n".join(lines)
