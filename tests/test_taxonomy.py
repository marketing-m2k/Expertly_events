"""Tax, Finance and Legal are broad professional fields, not three keywords.
What counts is defined in scraper/taxonomy.json (and given to the Gemini
review as well). These tests use the topic list the user supplied on
2026-09-24 -- the list is only examples and is meant to grow.

    python -m pytest tests/
"""

from scraper.ai_review import PROMPT
from scraper.classify_rules import classify_event, load_taxonomy, taxonomy_topics

USER_TAXONOMY = {
    "Tax": ["International Tax", "Corporate Tax", "Indirect Tax", "Transfer Pricing", "US Tax", "India Tax",
            "Tax Compliance", "Tax Technology", "Tax Controversy", "Tax Policy & Regulation", "M&A Tax",
            "Expatriate Tax", "Employment Tax", "Wealth & Estate Tax", "Real Estate Tax", "Customs & Trade Tax"],
    "Finance": ["Corporate Finance", "Investment Banking", "Accounting & Financial Reporting", "Audit & Assurance",
                "FP&A", "CFO & Finance Leadership", "Mergers & Acquisitions", "Private Equity", "Venture Capital",
                "Banking & Financial Services", "FinTech", "Risk Management", "Treasury Management",
                "ESG & Sustainable Finance", "Wealth Management", "Asset Management", "Capital Markets",
                "Financial Regulation", "Insurance & Reinsurance", "Cryptocurrency & Digital Assets"],
    "Legal": ["Corporate Law", "Commercial Law", "M&A Law", "Tax Law", "Corporate Governance",
              "Regulatory & Compliance", "Securities Law", "Banking & Finance Law", "Employment Law",
              "Intellectual Property", "Data Privacy & Protection", "Technology & Cyber Law",
              "Competition & Antitrust", "Insolvency & Restructuring", "Dispute Resolution", "International Law",
              "Real Estate Law", "ESG & Sustainability Law", "Contract Law", "Foreign Investment Law"],
}
ACCEPTED = ("Tax", "Finance", "Legal")


def test_every_topic_the_user_listed_is_accepted_as_an_event_title():
    for category, topics in USER_TAXONOMY.items():
        for topic in topics:
            for title in (topic, f"{topic} Conference 2026", f"Annual {topic} Summit"):
                result = classify_event(title)
                assert result["category"] in ACCEPTED, (category, title, result)


def test_topics_that_belong_to_one_field_get_that_field():
    cases = (("Intellectual Property Forum", "Legal"), ("Data Privacy and Protection Summit", "Legal"),
             ("Insolvency and Restructuring Conference", "Legal"), ("Competition and Antitrust Update", "Legal"),
             ("Private Equity and Venture Capital Forum", "Finance"), ("Cryptocurrency and Digital Assets Summit", "Finance"),
             ("Reinsurance Conference", "Finance"), ("Treasury Management Masterclass", "Finance"),
             ("Expatriate Tax Briefing", "Tax"), ("Customs and Trade Symposium", "Tax"),
             ("Foreign Investment Law Seminar", "Legal"), ("Employment Law Update", "Legal"))
    for title, expected in cases:
        assert classify_event(title)["category"] == expected, title


def test_corporate_governance_counts_as_legal():
    assert classify_event("Tasmanian Corporate Governance Forum 2026")["category"] == "Legal"


def test_a_single_generic_word_is_not_enough_on_rules_alone():
    for vague in ("Risk Culture Workshop", "Sustainability Roundtable", "Policy Forum", "Annual Conference 2026"):
        assert classify_event(vague)["category"] == "Unclear", vague


def test_one_passing_mention_in_the_description_is_not_enough():
    assert classify_event("Members Night", "A relaxed evening. Speakers touch on tax briefly.")["category"] == "Unclear"
    assert classify_event("Members Night", "Speakers cover transfer pricing and indirect tax reform.")["category"] == "Tax"


def test_and_matches_ampersand_and_plurals_and_stems():
    assert classify_event("Mergers & Acquisitions Forum")["category"] == "Finance"
    assert classify_event("Arbitrators Roundtable")["category"] == "Legal"
    assert classify_event("Trade Marks and Patents Update")["category"] == "Legal"


def test_no_false_hits_inside_other_words():
    assert classify_event("Taxonomy of Learning Styles")["category"] == "Unclear"
    assert classify_event("Lawn Bowls Championship")["category"] == "Unclear"


def test_the_reject_rules_still_win():
    assert classify_event("CFE Exam Review Course")["category"] == "Reject"
    assert classify_event("Tax Professionals Golf Day")["category"] == "Reject"


def test_the_taxonomy_file_is_valid_and_lists_topics_for_all_three_fields():
    assert set(load_taxonomy()) == set(ACCEPTED)
    assert all(len(topics) >= 15 for topics in taxonomy_topics().values())


def test_the_ai_review_prompt_carries_the_same_taxonomy():
    for phrase in ("Insolvency and Restructuring", "Data Privacy and Protection", "Cryptocurrency and Digital Assets",
                   "Foreign Investment Law", "Transfer Pricing", "not a complete list", "BROAD"):
        assert phrase in PROMPT, phrase
