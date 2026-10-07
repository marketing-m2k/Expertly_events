"""Writes baseline/<slug>.json from the event lists that were READ BY HAND off the
rendered listing pages captured on 2026-10-07 (baseline/raw/<slug>/page_*.txt).

The titles/dates below are the manual verification (a person -- here: the
reviewing model -- reading the rendered page text). URLs are then looked up
mechanically from the page's own link list by title so the baseline can be
matched by URL as well as by title. Anything whose URL cannot be found is left
blank and matched by title+date instead.

A baseline event has `valid`: false for things that are on the page but are not
scheduled events (on-demand products, standing series, discussion groups). They
are never counted as "missed" -- but are listed so nothing is silently ignored.

    python tools/make_baseline.py
"""

import json
import os
import re
import sys
from urllib.parse import urlsplit

sys.path.insert(0, ".")
from pipeline.common import norm_text, similarity  # noqa: E402

SNAPSHOT = "2026-10-07"


def links_for(slug: str) -> list[tuple[str, str]]:
    out = []
    d = os.path.join("baseline", "raw", slug)
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".links.json"):
            out += [tuple(x) for x in json.load(open(os.path.join(d, fn), encoding="utf-8"))]
    return out


def html_url_near(slug: str, title: str, used: set) -> str:
    """Fallback: the first link in the smallest block that contains the title text (card layouts whose
    link text is just 'Find out more' / 'Register')."""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    d = os.path.join("baseline", "raw", slug)
    want = norm_text(title)
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".html"):
            continue
        soup = BeautifulSoup(open(os.path.join(d, fn), encoding="utf-8").read(), "html.parser")
        for node in soup.find_all(string=True):
            if norm_text(str(node)) == want:
                el = node.parent
                for _ in range(6):
                    a = [x for x in el.find_all("a", href=True) if not x["href"].startswith(("#", "javascript", "mailto"))]
                    a = [x for x in a if urljoin("https://x/", x["href"]) not in used]
                    if a:
                        return a[0]["href"] if a[0]["href"].startswith("http") else urljoin(
                            re.search(r"# url: (\S+)", open(os.path.join(d, fn.replace(".html", ".txt")), encoding="utf-8").read()).group(1), a[0]["href"])
                    el = el.parent
                    if el is None:
                        break
    return ""


def find_url(slug: str, title: str, used: set) -> str:
    best, best_s = "", 0.0
    for text, href in links_for(slug):
        if href in used or not text or "#" == href[-1:]:
            continue
        s = similarity(title, text)
        if len(norm_text(title)) > 10 and norm_text(title) in norm_text(text):
            s = max(s, 0.95)
        if norm_text(text).startswith(norm_text(title)[:25]) and len(norm_text(title)) > 8:
            s = max(s, 0.9)
        if s > best_s:
            best, best_s = href, s
    if best_s >= 0.75:
        return best
    return html_url_near(slug, title, used)


def ev(title, date, valid=True, kind="scheduled", past=False, url=None, note=""):
    return {"title": title, "date_text": date, "valid": valid, "kind": kind, "past_on_listing": past, "url": url, "note": note}


ORGS = {
    "ciot": dict(org="Chartered Institute of Taxation (CIOT)", country="UK", url="https://www.tax.org.uk/events",
                 stated_total=None, archive_present=False, expected_state="discovery_complete",
                 notes="Static page; each event is a title + a weekday date line only (no month-day pattern the v1 listing regex accepts: 'Tuesday 6 October 2026').",
                 events=[ev("CIOT at Conservative Party Conference", "Tuesday 6 October 2026"),
                         ev("Tax Aid: OMB and Private Client Update Virtual Conference", "Monday 19 October 2026"),
                         ev("Indirect Taxes Conference", "Thursday 12 November 2026"),
                         ev("CTA Admission Ceremony", "Wednesday 24 February 2027")]),
    "fta": dict(org="Federation of Tax Administrators (FTA)", country="USA", url="https://taxadmin.org/events/",
                stated_total=None, archive_present=True, expected_state="discovery_complete",
                notes="Configured URL redirects to /calendar/ (Tribe Events list). 3 upcoming events; 'Previous Events' links to a past archive and must not be crawled.",
                events=[ev("FTA Tobacco Tax Section Basic Training Class", "October 5 - October 9 (2026)", note="listing gives no year; started 5 Oct 2026"),
                        ev("2026 Western States Association of Tax Administrators (WSATA) Annual Meeting", "November 1 - November 4"),
                        ev("2026 Compliance Workshop", "November 30 - December 2")]),
    "icsi": dict(org="ICSI", country="India", url="https://www.icsi.edu/events/",
                 stated_total=None, archive_present=False, expected_state="discovery_failed",
                 notes="SOFT 404: the page renders the site's '404 - route doesn't exist' view (HTTP 200). The only event-like text is a homepage 'Latest Update' ticker. "
                       "The correct outcome is discovery_failed (source URL broken) -- NOT 'organisation has no events'.",
                 events=[]),
    "arita": dict(org="Australian Restructuring Insolvency & Turnaround Association (ARITA)", country="AUS", url="https://arita.com.au/events",
                  stated_total=13, archive_present=False, expected_state="discovery_complete",
                  notes="Numbered pagination (1,2); 9 events on page 1, 4 on page 2. Page 2 is only reachable by clicking '2'.",
                  events=[ev("Vic/Tas Division Conference and Dinner 2026", "14th Oct 2026"),
                          ev("Qld Young Professionals Seminar: Litigation Funding", "22nd Oct 2026"),
                          ev("NSW/ACT Division Conference and Dinner 2026", "29th Oct 2026"),
                          ev("NSW Young Professionals Seminar: Creditor activism - The good, the bad and the ugly", "29th Oct 2026"),
                          ev("WA Forum: Views from the Bench", "29th Oct 2026"),
                          ev("Expert Series: Restructuring & Turnaround", "5th Nov 2026"),
                          ev("Expert Series: Bankruptcy", "17th Nov 2026"),
                          ev("Hunter Forum & EOY drinks: 2026 in review", "19th Nov 2026"),
                          ev("NSW Young Professionals End-of-year Social - Cruise", "19th Nov 2026"),
                          ev("Vic/Tas Young Professionals EOY lawn bowls", "20th Nov 2026"),
                          ev("Expert Series: Insolvency", "26th Nov 2026"),
                          ev("WA Young Professionals End-of-year Social", "26th Nov 2026"),
                          ev("Expert Series: Small Practice", "8th Dec 2026")]),
    "cfa-india": dict(org="CFA Society India", country="India", url="https://cfasocietyindia.org/upcomingevents/",
                      stated_total=3, archive_present=True, archive_items_approx=440, expected_state="discovery_complete",
                      notes="3 upcoming events; below them a 'Past Event' archive with 37 numbered pages (about 440 past events) on the same page. "
                            "The word 'Next' also appears inside a real event title ('The Next Era of Wealth Management...'), which a text-based 'next' click hits.",
                      events=[ev("AI Is Not Just a Brain: Building an Operating Architecture for Reliable Research | Mumbai", "October 10, 2026"),
                              ev("Communication for Impact: Writing, Storytelling & Digital Content", "October 17, 2026"),
                              ev("10th Value Investing Pioneers Summit | Delhi", "October 24, 2026")]),
    "picpa-dubai": dict(org="PICPA Dubai", country="UAE", url="https://picpadubai.org/events/list/",
                        stated_total=None, archive_present=True, expected_state="discovery_complete",
                        notes="The Events Calendar (Tribe) list view with JSON-LD Event data; 7 upcoming events; 'Previous Events' archive link.",
                        events=[ev("VAT Compliance, Audits & Risk Management in the UAE", "October 24, 2026"),
                                ev("Tax Accounting under IAS 12 & Deferred Tax Complexities", "October 24, 2026"),
                                ev("IFRS 10 & 3 : Consolidation, Business Combinations & Group Reporting", "October 24, 2026"),
                                ev("Financial Statement Analysis & Advanced Valuation Techniques", "October 24, 2026"),
                                ev("Data Analytics & Visualization through Power BI", "November 28, 2026"),
                                ev("Budgeting & Forecasting | Treasury & Cash Management Best Practices", "December 12, 2026"),
                                ev("PICPAlaro Sportsfest", "February 27, 2027", note="sports/social event; in scope question for classification, not discovery")]),
    "taxation-society-uae": dict(org="Taxation Society UAE", country="UAE", url="https://taxationsociety.org/events-conferences/",
                                 stated_total=1, archive_present=True, archive_items_approx=120, expected_state="discovery_complete",
                                 notes="1 upcoming event; about 120 past events listed under 'Past Events' on the same page (must not contaminate current discovery).",
                                 events=[ev("CFO Conclave 2026", "24-Oct-2026")]),
    "tei": dict(org="Tax Executives Institute (TEI)", country="USA", url="https://www.tei.org/events-education/events",
                stated_total=34, archive_present=False, expected_state="discovery_complete",
                notes="34 rows, each: title, start date, end date, venue, 'Read More'. Two 'New England Chapter: Ethics Training' rows on the same date are separate listings. Chapter 'Season Pass' rows span months.",
                events=[ev(t, d) for t, d in [
                    ("2026 Tax Leadership Academy", "March 14, 2026 - October 17, 2026"),
                    ("Chicago Chapter: Season Pass", "August 2, 2026 - June 30, 2027"),
                    ("Seattle Chapter: 2026-2027 Season Pass", "August 14, 2026 - December 31, 2026"),
                    ("Denver Chapter: 2026-2027 Season Pass", "September 17, 2026 - June 30, 2027"),
                    ("Latin America Chapter: Tax Day in São Paulo - TEI and ANEFAC", "October 13, 2026"),
                    ("Seattle Chapter: Property Tax - Litigation Issues, Data Centers, and Intangible Asset Considerations (Virtual)", "October 15, 2026"),
                    ("New England Chapter: Ethics Training", "October 16, 2026"),
                    ("New England Chapter: Ethics Training", "October 16, 2026"),
                    ("2026 Annual Conference", "October 18, 2026 - October 21, 2026"),
                    ("Denver Chapter: State Tax Hot Topics", "October 20, 2026"),
                    ("Wisconsin Chapter: Oct Meeting - Indirect Tax / WI DOR", "October 20, 2026"),
                    ("Carolinas Chapter: TEI Carolinas Fall Tax Update Seminar - Raleigh", "October 26, 2026"),
                    ("2026 Federal Tax Course - Level 1 (Simulive Edition)", "October 26, 2026 - December 9, 2026"),
                    ("Portland Chapter: International Tax Day", "October 27, 2026"),
                    ("Carolinas Chapter: TEI Carolinas Fall Tax Update Seminar - Charlotte", "October 27, 2026"),
                    ("Region 1: Risk Management for Large Enterprises: Audits and AI, Director's Liability, and Transfer Pricing", "October 27, 2026"),
                    ("Virginia Chapter: TEI VA Chapter October 2026 Meeting", "October 27, 2026"),
                    ("Nevada Chapter: Ethics plus ASC 740 Highlights and OBBBA SALT Conformity Update", "October 28, 2026"),
                    ("Nebraska Chapter: Top Golf Fall Social Event", "October 28, 2026"),
                    ("Detroit Chapter: November ASC 740 Meeting", "November 2, 2026"),
                    ("Indiana Chapter: TopGolf & Deloitte Tax Controversy/M&A (In-Person)", "November 5, 2026"),
                    ("2026 TechTAXcelerate: Building Smarter Tax Functions Through Technology, Data & AI", "November 9, 2026 - November 13, 2026"),
                    ("Rochester Chapter State Tax Update", "November 10, 2026"),
                    ("Connecticut Valley Chapter: Tax Education Day 2026", "November 12, 2026"),
                    ("TEI NY Chapter Tax Symposium", "November 17, 2026"),
                    ("2026 US International Tax Course (Simulive Edition)", "January 21, 2027 - March 10, 2027"),
                    ("2027 Midyear Conference", "March 21, 2027 - March 24, 2027"),
                    ("Region 10: 47th Annual Conference", "April 28, 2027 - April 30, 2027"),
                    ("2027 Tax Technology Seminar", "May 2, 2027 - May 5, 2027"),
                    ("Region 8: 2027 Annual Conference", "June 9, 2027 - June 13, 2027"),
                    ("2027 Annual Conference", "October 24, 2027 - October 27, 2027"),
                    ("2028 Midyear Conference", "March 19, 2028 - March 22, 2028"),
                    ("2028 Annual Conference", "October 22, 2028 - October 25, 2028"),
                    ("2029 Midyear Conference", "March 18, 2029 - March 21, 2029")]]),
    "icaew": dict(org="ICAEW", country="UK", url="https://www.icaew.com/learning-and-development/events",
                  stated_total=None, archive_present=False, expected_state="discovery_complete",
                  notes="Configured URL is the 'ICAEW flagship events' page: 12 dated upcoming events (two already past by a few days but still listed) and 7 on-demand recordings. "
                        "No pagination: v1's 'next' text click found nothing, harmlessly.",
                  events=[ev("Farming and Rural Business Conference", "24 September 2026", past=True),
                          ev("Solicitors Conference", "6 October 2026", past=True),
                          ev("ICAEW Annual Conference", "16 October 2026"),
                          ev("Global Ethics Day", "23 October 2026"),
                          ev("Internal Audit Conference", "5 November 2026"),
                          ev("ICAEW ASEAN Young Leaders Forum", "6-7 November 2026"),
                          ev("ICAEW Sustainability Summit", "9-10 November 2026"),
                          ev("Digital Assets Conference", "13 November 2026"),
                          ev("Forensic & Expert Witness Conference", "18 November 2026"),
                          ev("AI & Analytics Conference", "26 November 2026"),
                          ev("Public Sector Conference", "4 December 2026"),
                          ev("Charity Conference", "21 & 22 January 2027")] +
                         [ev(t, "", valid=False, kind="on_demand", note="recording; no event date") for t in
                          ["Changes to UK GAAP Conference 2026", "CPD Conference 2026", "Healthcare Conference 2026",
                           "Forensic & Expert Witness Conference 2025", "Public Sector Conference 2025",
                           "Restructuring & Insolvency Conference 2026", "Valuation Conference 2026"]]),
    "isca-sg": dict(org="Institute of Singapore Chartered Accountants (ISCA)", country="SG", url="https://www.isca.org.sg/member-support/events",
                    stated_total=None, archive_present=False, expected_state="discovery_complete",
                    notes="Landing page: 3 upcoming courses/talks (with session dates) + 5 flagship events (3 already past) + 7 undated 'signature event' series. "
                          "The 'View Events' link leads to the full registration listing, which is outside the configured URL.",
                    events=[ev("SCAQFRID2026: SCAQ Fridays", "9 Oct 2026"),
                            ev("BT2604: Deloitte Breakfast Talk", "22 Oct 2026"),
                            ev("Techtalk 2608: Caseware TechTalk", "28 Oct 2026"),
                            ev("ISCA Dinner 2026", "3 July 2026", past=True),
                            ev("AccountanCity 2026", "17 & 18 July 2026", past=True),
                            ev("Tech & AI Fair", "28 August 2026", past=True),
                            ev("ISCA Run 2026", "17 Oct 2026"),
                            ev("ISCA Conference 2026", "11 Nov 2026")] +
                           [ev(t, "", valid=False, kind="series", note="standing programme, no date") for t in
                            ["Decision Maker's Luncheon", "Board Nexus", "Leaders at Crossings", "TalkTank", "Educators Connect",
                             "Practitioners Connect", "Public Sector Connect", "Women Connect"]]),
    "taxinstitute": dict(org="The Tax Institute", country="AUS", url="https://www.taxinstitute.com.au/events",
                         stated_total=None, archive_present=False, expected_state="discovery_complete",
                         notes="Sectioned page (Conferences & Forums / Conventions & Retreats / Masterclasses & Intensives / Local events). "
                               "'SA Tax Forum' shows '4 - 5 March 2026' on the card (before today): the event page decides. "
                               "'Next Gen Tax Discussion Group' contains the word 'Next' -- a text-based 'next' click navigates away into it.",
                         events=[ev("National GST Conference", "15 - 16 October 2026"),
                                 ev("National Superannuation Conference", "22 - 23 October 2026"),
                                 ev("Death & Taxes Conference", "28 - 29 October 2026"),
                                 ev("National Resources Tax Conference", "12-13 November 2026"),
                                 ev("SA Tax Forum", "4 - 5 March 2026", note="date as printed on the card; may be a site typo"),
                                 ev("WA Tax Forum", "11 - 12 March 2027"),
                                 ev("Financial Services Taxation Conference", "11 - 12 March 2027"),
                                 ev("Tasmanian Convention", "15-16 October 2026"),
                                 ev("Noosa Tax Convention", "18 - 20 November 2026"),
                                 ev("Private Business Tax Retreat", "25 - 26 February 2027"),
                                 ev("Regional Masterclass", "8 - 9 October 2026"),
                                 ev("Property Intensive", "5 - 6 November 2026"),
                                 ev("Superannuation Intensive", "16 - 17 March 2027"),
                                 ev("Estate Planning Intensive", "22 - 23 March 2027")] +
                                [ev(t, "", valid=False, kind="on_demand", note="OnDemand product; no event date") for t in
                                 ["Infrastructure & Investments Tax Conference", "NSW Tax Forum", "QLD Tax Forum", "Agribusiness Intensive",
                                  "Tax Disputes Masterclass", "Not-for-Profit Tax Intensive", "Trusts Intensive"]] +
                                [ev(t, "", valid=False, kind="series", note="local discussion group / club, no date") for t in
                                 ["Local Tax Club - Sydney", "Sydney Tax Discussion Group", "Women in Tax - NSW", "Local Tax Club - Melbourne",
                                  "Next Gen Tax Discussion Group", "Local Tax Club - Geelong", "Melbourne Tax Discussion Group",
                                  "Local Tax Club - Toowoomba", "Local Tax Club - Gold Coast", "Augustinian Tax Discussion Group",
                                  "Hobart Tax Discussion Group", "Women in Tax Launceston", "Local Tax Club - Perth", "Women in Tax - Perth",
                                  "Local Tax Club - Adelaide", "Women in Tax - Adelaide", "Canberra Tax Discussion Group"]] +
                                [ev("Tax in Practice Part 2: Bendel - After the dust settles", "", valid=True, note="local event listed with no date on the card; date only on its page"),
                                 ev("Tax in Practice - Supercharge Strategies - Division 296", "", valid=True, note="local event listed with no date on the card; date only on its page")]),
}


def main():
    os.makedirs("baseline", exist_ok=True)
    for slug, spec in ORGS.items():
        used: set = set()
        for e in spec["events"]:
            e["url"] = e["url"] or find_url(slug, e["title"], used) or ""
            if e["url"] and urlsplit(e["url"]).path in ("", "/"):
                e["url"] = ""      # a bare site root is not the event's own page
            if e["url"]:
                used.add(e["url"])
        valid = [e for e in spec["events"] if e["valid"]]
        spec.update(slug=slug, snapshot_date=SNAPSHOT,
                    ideal_pages=["page_01.html", "page_02.html"] if slug == "arita" else ["page_01.html"],
                    verified_by="manual: rendered page text read event-by-event on 2026-10-07 (see baseline/raw/%s/)" % slug,
                    true_event_count=len(valid))
        with open(os.path.join("baseline", f"{slug}.json"), "w", encoding="utf-8") as f:
            json.dump(spec, f, indent=1, ensure_ascii=False)
        no_url = [e["title"] for e in valid if not e["url"]]
        print(f"{slug:22} valid={len(valid):3} invalid={len(spec['events']) - len(valid):3} no-url={len(no_url)}", ("| " + "; ".join(no_url[:3])) if no_url else "")


if __name__ == "__main__":
    main()
