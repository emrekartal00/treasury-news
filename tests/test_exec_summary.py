# -*- coding: utf-8 -*-
"""The Turkish executive summary on top of the research mail (owner decision D17).

exec_summary.py (port of treasury-report's research_summary) and its hook in mailer.py.
Every report here is MADE UP (bank keys are the source keys, titles / views / numbers are
only for shape); the model is a fake ``ask`` or a monkeypatched ``ai.chat``; nothing is sent,
no database is touched. Runs with ``python -m pytest``.
"""
import dataclasses
import json
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import exec_summary as xs  # noqa: E402

DAY = "2026-10-22"
LABEL = "22 Ekim 2026"


def _r(rid, source, title, headline, stance, instruments, points):
    return {"id": rid, "title": title, "headline": None, "source": source, "date": DAY,
            "authors": [], "source_path": None, "summary": {
                "headline": headline, "stance": stance, "instruments": list(instruments),
                "key_points": [{"point": p, "evidence": ""} for p in points],
                "one_paragraph": headline}}


REPORTS = {
    "SYN-R1": _r("SYN-R1", "gs", "Example: TRY outlook after the cut",
                 "FX, TRY: report sees USD/TRY drifting higher after the cut", "bullish",
                 ["USD/TRY"], ["3-month USD/TRY forecast raised to 15.20.",
                               "Real rates remain positive after the 150bp cut."]),
    "SYN-R2": _r("SYN-R2", "jpm", "Example: CBRT cuts deeper",
                 "Rates, TRY: report expects the policy rate at 36% by year end", "bearish",
                 ["policy rate"], ["Year-end policy rate forecast at 36%.",
                                   "Another 150bp cut expected in December."]),
    "SYN-R3": _r("SYN-R3", "citi", "Example: Lira carry still works",
                 "FX, TRY: report keeps the lira carry view", "bearish",
                 ["USD/TRY"], ["Carry-adjusted USD/TRY target at 14.80.",
                               "Volatility may stay elevated in the near term."]),
    "SYN-R4": _r("SYN-R4", "ms", "Example: CBRT path",
                 "Rates, TRY: report sees the policy rate at 35% by year end", "bearish",
                 ["policy rate"], ["Year-end policy rate forecast at 35%."]),
    "SYN-R5": _r("SYN-R5", "db", "Example: UST supply",
                 "Rates, US: report sees UST 10Y yield range bound", "neutral",
                 ["UST 10Y"], ["UST 10Y seen in a 4.10%-4.40% range."]),
    "SYN-R6": _r("SYN-R6", "barc", "Example: TRY after the decision",
                 "FX, TRY: report sees USD/TRY stable near term", "neutral",
                 ["USD/TRY"], ["USD/TRY seen near 15.00 over one month."]),
    "SYN-R7": _r("SYN-R7", "gs", "Example: empty extraction", "NO_CONTENT", "n/a", [], []),
    "SYN-R8": _r("SYN-R8", "jpm", "Example: EUR rates",
                 "Rates, EUR: report sees ECB on hold into year end", "neutral",
                 ["ECB deposit rate"], ["Deposit rate stays at 2.00% through Q4."]),
}
MAIL_IDS = ["SYN-R2", "SYN-R1", "SYN-R4", "SYN-R3"]          # the mail's order (curation)
DIGEST_IDS = ["SYN-R1", "SYN-R2", "SYN-R3", "SYN-R4", "SYN-R5", "SYN-R6", "SYN-R7", "SYN-R8"]

GOOD = {"baslik": "Kurumlar kurda ayrışıyor, faizde birleşiyor", "maddeler": [
    "USD/TRY için {K1.c2} kurum ayrışıyor ve Goldman Sachs yükseliş, Citi düşüş, Barclays yatay "
    "seyir bekliyor [R2][R4][R6][K1].",
    "JPMorgan ve Morgan Stanley yıl sonu politika faizini {R1.n1} ve {R3.n1} öngörüyor [K4].",
    "Goldman Sachs {R2.l1} USD/TRY tahminini {R2.n1} olarak yükseltti [R2].",
    "Deutsche Bank {R5.l1} getirisinin {R5.n1} ile {R5.n2} arasında kalacağını öngörüyor [R5]."]}


def mail_reports():
    return [dict(REPORTS[i]) for i in MAIL_IDS]


def digest_reports():
    return [dict(REPORTS[i]) for i in DIGEST_IDS]


def _reports():
    return xs.pick_reports(mail_reports(), digest_reports())


def _ask(answer, calls=None):
    def ask(system, user):
        if calls is not None:
            calls.append(user)
        return json.dumps(answer, ensure_ascii=False) if isinstance(answer, dict) else answer
    return ask


# ───────────────────────────────────────────── picking the reports
def test_pick_keeps_mail_order_then_digest_and_drops_no_content():
    ids = [r["id"] for r in _reports()]
    assert ids[:4] == MAIL_IDS                          # the mail's own order first
    assert ids[4:] == ["SYN-R5", "SYN-R6", "SYN-R8"]    # then the digest, each once
    assert "SYN-R7" not in ids                          # NO_CONTENT summary


def test_pick_stops_at_thirty():
    many = [dict(REPORTS["SYN-R1"], id="X%d" % i) for i in range(40)]
    assert xs.MAX_REPORTS == 30 and len(xs.pick_reports(many, [])) == 30


def test_reports_without_subjects_are_kept():
    """treasury-news summaries have no ``subjects`` field; they must not be dropped."""
    assert "subjects" not in REPORTS["SYN-R1"]["summary"]
    assert len(xs.pick_reports([REPORTS["SYN-R1"]], [])) == 1


def _equity(rid, **kw):
    r = dict(REPORTS["SYN-R5"], id=rid, **{k: v for k, v in kw.items() if k != "headline"})
    if "headline" in kw:
        r["summary"] = dict(r["summary"], headline=kw["headline"])
    return r


def test_digest_only_equity_reports_are_left_out():
    by_headline = _equity("EQ1", headline="Equity, TR: report upgrades Turkish banks")
    by_type = _equity("EQ2", report_types=["Equities", "Europe"])
    by_title = _equity("EQ3", title="Example: Q3 earnings preview")
    cross = _equity("XA1", report_types=["Equities", "Rates", "FX"])        # cross-asset stays
    macro = _equity("MAC", title="Example: price stability and the CBRT")
    ids = [r["id"] for r in xs.pick_reports(mail_reports(), [by_headline, by_type, by_title, cross, macro])]
    assert ids == MAIL_IDS + ["XA1", "MAC"]


def test_a_mailed_report_is_never_second_guessed():
    mailed = _equity("EQ1", headline="Equity, TR: report upgrades Turkish banks")
    assert [r["id"] for r in xs.pick_reports([mailed], [mailed])] == ["EQ1"]


# ───────────────────────────────────────────── facts and slots
def test_one_count_fact_per_stance_stands_for_its_own_reports_only():
    spec = xs.build_spec(_reports(), LABEL)
    usd = spec.groups[0]
    assert usd["label"] == "USD/TRY" and usd["n_banks"] == 3 and usd["diverges"]
    assert spec.members == {"K1": ["R6"], "K2": ["R2"], "K3": ["R4"], "K4": ["R1", "R3"]}


def test_the_prompt_has_no_digit_outside_slots_and_the_date_line():
    spec = xs.build_spec(_reports(), LABEL)
    facts = spec.user.split("Yuvalar:")[0].split("\n", 1)[1]
    assert not any(ch.isdigit() for ch in xs._CITE.sub("", xs.SLOT.sub("", facts)))


def test_report_facts_are_numbered_in_pick_order():
    spec = xs.build_spec(_reports(), LABEL)
    assert spec.user.splitlines()[2].startswith("[R1] JPMorgan")
    assert "[R7]" in spec.user and "[R8]" not in spec.user


# ───────────────────────────────────────────── the model's answer
def test_verified_answer_is_filled_from_the_reports_and_shows_only_report_citations():
    s = xs.summarize(_reports(), _ask(GOOD), LABEL)
    assert s["status"] == xs.VERIFIED and s["attempts"] == 1
    assert s["bullets"][0].endswith("[R2][R4][R6].")    # [K1] expanded, repeats collapsed
    assert s["bullets"][1] == ("JPMorgan ve Morgan Stanley yıl sonu politika faizini %36 ve %35 "
                               "öngörüyor [R1][R3].")      # one claim, several reports
    assert "UST 10Y getirisinin %4,10 ile %4,40 arasında" in s["bullets"][3]
    assert not any("[K" in b or "{" in b for b in s["bullets"])
    assert [r["id"] for r in s["refs"]] == ["R1", "R2", "R3", "R4", "R5", "R6"]
    assert s["refs"][0]["report_id"] == "SYN-R2"


def test_a_number_the_model_writes_itself_falls_back_to_the_template():
    bad = dict(GOOD, maddeler=GOOD["maddeler"][:2] + ["Goldman Sachs tahmini 16,00 [R2]."])
    calls = []
    s = xs.summarize(_reports(), _ask(bad, calls), LABEL)
    assert s["status"] == xs.TEMPLATE and len(calls) == 2
    assert "Önceki yanıtın" in calls[1]                 # errors fed back on the second try
    assert any("16,00" in e and "kendi yazdığın sayı" in e for e in s["errors"])
    assert all(xs._CITE.search(b) for b in s["bullets"])


def test_a_swapped_or_foreign_slot_never_reaches_verified():
    for bad in ("JPMorgan ve Morgan Stanley faizi {R3.n1} ve {R1.n1} görüyor [R1][R3].",
                "Barclays {R2.n1} görüyor [R2][R6].", "Goldman Sachs {R2.n1} görüyor [R4]."):
        s = xs.summarize(_reports(), _ask(dict(GOOD, maddeler=GOOD["maddeler"][:3] + [bad])), LABEL)
        assert s["status"] == xs.TEMPLATE, bad


def _verdict(bullet):
    return xs.validate(dict(GOOD, maddeler=GOOD["maddeler"][:3] + [bullet]), xs.build_spec(_reports(), LABEL))


@pytest.mark.parametrize("bullet", [
    "JPMorgan politika faizini %{R1.n1} öngörüyor [R1].",
    "JPMorgan politika faizini {R1.n1}% öngörüyor [R1].",
    "JPMorgan politika faizini -{R1.n1} öngörüyor [R1].",
    "JPMorgan politika faizini eksi {R1.n1} öngörüyor [R1].",
    "JPMorgan aralıkta {R1.n2} baz puan indirim bekliyor [R1].",
    "JPMorgan aralıkta {R1.n2}'lik baz puan indirim bekliyor [R1].",
    "JPMorgan politika faizini {R1.n1} kat öngörüyor [R1].",
    "JPMorgan politika faizini {R1.n1} milyar öngörüyor [R1].",
    "JPMorgan politika faizini {R1.n1}{R1.n2} öngörüyor [R1].",
    "Deutsche Bank getirinin {R5.n1}, {R5.n2} aralığında kalacağını öngörüyor [R5].",
    "Deutsche Bank getirinin {R5.n1} {R5.n2} aralığında kalacağını öngörüyor [R5].",
    "JPMorgan ve Goldman Sachs {R1.n1}–{R2.n1} görüyor [R1][R2].",          # a range of TWO sources
    "JPMorgan politika faizini {R1.n1}lik öngörüyor [R1].",
    "JPMorgan politika faizini {R1.n1} TL öngörüyor [R1].",
])
def test_no_unit_sign_or_second_slot_next_to_a_slot(bullet):
    assert any("yuva" in e and ("yapışık" in e or "birim" in e or "ikinci yuva" in e)
               for e in _verdict(bullet)), bullet


def test_a_dash_range_of_one_source_is_allowed():
    """PRISMA pass-6 owner decision: "{a}–{b}" only as a range of one source."""
    assert _verdict("Deutsche Bank getirinin {R5.n1}–{R5.n2} aralığında kalacağını öngörüyor [R5].") == []


@pytest.mark.parametrize("bullet,word", [
    ("JPMorgan ikinci çeyrekte indirim bekliyor [R1].", "ikinci"),
    ("JPMorgan son çeyrekte indirim bekliyor [R1].", "çeyrekte"),
    ("JPMorgan yarım puan indirim bekliyor [R1].", "yarım"),
    ("JPMorgan faizin üçte bir oranında düşeceğini öngörüyor [R1].", "üçte"),
    ("JPMorgan faizde III indirimi bekliyor [R1].", "III"),
    ("JPMorgan faizde Ⅲ indirim bekliyor [R1].", "Ⅲ"),
    ("JPMorgan faizi %³⁶ öngörüyor [R1].", "³"),
    ("JPMorgan faizde ½ puan indirim bekliyor [R1].", "½"),
    ("JPMorgan faizde twice indirim bekliyor [R1].", "twice"),
    ("JPMorgan indirimlerin ikincisini aralıkta bekliyor [R1].", "ikincisini"),
    ("JPMorgan indirimin yarısını aralıkta bekliyor [R1].", "yarısını"),
    ("JPMorgan indirimin çeyreğini aralıkta bekliyor [R1].", "çeyreğini"),
    ("JPMorgan faizde üçüncüsü olan indirimi bekliyor [R1].", "üçüncüsü"),
])
def test_numbers_without_digits_are_refused(bullet, word):
    assert any("kendi yazdığın sayı: %s" % word in e for e in _verdict(bullet)), bullet


def test_a_dotted_roman_numeral_is_refused_too():
    assert _verdict("JPMorgan faizde III. indirimi bekliyor [R1].")


@pytest.mark.parametrize("bullet", [
    "JPMorgan USD/TRY için {R1.n1} öngörüyor [R1].",                 # R1.n1 is the policy rate
    "Goldman Sachs politika faizi için {R2.n1} öngörüyor [R2].",      # R2.n1 is USD/TRY
])
def test_a_value_is_bound_to_its_own_series(bullet):
    assert any("serisinin değeri" in e for e in _verdict(bullet)), bullet


def test_the_series_is_per_number_not_per_report():
    """One report, two series: each number keeps the series written before it in the report."""
    two = _r("SYN-T1", "gs", "Example: two calls", "Rates, TRY: report sees cuts and a weaker lira",
             "bearish", ["policy rate", "USD/TRY"],
             ["Policy rate seen at 36% by year end.", "USD/TRY seen at 15.20 by year end."])
    spec = xs.build_spec([two], LABEL)
    by_display = {s.display: s.series for s in spec.slots.values() if s.kind == "value"}
    assert by_display["%36"] == "policy rate" and by_display["15,20"] == "usd/try"
    good = "Goldman Sachs politika faizini {R1.n1}, USD/TRY kurunu {R1.n2} öngörüyor [R1]."
    swapped = "Goldman Sachs politika faizini {R1.n2}, USD/TRY kurunu {R1.n1} öngörüyor [R1]."
    ids = {s.display: s.id for s in spec.slots.values() if s.kind == "value"}
    good = good.replace("{R1.n1}", "{%s}" % ids["%36"]).replace("{R1.n2}", "{%s}" % ids["15,20"])
    swapped = swapped.replace("{R1.n1}", "{%s}" % ids["%36"]).replace("{R1.n2}", "{%s}" % ids["15,20"])
    assert not any("serisinin" in e for e in xs.check_numbers(good, spec))
    assert any("serisinin" in e for e in xs.check_numbers(swapped, spec))


@pytest.mark.parametrize("bullet", [
    "JPMorgan ECB mevduat faizini {R7.l1} olarak öngörüyor [R7].",            # R7.l1 = "4. çeyrek"
    "JPMorgan ECB mevduat faizinin {R7.l1} seviyesinde kalacağını öngörüyor [R7].",
    "Goldman Sachs USD/TRY kurunu {R2.l1} öngörüyor [R2].",
    "Goldman Sachs USD/TRY için {R2.l1} ile {R2.n1} arasında bir seyir bekliyor [R2].",
])
def test_a_label_slot_is_never_a_value(bullet):
    assert any("etiket" in e for e in _verdict(bullet)), bullet


@pytest.mark.parametrize("bullet", [
    "UBS ve JPMorgan politika faizini {R1.n1} öngörüyor [R1].",
    "Nomura Securities politika faizinde düşüş bekliyor [R1].",
    "Örnek Banka A politika faizinde düşüş bekliyor [R1].",
    "Pictet politika faizini {R1.n1} öngörüyor [R1].",
    "İş Yatırım politika faizini {R1.n1} öngörüyor [R1].",
    "Garanti BBVA politika faizini {R1.n1} öngörüyor [R1].",
    "Kuveyt Türk Menkul politika faizini {R1.n1} öngörüyor [R1].",
    "JPMorgan gibi Yapı Kredi de politika faizini {R1.n1} öngörüyor [R1].",
    "Citibank politika faizini {R1.n1} öngörüyor [R1].",                # Citi, but R1 is JPMorgan's
])
def test_unknown_or_uncited_bank_names_are_refused(bullet):
    assert any("kurum" in e or "özel ad" in e for e in _verdict(bullet)), bullet


def test_bank_spellings_of_a_cited_source_pass():
    for bullet in ("Citibank USD/TRY hedefini {R4.n1} görüyor [R4].",
                   "J. P. Morgan politika faizini {R1.n1} öngörüyor [R1]."):
        assert _verdict(bullet) == [], bullet


def test_central_banks_are_not_unknown_sources():
    assert xs.unknown_banks("Merkez Bankası ve Avrupa Merkez Bankası; Bank of England") == []


@pytest.mark.parametrize("bullet", [
    "JPMorgan politika faizinde artış beklemiyor [R1].",
    "JPMorgan politika faizinde artış öngörmüyor [R1].",
    "JPMorgan politika faizinde artış beklemeyecek [R1].",
    "JPMorgan politika faizinde indirim görmüyor [R1].",
    "JPMorgan politika faizinde indirim tahmin etmiyor [R1].",
    "JPMorgan politika faizinde indirim yapılmadığını yazıyor [R1].",
    "JPMorgan politika faizinin düşmeyeceğini yazıyor [R1].",
    "JPMorgan politika faizinde indirimin olası değil olduğunu yazıyor [R1].",
    "JPMorgan politika faizinde indirimi dışlıyor [R1].",
    "JPMorgan does not expect a cut [R1].",
])
def test_negation_beyond_forecast_verbs_is_refused(bullet):
    assert any("olumsuz" in e for e in _verdict(bullet)), bullet


def test_positive_forecast_verbs_pass():
    assert xs.negations("bekliyor, öngörüyor, beklemektedir, beklemeye devam ediyor, görüyor") == []


def test_negation_in_the_headline_and_unknown_bank_in_the_headline_are_refused():
    spec = xs.build_spec(_reports(), LABEL)
    for h in ("Kurumlar indirim beklemiyor", "UBS kurda ayrışıyor"):
        assert any(e.startswith("başlık") for e in xs.validate(dict(GOOD, baslik=h), spec)), h


def test_the_final_re_read_compares_every_filled_number_with_its_report():
    spec = xs.build_spec(_reports(), LABEL)
    assert xs.reread(GOOD, spec) == []
    bad = dataclasses.replace(spec.slots["R1.n1"], display="%37")  # a fill that drifted
    spec.slots["R1.n1"] = bad
    assert any("R1.n1" in e and "kaynağında yok" in e for e in xs.reread(GOOD, spec))
    calls = []
    s = xs.write(spec, _ask(GOOD, calls))
    assert s["status"] == xs.TEMPLATE and "%37" not in " ".join(s["bullets"])


def test_the_re_read_uses_the_normalised_source_so_word_tenors_pass():
    r = _r("SYN-W1", "barc", "Example: word tenor", "FX, TRY: report sees USD/TRY stable", "neutral",
           ["USD/TRY"], ["USD/TRY seen near 15.00 over one month."])
    spec = xs.build_spec([r], LABEL)
    ids = {s.display: s.id for s in spec.slots.values()}
    out = {"baslik": "Kurda yatay seyir", "maddeler": [
        "Barclays USD/TRY kurunun {%s} içinde {%s} civarında kalacağını öngörüyor [R1]." % (
            ids["1 aylık"], ids["15,00"])]}
    assert xs.reread(out, spec) == []


def test_a_brace_is_never_published():
    spec = xs.build_spec(_reports(), LABEL)
    for b in ("JPMorgan politika faizini {{R1.n1}} öngörüyor [R1].", "JPMorgan {hedef} öngörüyor [R1].",
              "JPMorgan politika faizini {R1.n1 öngörüyor [R1]."):
        out = dict(GOOD, maddeler=GOOD["maddeler"][:3] + [b])
        assert any("parantez" in e for e in xs.validate(out, spec) + xs.reread(out, spec)), b
        s = xs.summarize(_reports(), _ask(out), LABEL)
        assert s["status"] != xs.VERIFIED and not any(ch in "{}" for ch in "".join(s["bullets"]))


def test_a_direction_word_far_from_its_slot_still_counts():
    # R1.n3 = 150 baz puan, a cut; "artış" four words later must still be seen
    assert any("yönü" in e for e in _verdict(
        "JPMorgan aralıkta {R1.n3} tutarında ve beklenenden daha büyük bir artış bekliyor [R1]."))


def test_the_slot_table_shows_bank_and_topic():
    spec = xs.build_spec(_reports(), LABEL)
    assert "{R1.n1} = %36 (JPMorgan, policy rate)" in spec.user


def test_ordinary_words_are_not_numbers():
    assert xs.raw_numbers("Kurumlar yarın açıklanacak veriyi izliyor; altında kalıyor, onun yanında [R1].") == []


@pytest.mark.parametrize("bullet", [
    "JPMorgan indirim bekliyor: Goldman Sachs yükseliş bekliyor [R1][R2].",
    "JPMorgan indirim bekliyor\nGoldman Sachs yükseliş bekliyor [R1][R2].",
])
def test_a_colon_or_line_break_starts_a_claim_that_needs_its_own_citation(bullet):
    assert any("atıfsız cümle" in e for e in _verdict(bullet)), bullet


def test_cited_claims_on_both_sides_of_a_colon_pass_and_keep_their_own_citations():
    bullet = "JPMorgan indirim bekliyor [R1]: Goldman Sachs yükseliş bekliyor [R2]."
    assert _verdict(bullet) == []
    s = xs.summarize(_reports(), _ask(dict(GOOD, maddeler=GOOD["maddeler"][:3] + [bullet])), LABEL)
    assert s["bullets"][3] == "JPMorgan indirim bekliyor [R1]. Goldman Sachs yükseliş bekliyor [R2]."


@pytest.mark.parametrize("headline", ["Goldman Sachs kurda yükseliş bekliyor", "Faizde rekor indirim",
                                      "Faizde ikinci indirim", "Bu fırsat kaçmaz", "İndirim bekleniyor"])
def test_the_headline_gets_the_same_word_number_and_bank_checks(headline):
    spec = xs.build_spec(_reports(), LABEL)
    assert any(e.startswith("başlık") for e in xs.validate(dict(GOOD, baslik=headline), spec)), headline


def test_every_sentence_needs_a_citation_and_the_headline_no_number():
    spec = xs.build_spec(_reports(), LABEL)
    out = dict(GOOD, baslik="Faizde 2 kurum", maddeler=GOOD["maddeler"][:2] + [
        "Goldman Sachs USD/TRY tahminini yükseltti. Bu kurlarda oynaklık yüksek [R2]."])
    errors = xs.validate(out, spec)
    assert any("başlıkta sayı" in e for e in errors)
    assert any("atıfsız cümle" in e for e in errors)


def test_answer_without_turkish_is_asked_again_clean():
    calls = []
    answers = iter([json.dumps({"baslik": "Banks differ", "maddeler": ["x [R1]."]}),
                    json.dumps(GOOD, ensure_ascii=False)])
    s = xs.summarize(_reports(), lambda sy, u: (calls.append(u), next(answers))[1], LABEL)
    assert s["status"] == xs.VERIFIED and len(calls) == 2
    assert "Önceki yanıtın" not in calls[1]


def test_think_blocks_fences_and_trailing_text_are_parsed():
    reply = ("<think>{\"x\": 1}</think>```json\n" + json.dumps(GOOD, ensure_ascii=False)
             + "\n```\nnot part of it {")
    assert xs._parse(reply)["baslik"] == GOOD["baslik"]


def test_template_groups_agreeing_reports_and_gives_dissent_its_own_line():
    s = xs.summarize(_reports(), None, LABEL)
    assert s["status"] == xs.NO_MODEL
    assert s["bullets"] == [
        "USD/TRY: Barclays yatay seyir bekliyor [R6].",
        "USD/TRY: Goldman Sachs yükseliş bekliyor [R2].",
        "USD/TRY: Citi düşüş bekliyor [R4].",
        "Politika faizi: JPMorgan ve Morgan Stanley düşüş bekliyor [R1][R3].",   # one line, two reports
        "UST 10Y: Deutsche Bank yatay seyir bekliyor [R5]."]
    assert not any("görüşte" in b or "policy rate" in b for b in s["bullets"])


def test_template_labels_are_turkish_and_tickers_stay():
    assert xs.label_tr("policy rate") == "Politika faizi"
    assert xs.label_tr("ECB deposit rate") == "ECB mevduat faizi"
    assert xs.label_tr("Rates, US") == "Faiz, ABD"
    assert xs.label_tr("inflation, Turkey") == "Enflasyon, Türkiye"
    assert [xs.label_tr(x) for x in ("USD/TRY", "UST 10Y", "EUR/USD")] == ["USD/TRY", "UST 10Y", "EUR/USD"]


def test_template_verb_follows_the_stance_without_a_direction_on_bonds():
    assert xs.stance_verb("politika faizi", "olumsuz") == "düşüş bekliyor"
    assert xs.stance_verb("USD/TRY", "olumlu") == "yükseliş bekliyor"
    assert xs.stance_verb("UST 10Y", "olumlu") == "olumlu bakıyor"     # price view, label may be a yield
    assert xs.stance_verb("Bund", "nötr") == "yatay seyir bekliyor"
    assert xs.stance_verb("USD/TRY", None) == "yön belirtmiyor"


def test_model_error_is_the_template_not_a_crash():
    def boom(system, user):
        raise ConnectionError("no endpoint")
    assert xs.summarize(_reports(), boom, LABEL)["status"] == xs.NO_MODEL


def test_empty_input_is_none():
    assert xs.summarize([], None) is None


# ───────────────────────────────────────────── the mail block
def test_mail_block_links_every_citation_and_is_outlook_safe():
    s = xs.summarize(_reports(), _ask(GOOD), LABEL)
    html = xs.to_html(s, lambda rid: "https://example.invalid/r/%s" % rid)
    assert html.count('href="https://example.invalid/r/SYN-R2"') >= 2   # bullet + source line
    assert "<script" not in html and "class=" not in html and "<ul" not in html
    assert 'lang="tr"' in html and "otomatik şablon" not in html
    assert xs.to_text(s).startswith("GÜNÜN ARAŞTIRMA ÖZETİ")


# ───────────────────────────────────────────── mailer hook
mailer = pytest.importorskip("mailer")      # needs oracledb installed (it is at the office)


def _overview():
    return {"date": DAY, "counts": {"total": len(MAIL_IDS)}}


def test_switches_default_off():
    assert mailer.EXEC_SUMMARY is False and mailer.EXEC_SUMMARY_DRY_RUN is False
    assert mailer.EXEC_SUMMARY_TIMEOUT > 0


def test_summary_sits_on_top_and_cards_carry_their_r_number():
    s = mailer.build_summary(mail_reports(), digest_reports(), DAY, "https://example.invalid",
                             ask=_ask(GOOD))
    html, text = mailer.render(_overview(), mail_reports(), "https://example.invalid", s)
    assert html.index("GÜNÜN ARAŞTIRMA ÖZETİ") < html.index("Example: CBRT cuts deeper")
    assert re.findall(r'>(\[R\d+\] [A-Z]+)</span>', html) == ["[R1] JPM", "[R2] GS", "[R3] MS", "[R4] CITI"]
    assert html.count('href="https://example.invalid/reports/SYN-R2"') >= 3   # card x2 + summary
    assert 'href="https://example.invalid/reports/SYN-R6"' in html             # digest-only report
    assert "GÜNÜN ARAŞTIRMA ÖZETİ" in text


def test_without_summary_the_mail_is_unchanged():
    html, _ = mailer.render(_overview(), mail_reports(), "", None)
    assert "GÜNÜN ARAŞTIRMA ÖZETİ" not in html and "[R1]" not in html


def test_time_limit_mails_without_the_summary():
    def slow(system, user):
        time.sleep(3)
        return json.dumps(GOOD, ensure_ascii=False)
    t0 = time.monotonic()
    assert mailer.build_summary(mail_reports(), digest_reports(), DAY, ask=slow, timeout=0.3) is None
    assert time.monotonic() - t0 < 2


def test_an_error_in_the_builder_mails_without_the_summary(monkeypatch):
    monkeypatch.setattr(xs, "summarize", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    assert mailer.build_summary(mail_reports(), digest_reports(), DAY, ask=_ask(GOOD)) is None


def test_the_model_is_called_through_ai_chat_with_the_time_left(monkeypatch):
    seen = []

    def chat(system, user, **kw):
        seen.append(kw)
        return json.dumps(GOOD, ensure_ascii=False), "stop"
    monkeypatch.setattr(mailer.ai, "chat", chat)
    s = mailer.build_summary(mail_reports(), digest_reports(), DAY, timeout=60)
    assert s["status"] == xs.VERIFIED and len(seen) == 1
    assert 0 < seen[0]["timeout"] <= 60 and seen[0]["max_tokens"] == 2048


def test_a_model_timeout_past_the_limit_drops_the_summary(monkeypatch):
    def chat(system, user, **kw):
        time.sleep(kw["timeout"] + 0.05)
        raise mailer.ai.LLMError("request failed: read timed out")
    monkeypatch.setattr(mailer.ai, "chat", chat)
    assert mailer.build_summary(mail_reports(), digest_reports(), DAY, timeout=1.5) is None


class _FakeCon:
    def cursor(self):
        return object()

    def close(self):
        pass

    def commit(self):
        pytest.fail("a dry run must not write EMAIL_LOG")


def test_dry_run_writes_the_html_and_sends_nothing(tmp_path, monkeypatch):
    out = tmp_path / "state" / "exec_summary_dry_run.html"
    monkeypatch.setattr(mailer, "EXEC_SUMMARY_DRY_RUN", True)
    monkeypatch.setattr(mailer, "DRY_RUN_FILE", out)
    monkeypatch.setattr(mailer.db_conn, "connect", lambda: _FakeCon())
    monkeypatch.setattr(mailer, "db_today", lambda cur: DAY)
    monkeypatch.setattr(mailer, "already_sent", lambda cur, d: True)        # re-runnable
    monkeypatch.setattr(mailer, "load_digest", lambda cur, d: (_overview(), DIGEST_IDS))
    monkeypatch.setattr(mailer, "load_report", lambda cur, rid: dict(REPORTS[rid]))
    monkeypatch.setattr(mailer.curate, "select", lambda reports: [r for r in mail_reports()])
    monkeypatch.setattr(mailer.ai, "chat", lambda s, u, **k: (json.dumps(GOOD, ensure_ascii=False), "stop"))
    monkeypatch.setattr(mailer, "send", lambda *a, **k: pytest.fail("dry run must not send"))
    monkeypatch.setattr(mailer, "log_email", lambda *a, **k: pytest.fail("dry run must not log"))
    monkeypatch.setattr(sys, "argv", ["mailer.py"])
    mailer.main()
    html = out.read_text(encoding="utf-8")
    assert "Kurumlar kurda ayrışıyor, faizde birleşiyor" in html and "%36 ve %35" in html


def test_load_report_carries_the_banks_report_types():
    class Cur:
        def execute(self, sql, params):
            assert "r.report_types" in sql

        def fetchone(self):
            return ("T", None, b'[]', None, None, b'{"headline": "h"}', "citi", DAY,
                    b'["Equities", "Europe"]')
    r = mailer.load_report(Cur(), "X1")
    assert r["report_types"] == ["Equities", "Europe"] and xs.is_equity(r)


def _office_day(monkeypatch, sent=None, logged=None):
    class Con(_FakeCon):
        def commit(self):
            pass
    monkeypatch.setattr(mailer.db_conn, "connect", lambda: Con())
    monkeypatch.setattr(mailer, "db_today", lambda cur: DAY)
    monkeypatch.setattr(mailer, "already_sent", lambda cur, d: False)
    monkeypatch.setattr(mailer, "load_digest", lambda cur, d: (_overview(), DIGEST_IDS))
    monkeypatch.setattr(mailer, "load_report", lambda cur, rid: dict(REPORTS[rid]))
    monkeypatch.setattr(mailer.curate, "select", lambda reports: [r for r in mail_reports()])
    monkeypatch.setattr(mailer.ai, "chat", lambda s, u, **k: (json.dumps(GOOD, ensure_ascii=False), "stop"))
    monkeypatch.setattr(mailer, "send", lambda subject, html, preview=False: sent.append(html) or "outlook")
    monkeypatch.setattr(mailer, "log_email", lambda cur, row, ids: logged.append((row["status"], ids)))
    monkeypatch.setattr(sys, "argv", ["mailer.py"])


def test_dry_run_never_stops_the_real_mail_inside_the_pipeline(tmp_path, monkeypatch):
    sent, logged = [], []
    out = tmp_path / "dry.html"
    _office_day(monkeypatch, sent, logged)
    monkeypatch.setattr(mailer, "EXEC_SUMMARY_DRY_RUN", True)
    monkeypatch.setattr(mailer, "DRY_RUN_FILE", out)
    monkeypatch.setenv(mailer.PIPELINE_ENV, "1")
    mailer.main()
    assert len(sent) == 1 and logged == [("SENT", MAIL_IDS)] and not out.exists()
    assert "GÜNÜN ARAŞTIRMA ÖZETİ" not in sent[0]           # EXEC_SUMMARY itself is still off


def test_the_pipeline_marks_the_mail_stage(monkeypatch):
    import pipeline
    seen = []
    monkeypatch.setattr(pipeline.subprocess, "run",
                        lambda cmd, env=None, **k: seen.append((cmd, env)) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "--only", "mail"])
    pipeline.main()
    assert seen[0][0][-1] == "mailer.py" and seen[0][1][mailer.PIPELINE_ENV] == "1"


def test_a_missing_exec_summary_module_mails_without_the_summary(monkeypatch):
    sent, logged = [], []
    _office_day(monkeypatch, sent, logged)
    monkeypatch.setattr(mailer, "EXEC_SUMMARY", True)
    monkeypatch.setitem(sys.modules, "exec_summary", None)       # "import exec_summary" fails
    mailer.main()
    assert len(sent) == 1 and "GÜNÜN ARAŞTIRMA ÖZETİ" not in sent[0] and logged[0][0] == "SENT"


def test_the_summary_goes_into_the_real_mail_when_switched_on(monkeypatch):
    sent, logged = [], []
    _office_day(monkeypatch, sent, logged)
    monkeypatch.setattr(mailer, "EXEC_SUMMARY", True)
    mailer.main()
    assert "GÜNÜN ARAŞTIRMA ÖZETİ" in sent[0] and logged == [("SENT", MAIL_IDS)]
