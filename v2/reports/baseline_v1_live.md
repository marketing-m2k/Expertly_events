# Discovery measurement - engine `v1`, mode `live`

**Discovery recall: 57.4%** (58 of 101 manually verified events found across 11 organisations)

| Measure | Value |
|---|---|
| Candidates returned | 233 |
| Missed valid events | 43 |
| Duplicate candidates | 0 |
| Extras (matched nothing in baseline) | 171 |
| ...of which already past (archive leakage) | 2 |
| Orgs silently empty (no events, no error reported) | 2 |
| Completeness state correct | 0 / 0 |
| Matched but missing title/link | 0 |
| Orgs with errors | 0 |

## Why events were missed

| Reason | Events |
|---|---|
| not_in_fetched_pages | 15 |
| card_group_low_score | 10 |
| no_repeated_card_group | 9 |
| generic_title_discarded | 7 |
| in_page_but_not_extracted | 2 |

## Per organisation

| Org | True | Found | Matched | Recall | Missed | Dup | Extra (past) | State | Expected | Pages | Sec |
|---|---|---|---|---|---|---|---|---|---|---|---|
| arita | 13 | 5 | 1 | 7.7% | 12 | 0 | 4 (0) | - | discovery_complete | 1 | 16.1 |
| cfa-india | 3 | 15 | 3 | 100.0% | 0 | 0 | 12 (0) | - | discovery_complete | 1 | 15.5 |
| ciot | 4 | 0 | 0 | 0.0% | 4 | 0 | 0 (0) | - | discovery_complete | 1 | 7.3 |
| fta | 3 | 3 | 3 | 100.0% | 0 | 0 | 0 (0) | - | discovery_complete | 2 | 17.1 |
| icaew | 12 | 25 | 1 | 8.3% | 11 | 0 | 24 (1) | - | discovery_complete | 1 | 27.4 |
| icsi | 0 | 0 | 0 | n/a | 0 | 0 | 0 (0) | - | discovery_failed | 1 | 6.2 |
| isca-sg | 8 | 23 | 3 | 37.5% | 5 | 0 | 20 (0) | - | discovery_complete | 1 | 19.3 |
| picpa-dubai | 7 | 19 | 7 | 100.0% | 0 | 0 | 12 (0) | - | discovery_complete | 2 | 28.9 |
| taxation-society-uae | 1 | 99 | 1 | 100.0% | 0 | 0 | 98 (1) | - | discovery_complete | 1 | 31.6 |
| taxinstitute | 16 | 10 | 5 | 31.2% | 11 | 0 | 1 (0) | - | discovery_complete | 3 | 24.7 |
| tei | 34 | 34 | 34 | 100.0% | 0 | 0 | 0 (0) | - | discovery_complete | 1 | 8.6 |

### arita

- MISSED: Vic/Tas Division Conference and Dinner 2026 (14th Oct 2026) - card_group_low_score
- MISSED: NSW/ACT Division Conference and Dinner 2026 (29th Oct 2026) - card_group_low_score
- MISSED: NSW Young Professionals Seminar: Creditor activism - The good, the bad and the ugly (29th Oct 2026) - card_group_low_score
- MISSED: WA Forum: Views from the Bench (29th Oct 2026) - card_group_low_score
- MISSED: Expert Series: Restructuring & Turnaround (5th Nov 2026) - card_group_low_score
- MISSED: Expert Series: Bankruptcy (17th Nov 2026) - card_group_low_score
- MISSED: Hunter Forum & EOY drinks: 2026 in review (19th Nov 2026) - card_group_low_score
- MISSED: NSW Young Professionals End-of-year Social - Cruise (19th Nov 2026) - card_group_low_score
- MISSED: Vic/Tas Young Professionals EOY lawn bowls (20th Nov 2026) - not_in_fetched_pages
- MISSED: Expert Series: Insolvency (26th Nov 2026) - not_in_fetched_pages
- MISSED: WA Young Professionals End-of-year Social (26th Nov 2026) - not_in_fetched_pages
- MISSED: Expert Series: Small Practice (8th Dec 2026) - not_in_fetched_pages
- extra: Education | 
- extra: About | 
- extra: Insolvency Help | 
- extra: News | 

### cfa-india

- extra: Research Paper Discussion | The End of ESG | 03-Oct-2026
- extra: Uncovering the Crane Industry with CEO of Trishakti Industries | Kolkata | 03-Oct-2026
- extra: From Statistics to Money: The Hidden Mathematics of Goals-Based Investing | Hyde | 27-Sep-2026
- extra: 11th India Wealth Management Conference | 26-Sep-2026
- extra: The Next Era of Wealth Management: AI, Behaviour & Judgment | Bengaluru | 19-Sep-2026
- extra: Global Investment Opportunity and Risks | Delhi | 22-Aug-2026
- ... and 6 more extras

### ciot

- **SILENTLY EMPTY**: returned nothing and reported no problem.
- MISSED: CIOT at Conservative Party Conference (Tuesday 6 October 2026) - no_repeated_card_group
- MISSED: Tax Aid: OMB and Private Client Update Virtual Conference (Monday 19 October 2026) - no_repeated_card_group
- MISSED: Indirect Taxes Conference (Thursday 12 November 2026) - no_repeated_card_group
- MISSED: CTA Admission Ceremony (Wednesday 24 February 2027) - no_repeated_card_group

### icaew

- MISSED: Farming and Rural Business Conference (24 September 2026) - not_in_fetched_pages
- MISSED: Solicitors Conference (6 October 2026) - not_in_fetched_pages
- MISSED: Global Ethics Day (23 October 2026) - not_in_fetched_pages
- MISSED: Internal Audit Conference (5 November 2026) - not_in_fetched_pages
- MISSED: ICAEW ASEAN Young Leaders Forum (6-7 November 2026) - not_in_fetched_pages
- MISSED: ICAEW Sustainability Summit (9-10 November 2026) - not_in_fetched_pages
- MISSED: Digital Assets Conference (13 November 2026) - not_in_fetched_pages
- MISSED: Forensic & Expert Witness Conference (18 November 2026) - not_in_fetched_pages
- MISSED: AI & Analytics Conference (26 November 2026) - not_in_fetched_pages
- MISSED: Public Sector Conference (4 December 2026) - not_in_fetched_pages
- MISSED: Charity Conference (21 & 22 January 2027) - not_in_fetched_pages
- extra: NED series 2025-2026 | 
- extra: Europe: The Bigger Picture webinar series - Career moves, global work and busine | 
- extra: Use cases of proprietary AI tools in M&A | 
- extra: What’s new in business tech | 
- extra: Hot topics and tips for 2025/26 audits | 
- extra: A practical guide to using Dynamic Arrays in Excel | 
- ... and 18 more extras

### icsi

- **SILENTLY EMPTY**: returned nothing and reported no problem.

### isca-sg

- MISSED: ISCA Dinner 2026 (3 July 2026) - no_repeated_card_group
- MISSED: AccountanCity 2026 (17 & 18 July 2026) - no_repeated_card_group
- MISSED: Tech & AI Fair (28 August 2026) - no_repeated_card_group
- MISSED: ISCA Run 2026 (17 Oct 2026) - no_repeated_card_group
- MISSED: ISCA Conference 2026 (11 Nov 2026) - no_repeated_card_group
- extra: Foundation Programme | 
- extra: Professional Programme | 
- extra: Accredited Training Organisations | 
- extra: Corporate Membership | 
- extra: Individual Membership | 
- extra: Membership Renewal | 
- ... and 14 more extras

### picpa-dubai

- extra: PPO-UAE 2026 Leaders’ Night & Induction Ceremony | 15-Feb-2026
- extra: Cryptocurrency 101 – Practical Session: Trading Demonstration and Virtual Asset  | 07-Mar-2026
- extra: PPO UAE: Info-Session on SPLE 2026 | 07-Mar-2026
- extra: FREE Medical Mission at SLMC | 21-Mar-2026
- extra: PPO UAE : Personal Finance Masterclass | 28-Mar-2026
- extra: Finance Governance Essentials: Building Effective Policy Frameworks | 25-Apr-2026
- ... and 6 more extras

### taxation-society-uae

- extra: UAE E-INVOICING LIVE DEMONSTRATION & NETWORKING BREAKFAST | 
- extra: CONNECT. COMPLY. CAPITALIZE. CAPITALIZE. CAPITALIZE. AI • ESG • TAX • E-INVOICIN | 
- extra: FUTURE-READY UAE TAX & COMPLIANCE INTEGRATING UAE CT, TP, VAT & E-INVOICING, INT | 
- extra: 3 MONTH MASTERCLASS ON IFRS-FINAL SESSION ON 20 JUNE | 
- extra: 3 MONTHS MASTERCLASS ON IFRS- 13TH JUNE 2026 | 
- extra: UAE CORPORATE TAX – 3rd Anniversary | 
- ... and 92 more extras

### taxinstitute

- MISSED: National GST Conference (15 - 16 October 2026) - generic_title_discarded
- MISSED: National Superannuation Conference (22 - 23 October 2026) - generic_title_discarded
- MISSED: Death & Taxes Conference (28 - 29 October 2026) - generic_title_discarded
- MISSED: National Resources Tax Conference (12-13 November 2026) - generic_title_discarded
- MISSED: SA Tax Forum (4 - 5 March 2026) - generic_title_discarded
- MISSED: WA Tax Forum (11 - 12 March 2027) - generic_title_discarded
- MISSED: Financial Services Taxation Conference (11 - 12 March 2027) - generic_title_discarded
- MISSED: Noosa Tax Convention (18 - 20 November 2026) - in_page_but_not_extracted
- MISSED: Private Business Tax Retreat (25 - 26 February 2027) - in_page_but_not_extracted
- MISSED: Tax in Practice Part 2: Bendel - After the dust settles () - card_group_low_score
- MISSED: Tax in Practice - Supercharge Strategies - Division 296 () - card_group_low_score
- extra: Interested in raising your company's profile? | 
