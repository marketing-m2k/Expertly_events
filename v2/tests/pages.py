"""Builders for realistic event DETAIL pages used by the extraction / quality / Gemini / pipeline tests."""

from __future__ import annotations

from datetime import date

TODAY = date(2026, 10, 7)
URL = "https://inst.org/events/gst-audit-workshop"

FILLER = "<p>" + "This professional development session is designed for tax practitioners, finance leaders and advisers who want practical, current guidance. " * 3 + "</p>"

RELATED = """
<section class="related-events"><h2>Related events</h2>
<ul>
<li><a href="https://inst.org/events/vat-update">VAT Update Seminar</a> <span>3 December 2026</span></li>
<li><a href="https://inst.org/events/payroll-day">Payroll Day</a> <span>9 December 2026</span></li>
<li><a href="https://inst.org/events/audit-forum">Audit Forum</a> <span>15 January 2027</span></li>
</ul></section>"""

CONTACT = """
<section class="contact"><h3>Contact us</h3><p><b>Address:</b> 1 Head Office Road, Sydney NSW 2000</p><p><b>Phone:</b> 02 5555 0000</p></section>"""

COOKIE = "<div id='cookie-banner' class='cookie-consent'><p>We use cookies to improve your experience. Accept all cookies or manage settings in our privacy policy.</p></div>"


def event_page(title="GST Audit Workshop", *, h1=None, date_line="12 October 2026", date_label="Date", venue="Hilton Chennai", venue_label="Venue",
               address=None, desc=None, related=False, contact=False, jsonld=None, cookie=False, extra_head="", extra_body="", wrap_form=False,
               fmt=None, og_title=None, title_tag=None, container="main", nav=True, time_line=None) -> str:
    h1 = title if h1 is None else h1
    rows = ""
    if date_line is not None:
        rows += f"<p><b>{date_label}:</b> {date_line}</p>"
    if time_line:
        rows += f"<p><b>Time:</b> {time_line}</p>"
    if venue is not None:
        rows += f"<p><b>{venue_label}:</b> {venue}</p>"
    if address:
        rows += f"<p><b>Address:</b> {address}</p>"
    if fmt:
        rows += f"<p><b>Format:</b> {fmt}</p>"
    desc = FILLER if desc is None else desc
    body = f"""<{container}>{COOKIE if cookie else ''}<h1>{h1}</h1>{rows}{desc}
        <a href="https://inst.org/register/gst-audit">Register now</a>{extra_body}{RELATED if related else ''}{CONTACT if contact else ''}</{container}>"""
    if wrap_form:
        body = f"<form method='post' action='/events/gst-audit-workshop' id='aspnetForm'><input type='hidden' name='__VIEWSTATE' value='abc'/>{body}</form>"
    nav_html = "<nav><a href='/'>Home</a><a href='/events'>Events</a><a href='/about'>About</a><a href='/contact'>Contact</a></nav>" if nav else ""
    ld = f"<script type='application/ld+json'>{jsonld}</script>" if jsonld else ""
    og = f"<meta property='og:title' content=\"{og_title}\">" if og_title else ""
    return (f"<!doctype html><html><head><title>{title_tag or (title + ' | Example Institute')}</title>{og}{ld}{extra_head}</head>"
            f"<body>{nav_html}{body}<footer><p>Copyright 2026 Example Institute. All rights reserved.</p><p>1 Head Office Road, Sydney NSW 2000</p></footer></body></html>")


def jsonld_event(name="GST Audit Workshop", start="2026-10-12", end=None, url=URL, venue="Hilton Chennai", city="Chennai", mode=None) -> str:
    end_s = f',"endDate":"{end}"' if end else ""
    mode_s = f',"eventAttendanceMode":"https://schema.org/{mode}"' if mode else ""
    return (f'{{"@context":"https://schema.org","@type":"EducationEvent","name":"{name}","startDate":"{start}"{end_s},"url":"{url}"{mode_s},'
            f'"location":{{"@type":"Place","name":"{venue}","address":{{"@type":"PostalAddress","streetAddress":"2 Mount Road","addressLocality":"{city}","addressCountry":"India"}}}}}}')
