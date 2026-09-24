"""More findings from replaying saved AUS events (24 Sep 2026)."""

from scraper.classify_rules import classify_event
from scraper.label_extract import is_generic_title


def test_a_real_event_named_with_the_singular_word_event_is_never_dropped_as_menu_text():
    assert not is_generic_title("Australia's biggest governance event")
    assert not is_generic_title("The Annual Tax Event")
    assert is_generic_title("Hong Kong events")  # the plural is a listing link


def test_common_tax_vocabulary_is_recognised():
    assert classify_event("Part IVA Risks for SMEs")["category"] == "Tax"
    assert classify_event("Trusts Intensive")["category"] == "Tax"
    assert classify_event("ATO Compliance Update")["category"] == "Tax"
    assert classify_event("HMRC Investigations Briefing")["category"] == "Tax"


def test_super_alone_is_only_a_generic_word():
    assert classify_event("Melbourne Super Discussion Groups")["category"] == "Unclear"
    assert classify_event("Super Discussion Group", "Superannuation contributions and SMSF audit rules.")["category"] == "Finance"
