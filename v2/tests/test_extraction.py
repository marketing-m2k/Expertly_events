"""Phase 3 extraction: scoped to the event, label/value associations kept, nothing guessed."""

import json
import os
from datetime import date

import pytest

from pipeline.extraction.detail import extract_event
from tests.pages import RELATED, TODAY, URL, event_page, jsonld_event

LIVE = os.path.join(os.path.dirname(__file__), "fixtures", "live")


def ex(html, **kw):
    return extract_event(html, kw.pop("url", URL), org_name=kw.pop("org_name", "Example Institute"), today=TODAY, **kw)


def val(ev, k):
    return ev.fields[k].value


def test_correct_date_and_venue_from_labels():
    ev = ex(event_page())
    assert val(ev, "name") == "GST Audit Workshop"
    assert val(ev, "start_date") == "2026-10-12" and ev.fields["start_date"].source == "label"
    assert val(ev, "venue") == "Hilton Chennai"
    assert "Date:" in ev.fields["start_date"].evidence                      # the evidence keeps the label/value relationship


def test_related_event_dates_never_become_the_events_date():
    ev = ex(event_page(date_line=None, related=True))                         # the page itself states NO date; related events do
    assert val(ev, "start_date") is None
    assert ev.fields["start_date"].verification == "unresolved"
    texts = " ".join(ev.ctx.excluded_texts)
    assert "VAT Update Seminar" in texts and "3 December 2026" in texts       # kept apart as excluded evidence, not used


def test_the_events_own_date_wins_over_related_events_dates():
    ev = ex(event_page(date_line="12 October 2026", related=True))
    assert val(ev, "start_date") == "2026-10-12" and "Related events" not in ev.ctx.container_text


def test_contact_address_is_never_the_venue():
    ev = ex(event_page(venue=None, contact=True))
    assert val(ev, "venue") is None and val(ev, "address") is None
    assert "1 Head Office Road" in " ".join(ev.ctx.excluded_texts)               # the contact block is kept apart, never used


def test_event_address_is_taken_but_contact_address_is_not():
    ev = ex(event_page(venue="Hilton Chennai", address="2 Mount Road, Chennai 600002", contact=True))
    assert val(ev, "address") == "2 Mount Road, Chennai 600002"


def test_label_value_pairs_stay_associated():
    ev = ex(event_page(date_line="12 October 2026", venue="Hilton Chennai", related=True, contact=True,
                       extra_body="<dl><dt>Date</dt><dd>1 January 2027</dd><dt>Venue</dt><dd>Wrong Hotel</dd></dl>"))
    # a second Date/Venue pair exists on the page: that must be a CONFLICT, not 'whichever label came first'
    assert ev.fields["start_date"].verification == "conflict" or val(ev, "start_date") == "2026-10-12"
    pairs = [(p.canon, p.value) for p in ev.ctx.pairs]
    assert ("date", "12 October 2026") in pairs and ("venue", "Hilton Chennai") in pairs


def test_title_conflict_between_heading_and_meta_is_reported():
    ev = ex(event_page(h1="Annual Dinner Gala", og_title="Transfer Pricing Masterclass", title_tag="Transfer Pricing Masterclass | Inst"), card_title="")
    assert ev.fields["name"].verification == "conflict" or ev.fields["name"].confidence != "high"
    assert any(a["value"] for a in ev.fields["name"].alternatives)


def test_h1_is_not_blindly_trusted_when_it_is_generic():
    ev = ex(event_page(h1="Events", og_title="Insolvency Practitioners Forum", title_tag="Insolvency Practitioners Forum | Inst"))
    assert val(ev, "name") == "Insolvency Practitioners Forum"


def test_title_sources_agree_gives_high_confidence():
    ev = ex(event_page(), card_title="GST Audit Workshop")
    assert ev.fields["name"].confidence == "high"


def test_jsonld_of_the_page_is_used_and_validated():
    ev = ex(event_page(jsonld=jsonld_event(), date_line=None, venue=None))
    assert val(ev, "start_date") == "2026-10-12" and ev.fields["start_date"].source == "jsonld"
    assert val(ev, "venue") == "Hilton Chennai" and val(ev, "city") == "Chennai"


def test_jsonld_belonging_to_another_event_is_not_used():
    other = jsonld_event(name="Completely Different Webinar", start="2027-03-03", url="https://inst.org/events/other-webinar")
    ev = ex(event_page(jsonld=other, date_line="12 October 2026"))
    assert val(ev, "start_date") == "2026-10-12" and ev.fields["start_date"].source == "label"
    assert ev.ctx.jsonld is None and len(ev.ctx.jsonld_foreign) == 1


def test_jsonld_conflicting_with_the_page_label_is_a_conflict():
    ev = ex(event_page(jsonld=jsonld_event(start="2026-11-20"), date_line="12 October 2026"))
    fv = ev.fields["start_date"]
    assert fv.verification == "conflict" and fv.value is None
    assert {a["value"] for a in fv.alternatives} >= {"2026-11-20", "2026-10-12"}


def test_missing_year_is_never_invented():
    ev = ex(event_page(date_line="12 October"))
    fv = ev.fields["start_date"]
    assert fv.value is None and "without a year" in fv.note and "year_missing" in fv.note
    assert any("12 October" in a["evidence"] for a in fv.alternatives)


def test_year_established_from_weekday_is_allowed_and_explained():
    ev = ex(event_page(date_line="Monday 12 October"))
    fv = ev.fields["start_date"]
    assert fv.value == "2026-10-12" and "weekday" in fv.note


def test_year_corroborated_by_the_listing_card():
    ev = ex(event_page(date_line="12 October"), card_date_hint="12 October 2026")
    fv = ev.fields["start_date"]
    assert fv.value == "2026-10-12" and "listing card" in fv.note
    ev = ex(event_page(date_line="12 October"), card_date_hint="13 October 2026")
    assert ev.fields["start_date"].value is None


def test_deadline_date_is_not_the_event_date():
    html = event_page(date_line=None, extra_body="<p>Registration closes 5 October 2026.</p><p>Early bird until 1 September 2026</p>")
    assert val(ex(html), "start_date") is None


def test_date_range_gives_start_and_end_and_times():
    ev = ex(event_page(date_line="12-14 October 2026", time_line="9:00 AM - 5:00 PM AEST"))
    assert (val(ev, "start_date"), val(ev, "end_date")) == ("2026-10-12", "2026-10-14")
    assert (val(ev, "start_time"), val(ev, "end_time"), val(ev, "timezone")) == ("09:00", "17:00", "AEST")


def test_gst_in_a_title_is_not_a_timezone():
    ev = ex(event_page(title="National GST Conference", date_line="12 October 2026"))
    assert val(ev, "timezone") is None


def test_description_skips_cookie_banner_boilerplate_and_related_blocks():
    desc = "<p>Join us for a practical session on GST audit readiness, covering notices, reconciliations and departmental audits in detail.</p>"
    ev = ex(event_page(cookie=True, desc=desc, related=True))
    d = val(ev, "description")
    assert d.startswith("Join us for a practical session") and "cookies" not in d.lower() and "VAT Update" not in d


def test_description_that_swallowed_a_related_event_title_is_flagged():
    from pipeline.quality import assess_quality
    ev = ex(event_page(desc="<p>Also see our VAT Update Seminar and other great sessions coming up this season at the institute.</p>", related=True))
    q = assess_quality(ev, today=TODAY)
    assert ev.fields["description"].verification == "conflict" and "unrelated_content" in q.flags


def test_aspnet_form_wrapped_page_keeps_its_content():
    ev = ex(event_page(wrap_form=True, date_line="12 October 2026", venue="Hilton Chennai"))
    assert val(ev, "start_date") == "2026-10-12" and val(ev, "venue") == "Hilton Chennai" and "GST Audit Workshop" in ev.ctx.container_text


def test_format_is_read_from_the_page_only():
    assert val(ex(event_page(fmt="Online (Zoom)")), "format") == "Virtual"
    assert val(ex(event_page(fmt="In person")), "format") == "In Person"
    assert val(ex(event_page(jsonld=jsonld_event(mode="MixedEventAttendanceMode"))), "format") == "Hybrid"
    assert val(ex(event_page()), "format") is None                                  # nothing said -> nothing guessed


def test_online_venue_means_virtual_not_missing_location():
    ev = ex(event_page(venue="Online via Zoom"))
    assert val(ev, "format") == "Virtual" and val(ev, "venue") is None


def test_unreadable_page_is_marked_not_guessed():
    ev = ex("<html><body><div id='root'></div></body></html>", card_title="GST Audit Workshop")
    assert not ev.readable and ev.fields["start_date"].verification == "unresolved"
    assert ev.fields["name"].source == "card" and ev.fields["name"].confidence == "low"
    ev = ex("")
    assert not ev.readable


def test_list_page_is_detected_when_the_link_is_not_an_event_page():
    items = "".join(f"<li><a href='https://inst.org/events/e{i}'>Event number {i}</a> <span>{i + 10} November 2026</span></li>" for i in range(1, 6))
    ev = ex(f"<html><body><main><h1>Events</h1><p>{'Browse all of our upcoming professional events below. ' * 6}</p><ul>{items}</ul></main></body></html>")
    assert ev.page_kind == "listing"


def test_hero_banner_outside_main_is_included():
    html = ("<html><body><div class='hero'><h1>Digital Assets Conference</h1><h2>Friday 13 November 2026, Chartered Accountants' Hall</h2></div>"
            f"<main>{'<p>' + 'Learn about digital assets, audit and regulation in this full day conference. ' * 4 + '</p>'}</main></body></html>")
    ev = ex(html)
    assert val(ev, "start_date") == "2026-11-13"


@pytest.mark.parametrize("slug,expect_date,expect_name", [
    ("taxinst_sa", "2027-03-04", "SA Tax Forum"),            # card said March 2026; the event page says 2027: the page wins
    ("taxinst_gst", "2026-10-15", "National GST Conference"),
    ("fta_hub", "2026-10-05", "2026 Tobacco Basic Training"),
    ("cfa_vips", "2026-10-24", None),
    ("icaew_digital", "2026-11-13", "Digital Assets Conference"),
])
def test_real_event_pages(slug, expect_date, expect_name):
    p = os.path.join(LIVE, f"{slug}.html")
    if not os.path.exists(p):
        pytest.skip("fixture not captured")
    meta = json.load(open(os.path.join(LIVE, f"{slug}.json")))
    ev = extract_event(open(p, encoding="utf-8").read(), meta["url"], final_url=meta["final_url"], org_name="Org", today=TODAY)
    assert val(ev, "start_date") == expect_date
    if expect_name:
        assert val(ev, "name") == expect_name


def test_real_page_session_level_times_are_not_the_events_time():
    p = os.path.join(LIVE, "taxinst_gst.html")
    if not os.path.exists(p):
        pytest.skip("fixture not captured")
    meta = json.load(open(os.path.join(LIVE, "taxinst_gst.json")))
    ev = extract_event(open(p, encoding="utf-8").read(), meta["url"], org_name="Org", today=TODAY)
    assert val(ev, "start_time") is None and val(ev, "address") is None        # the 'Networking function' block's time/venue
    assert val(ev, "end_date") == "2026-10-16"


def test_real_page_year_from_weekday():
    p = os.path.join(LIVE, "ciot_conf.html")
    if not os.path.exists(p):
        pytest.skip("fixture not captured")
    meta = json.load(open(os.path.join(LIVE, "ciot_conf.json")))
    ev = extract_event(open(p, encoding="utf-8").read(), meta["url"], org_name="Org", today=TODAY)
    assert val(ev, "start_date") == "2026-10-06" and "weekday" in ev.fields["start_date"].note
