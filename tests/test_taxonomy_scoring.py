"""Found by replaying the saved AUS events through the broader taxonomy
(24 Sep 2026): generic words must never accept an event on their own, a
dinner is social, and list pages' menu/promo/link text is not an event."""

from scraper.classify_rules import classify_event
from scraper.label_extract import is_generic_title


def test_two_generic_words_in_a_title_never_accept_an_event():
    assert classify_event("All Policy & Professional Standards")["category"] == "Unclear"
    assert classify_event("Risk Policy Standards Forum")["category"] == "Unclear"


def test_description_only_evidence_needs_three_topic_mentions():
    assert classify_event("Members Night", "Speakers cover bank and compliance.")["category"] == "Unclear"
    assert classify_event("Members Night", "Speakers cover transfer pricing and indirect tax reform.")["category"] == "Tax"


def test_a_generic_word_in_the_title_helps_but_a_topic_is_still_required():
    assert classify_event("Risk Forum", "Speakers cover credit risk and market risk.")["category"] == "Finance"


def test_a_dinner_is_a_social_event():
    assert classify_event("Young Lawyers' Premium Dinner 2026")["category"] == "Reject"
    assert classify_event("Tax Institute Annual Gala Dinner")["category"] == "Reject"


def test_menu_promo_and_listing_links_are_not_events():
    for junk in ("Become an event sponsor", "Save by becoming a member", "Partner with us", "Global events",
                 "Australian Capital Territory events", "Hong Kong events", "Sponsorship opportunities",
                 "Contact us", "Join us"):
        assert is_generic_title(junk), junk


def test_real_events_with_vague_names_are_kept_for_the_ai_review():
    for real in ("Tasmanian Convention", "National Congress 2026", "Property Intensive",
                 "Melbourne Super Discussion Groups October 2026", "GST Events Update Workshop"):
        assert not is_generic_title(real), real
