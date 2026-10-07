"""Markdown rendering of a measurement run."""

from __future__ import annotations


def _pct(v):
    return "n/a" if v is None else f"{v:.1f}%"


def render_markdown(payload: dict) -> str:
    agg, orgs = payload["aggregate"], payload["orgs"]
    L = [f"# Discovery measurement - engine `{payload['engine']}`, mode `{payload['mode']}`", ""]
    L += [f"**Discovery recall: {_pct(agg['recall_pct'])}** ({agg['matched_valid']} of {agg['true_events']} manually verified events found "
          f"across {agg['orgs']} organisations)", ""]
    L += ["| Measure | Value |", "|---|---|",
          f"| Candidates returned | {agg['discovered_total']} |",
          f"| Missed valid events | {agg['missed']} |",
          f"| Duplicate candidates | {agg['duplicates']} |",
          f"| Extras (matched nothing in baseline) | {agg['extras']} |",
          f"| ...of which already past (archive leakage) | {agg['past_extras']} |",
          f"| Orgs silently empty (no events, no error reported) | {agg['silent_empty_orgs']} |",
          f"| Completeness state correct | {agg['states_correct']} / {agg['states_checked']} |",
          f"| Matched but missing title/link | {agg['extraction_failures']} |",
          f"| Orgs with errors | {agg['orgs_with_errors']} |", ""]
    if agg["miss_reasons"]:
        L += ["## Why events were missed", "", "| Reason | Events |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in sorted(agg["miss_reasons"].items(), key=lambda kv: -kv[1])]
        L.append("")
    if agg["drop_reasons"]:
        L += ["## Dropped candidates (reason recorded by the engine)", "", "| Reason | Count |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in sorted(agg["drop_reasons"].items(), key=lambda kv: -kv[1])]
        L.append("")
    if agg["failure_kinds"]:
        L += ["## Failure kinds (orgs affected)", "", "| Kind | Orgs |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in sorted(agg["failure_kinds"].items())]
        L.append("")
    L += ["## Per organisation", "",
          "| Org | True | Found | Matched | Recall | Missed | Dup | Extra (past) | State | Expected | Pages | Sec |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for o in orgs:
        L.append(f"| {o['slug']} | {o['true_count']} | {o['discovered_count']} | {o['matched_valid']} | {_pct(o['recall'])} | "
                 f"{len(o['missed'])} | {len(o['duplicates'])} | {len(o['extras'])} ({o['past_extras']}) | "
                 f"{o['state_reported'] or '-'} | {o['state_expected'] or '-'} | {o['pages_walked']} | {o['seconds']} |")
    L.append("")
    for o in orgs:
        if o["missed"] or o["extras"] or o["errors"] or o["silently_empty"]:
            L += [f"### {o['slug']}", ""]
            if o["silently_empty"]:
                L.append("- **SILENTLY EMPTY**: returned nothing and reported no problem.")
            for e in o["errors"]:
                L.append(f"- error: {e}")
            for m in o["missed"][:12]:
                L.append(f"- MISSED: {m['title']} ({m.get('date_text', '')}) - {m.get('reason', '')}")
            if len(o["missed"]) > 12:
                L.append(f"- ... and {len(o['missed']) - 12} more missed")
            for x in o["extras"][:6]:
                L.append(f"- extra: {x['title'][:80]} | {x.get('date_hint', '')}")
            if len(o["extras"]) > 6:
                L.append(f"- ... and {len(o['extras']) - 6} more extras")
            L.append("")
    return "\n".join(L)
