"""Card discovery: must not need two identical siblings, must not lose events to generic titles,
nested groups or alternating layouts, and must keep archives out of current discovery."""

from pipeline.discovery.cards import find_cards, strip_date_affix
from pipeline.discovery.jsonld import parse_jsonld_events, validate_for_page
from tests.helpers import card_article, cards, page

URL = "https://ex.org/events"


def found(html, url=URL):
    cs, drops, stats = find_cards(html, url)
    return cs


def titles(cs):
    return [c.title for c in cs if not c.in_archive]


def test_normal_cards():
    cs = found(page("<h1>Events</h1>" + cards(5)))
    assert len(cs) == 5 and all(c.date_hint and c.url for c in cs)
    assert titles(cs)[0] == "Tax Workshop 1"


def test_single_event_is_found():
    cs = found(page("<h1>Events</h1>" + card_article(1)))
    assert len(cs) == 1 and cs[0].title == "Tax Workshop 1"


def test_featured_card_and_list_with_different_markup():
    featured = ("<section class='hero'><div class='big'><img src='x.jpg'><h2><a href='https://ex.org/events/annual-tax-summit-2026'>Annual Tax Summit 2026</a></h2>"
                "<span>18 November 2026</span><a class='btn' href='https://ex.org/events/annual-tax-summit-2026/register'>Register</a></div></section>")
    listing = ("<ul><li><a href='https://ex.org/events/vat-update-2026'><strong>VAT Update</strong></a> <em>3 December 2026</em></li>"
               "<li><div><a href='https://ex.org/events/transfer-pricing-day'>Transfer Pricing Day</a></div><div>9 December 2026</div></li></ul>")
    cs = found(page("<h1>Events</h1>" + featured + listing))
    assert {c.title for c in cs} == {"Annual Tax Summit 2026", "VAT Update", "Transfer Pricing Day"}
    hero = next(c for c in cs if c.title.startswith("Annual"))
    assert len(hero.anchors) == 2                      # title link + Register button are ONE event


def test_alternating_layouts_each_card_found():
    html = "".join(
        card_article(i, cls="odd" if i % 2 else "even-variant-xyz") if i % 3 else
        f"<div class='wide'><a href='https://ex.org/events/special-{i}'><h4>Special Briefing {i}</h4></a><span>{i + 10} November 2026</span></div>"
        for i in range(1, 8))
    cs = found(page(html))
    assert len(cs) == 7


def test_cards_where_the_title_is_not_inside_the_link_and_date_is_outside_the_title():
    html = ("<div class='row'><div class='left'><span class='d'>12 November 2026</span></div>"
            "<div class='right'><h3>Estate Planning Forum</h3><p>Hilton Hotel</p><a href='https://ex.org/events/estate-planning-forum'>Read more</a></div></div>")
    cs = found(page(html))
    assert len(cs) == 1 and cs[0].title == "Estate Planning Forum" and cs[0].date_hint == "12 November 2026"


def test_generic_titles_are_kept_not_discarded():
    html = ("<article><h3>Webinar</h3><p>12 November 2026</p><a href='https://ex.org/events/gst-webinar-2026'>Read more</a></article>"
            "<article><h3>Event - Update</h3><p>13 November 2026</p><a href='https://ex.org/events/payroll-update-2026'>Read more</a></article>")
    cs = found(page(html))
    assert len(cs) == 2
    assert all(c.title_confidence == "low" for c in cs) and all(c.title for c in cs)
    assert any("generic" in " ".join(c.notes) for c in cs)


def test_a_nested_group_does_not_suppress_the_real_list():
    inner = "".join(card_article(i) for i in range(1, 5))
    html = f"<div class='calendar'><div class='month'><h2>November 2026</h2>{inner}</div></div>"
    assert len(found(page(html))) == 4


def test_date_line_below_the_title_is_attributed_to_the_right_event():
    html = ("<div><h3><a href='https://ex.org/p/one-day-conference'>One Day Conference</a></h3><p>Monday 19 October 2026</p>"
            "<h3><a href='https://ex.org/p/two-day-summit'>Two Day Summit</a></h3><p>Thursday 12 November 2026</p></div>")
    cs = {c.title: c.date_hint for c in found(page(html))}
    assert cs["One Day Conference"] == "Monday 19 October 2026" and cs["Two Day Summit"] == "Thursday 12 November 2026"


def test_date_heading_above_a_group_of_cards():
    html = ("<div><h2>12 November 2026</h2><div><a href='https://ex.org/events/morning-session-a'><h4>Morning Session A</h4></a></div>"
            "<div><a href='https://ex.org/events/morning-session-b'><h4>Morning Session B</h4></a></div>"
            "<h2>14 November 2026</h2><div><a href='https://ex.org/events/evening-gala-c'><h4>Evening Gala C</h4></a></div></div>")
    cs = {c.title: c.date_hint for c in found(page(html))}
    assert cs["Morning Session A"] == "12 November 2026" and cs["Morning Session B"] == "12 November 2026"
    assert cs["Evening Gala C"] == "14 November 2026"


def test_navigation_and_category_links_are_not_events():
    html = ("<ul><li><a href='https://ex.org/events/category/tax'>Tax</a></li><li><a href='https://ex.org/news/latest'>Latest news</a></li>"
            "<li><a href='https://ex.org/login'>Log in</a></li><li><a href='https://ex.org/events/page/2'>2</a></li></ul>" + card_article(1))
    cs = found(page(html))
    assert titles(cs) == ["Tax Workshop 1"]


def test_past_events_section_is_flagged_archive():
    html = ("<h2>Upcoming events</h2>" + cards(2) + "<h2>Past events</h2>" + cards(3, start=10))
    cs = found(page(html))
    assert sum(1 for c in cs if not c.in_archive) == 2 and sum(1 for c in cs if c.in_archive) == 3


def test_archive_by_class_name_is_flagged():
    html = "<div class='upcoming-events'>" + cards(2) + "</div><div class='past-events'>" + cards(2, start=10) + "</div>"
    cs = found(page(html))
    assert [c.in_archive for c in cs] == [False, False, True, True]


def test_dates_without_links_become_text_date_candidates():
    html = "<table><tr><td>14 November 2026</td><td>Audit Committee Roundtable</td></tr><tr><td>20 November 2026</td><td>Treasury Briefing</td></tr></table>"
    cs = found(page(html))
    assert {c.title for c in cs} == {"Audit Committee Roundtable", "Treasury Briefing"} and all("text_date" in c.methods for c in cs)


def test_listing_date_formats_are_recognised():
    for fmt in ("Mar 12 2026", "12 March 2026", "12/03/2026", "2026-03-12"):
        html = f"<article><h3>Workshop A</h3><p>{fmt}</p><a href='https://ex.org/events/workshop-a-2026'>more</a></article>"
        assert found(page(html))[0].date_hint, fmt


def test_deadline_dates_are_not_the_card_date():
    html = "<article><h3>Insolvency Forum</h3><p>Registration closes 5 October 2026</p><p>Forum: 20 October 2026</p><a href='https://ex.org/events/insolvency-forum-2026'>Details</a></article>"
    assert found(page(html))[0].date_hint == "20 October 2026"


def test_title_date_prefix_is_split_off():
    assert strip_date_affix("24 September 2026: Farming Conference") == "Farming Conference"
    assert strip_date_affix("Farming Conference - 24 September 2026") == "Farming Conference"
    assert strip_date_affix("Tax Update 2026") == "Tax Update 2026"


def test_two_links_in_one_card_are_one_event():
    html = ("<div class='card'><h3><a href='https://ex.org/events/chapter-tax-day'>Chapter Tax Day</a></h3><p>13 October 2026</p>"
            "<a href='https://tickets.example.com/e/12345'>Buy tickets</a><a href='https://ex.org/events/chapter-tax-day'>Read More</a></div>"
            "<div class='card'><h3><a href='https://ex.org/events/second-day'>Second Day</a></h3><p>14 October 2026</p></div>")
    cs = found(page(html))
    assert len(cs) == 2


# ---- JSON-LD ------------------------------------------------------------------------------------------------------------

def jsonld(*blobs):
    return "".join(f"<script type='application/ld+json'>{b}</script>" for b in blobs)


def test_jsonld_events_are_read():
    html = page(jsonld('{"@type":"Event","name":"Tax Day","startDate":"2026-11-02","url":"https://ex.org/e/tax-day","location":{"@type":"Place","name":"Hall","address":{"streetAddress":"1 Main St","addressLocality":"Leeds"}}}'))
    ev, problems = parse_jsonld_events(html, URL)
    assert len(ev) == 1 and ev[0]["location"]["locality"] == "Leeds" and not problems


def test_malformed_jsonld_does_not_crash_or_hide_the_other_events():
    html = page(jsonld('{"@type":"Event","startDate":"2026-11-02"}',                   # no name, no url
                       '{"@type":"Event"',                                           # not even JSON
                       '{"@graph":[{"@type":"Event","name":"Good One","startDate":"2026-11-05","url":"https://ex.org/e/good"},'
                       '{"@type":"Event","name":["x"],"location":42,"organizer":[null]}]}'))
    ev, problems = parse_jsonld_events(html, URL)
    names = [e["name"] for e in ev]
    assert "Good One" in names
    reasons = {p["reason"] for p in problems}
    assert "jsonld_unparseable" in reasons and "jsonld_missing_name" in reasons


def test_jsonld_with_trailing_comma_is_repaired():
    ev, problems = parse_jsonld_events(page(jsonld('{"@type":"Event","name":"T","startDate":"2026-11-02","url":"https://ex.org/e/t",}')), URL)
    assert len(ev) == 1


def test_jsonld_belonging_to_another_event_is_flagged():
    recs, _ = parse_jsonld_events(page(jsonld('{"@type":"Event","name":"Other Webinar","startDate":"2026-12-01","url":"https://ex.org/e/other"}')), URL)
    out = validate_for_page(recs, "https://ex.org/e/this-event", ["This Event Title"])
    assert out[0]["belongs"] is False
    out = validate_for_page(recs, "https://ex.org/e/other", ["x"])
    assert out[0]["belongs"] is True
