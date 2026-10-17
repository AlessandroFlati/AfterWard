"""Literal source checks and contrary-fact audit promoted from the v7 research pipeline."""
import re


def source_index(packet):
    from carerelay.evidence import cite
    result = {}
    for page in packet.pages:
        lines = list(re.finditer(r"[^\r\n]+", page.text))
        for index, line in enumerate(lines):
            if not line.group().strip():
                continue
            start, end = line.span()
            # Expand repeated lines deterministically; never select an arbitrary occurrence.
            for radius in range(len(lines)):
                left = lines[max(0, index - radius)].start()
                right = lines[min(len(lines) - 1, index + radius)].end()
                quote = page.text[left:right]
                if len(quote) > 1000:
                    break
                try:
                    citation = cite(packet, page.number, quote)
                except ValueError:
                    continue
                result[f"p{page.number}l{index + 1}"] = {
                    "page": page.number, "text": line.group(), "start": start, "end": end,
                    "quote": citation.quote}
                break
    return result

def indexed_pages(packet):
    index = source_index(packet)
    return [{"page": page.number, "text": page.text,
        "source_lines": [{"source_id": key, "text": value["text"]}
            for key, value in index.items() if value["page"] == page.number]}
        for page in packet.pages]

FIELD_FOR_GAP = {
    "missing_amount": "amount", "missing_frequency": "frequency", "missing_start": "start",
    "missing_duration": "duration", "missing_attachment": "document_content",
    "missing_contact": "recipient", "missing_appointment": "appointment",
}

EXCLUSIONS = {"different_task", "not_current", "not_a_usable_value"}

def resolve_references(references, packet):
    from carerelay.evidence import cite
    if not isinstance(references, list) or not references:
        raise ValueError("At least one source reference is required.")
    index = source_index(packet)
    result, used = [], set()
    for ref in references:
        if not isinstance(ref, dict) or set(ref) not in ({"source_id"}, {"source_start", "source_end"}):
            raise ValueError("Use a source_id or source_start/source_end, without generated quotes.")
        first, last = (ref["source_id"], ref["source_id"]) if "source_id" in ref else (ref["source_start"], ref["source_end"])
        if not isinstance(first, str) or not isinstance(last, str) or first not in index or last not in index:
            raise ValueError("Unknown source reference.")
        a, b = index[first], index[last]
        if a["page"] != b["page"] or a["start"] > b["start"]:
            raise ValueError("A source span must be ordered within one page.")
        covered = {key for key, source in index.items() if source["page"] == a["page"] and a["start"] <= source["start"] <= b["start"]}
        if covered & used:
            raise ValueError("Duplicate or overlapping references within an item.")
        used.update(covered)
        page = next(p for p in packet.pages if p.number == a["page"])
        quote = a["quote"] if first == last else page.text[a["start"]:b["end"]]
        if len(quote) > 1000:
            raise ValueError("Source span exceeds 1000 characters; split into separate facts.")
        citation = cite(packet, a["page"], quote)
        result.append({"page": citation.page, "quote": citation.quote})
    return result

def resolve_guarded_output(value, packet, stage, facts=()):
    envelope = "facts" if stage == "facts" else "findings"
    required = {envelope} if stage == "facts" else {envelope, "fact_checks"}
    if not isinstance(value, dict) or set(value) != required or not isinstance(value[envelope], list):
        raise ValueError("Invalid guarded output envelope; findings must include fact_checks, possibly empty.")
    resolved = []
    for row in value[envelope]:
        if not isinstance(row, dict):
            raise ValueError("Every output item must be an object.")
        resolved.append({**row, "citations": resolve_references(row.get("citations"), packet)})
    audit = []
    if stage == "findings":
        expected = set()
        for finding in resolved:
            code, topic = finding.get("code"), finding.get("topic")
            if not isinstance(code, str) or not isinstance(topic, str):
                raise ValueError("Every finding needs a string code and topic.")
            for number, fact in enumerate(facts):
                if fact["topic"] == topic and fact["field"] == FIELD_FOR_GAP.get(code):
                    expected.add((topic + ":" + code, number))
        checks = value["fact_checks"]
        if not isinstance(checks, list) or len(checks) > 80:
            raise ValueError("Invalid fact_checks list.")
        observed = set()
        for check in checks:
            if not isinstance(check, dict) or set(check) != {"finding_key", "fact_index", "reason", "explanation", "citations"}:
                raise ValueError("Invalid contrary-fact check fields.")
            key, number = check["finding_key"], check["fact_index"]
            if not isinstance(key, str) or type(number) is not int or (key, number) not in expected or (key, number) in observed:
                raise ValueError("Unknown, unnecessary or repeated contrary-fact check: "
                    + repr((key, number)) + ". Exact required (finding_key, fact_index) pairs: "
                    + repr(sorted(expected)) + ". Keep legitimate findings; repair their check indices.")
            if (not isinstance(check["reason"], str) or check["reason"] not in EXCLUSIONS
                    or not isinstance(check["explanation"], str) or not 12 <= len(check["explanation"]) <= 500):
                raise ValueError("Explain why this positive fact does not satisfy this task's field.")
            citations = resolve_references(check["citations"], packet)
            if len(citations) > 3:
                raise ValueError("A contrary-fact check allows at most three source spans.")
            observed.add((key, number))
            audit.append({**check, "citations": citations})
        if observed != expected:
            missing = sorted(expected - observed)
            raise ValueError("Unresolved contradiction with positive extracted facts: " + repr(missing))
    return {envelope: resolved}, audit

PAIR_FIELDS = {"amount", "frequency", "start", "duration", "appointment"}

DECISIONS = {"conflict", "same_value", "different_task", "different_interval", "explicit_replacement", "complementary", "insufficient_evidence"}

def evidence_pairs(facts):
    pairs=[]
    for a, first in enumerate(facts):
        for b in range(a+1,len(facts)):
            second=facts[b]
            if (first["field"] in PAIR_FIELDS and first["field"]==second["field"]
                    and first["topic"]==second["topic"] and first["citations"]!=second["citations"]):
                pairs.append({"pair_id":f"P{len(pairs)+1:02d}","fact_indices":[a,b],"topic":first["topic"],"field":first["field"]})
    if len(pairs)>80:
        raise ValueError("Too many evidence pairs for one bounded final review.")
    return pairs

def _explanation(row):
    if not isinstance(row.get("explanation"),str) or not 12<=len(row["explanation"])<=400:
        raise ValueError("A bounded, concrete explanation is required.")

def _covers_fact(packet, fact, citations):
    """Allow exact source spans to cover a fact collectively, skipping only whitespace."""
    text=next(p.text for p in packet.pages if p.number==fact['page'])
    if text.count(fact['quote'])!=1:return False
    start=text.index(fact['quote']);end=start+len(fact['quote']);cursor=start
    intervals=[]
    for citation in citations:
        if citation['page']!=fact['page'] or text.count(citation['quote'])!=1:continue
        left=text.index(citation['quote']);right=left+len(citation['quote'])
        if left<end and right>start:intervals.append((left,right))
    for left,right in sorted(intervals):
        if right<=cursor:continue
        if left>cursor and text[cursor:left].strip():return False
        cursor=max(cursor,right)
        if cursor>=end:return True
    return False

def resolve_final(value,packet,facts,previous):
    if not isinstance(value,dict) or set(value)!={"findings","fact_checks","pair_checks","previous_checks"}:
        raise ValueError("Final review needs findings, fact_checks, pair_checks and previous_checks.")
    resolved,fact_audit=resolve_guarded_output({k:value[k] for k in ("findings","fact_checks")},packet,"findings",facts)
    keys={r["topic"]+":"+r["code"] for r in resolved["findings"]}
    expected={p["pair_id"]:p for p in evidence_pairs(facts)}
    pair_audit=[]; seen=set()
    if not isinstance(value["pair_checks"],list): raise ValueError("pair_checks must be a list.")
    for row in value["pair_checks"]:
        if not isinstance(row,dict) or set(row)!={"pair_id","decision","explanation","citations"}:
            raise ValueError("Invalid pair check fields.")
        identifier=row["pair_id"]
        if not isinstance(identifier,str) or identifier not in expected or identifier in seen:
            raise ValueError("Unknown or repeated pair check.")
        if not isinstance(row["decision"],str) or row["decision"] not in DECISIONS:
            raise ValueError("Invalid pair decision.")
        _explanation(row); citations=resolve_references(row["citations"],packet)
        if len(citations)>4: raise ValueError("At most four pair citations.")
        pair=expected[identifier]
        for index in pair["fact_indices"]:
            if not any(_covers_fact(packet,a,citations) for a in facts[index]["citations"]):
                raise ValueError("Cite evidence from both compared facts.")
        if row["decision"]=="conflict" and pair["topic"]+":conflict" not in keys:
            raise ValueError("A declared conflict must appear in final findings.")
        seen.add(identifier); pair_audit.append({**row,"citations":citations})
    if seen!=set(expected): raise ValueError("Every evidence pair requires an explicit check.")
    required={r["topic"]+":"+r["code"] for r in previous}; seen=set(); previous_audit=[]
    if not isinstance(value["previous_checks"],list): raise ValueError("previous_checks must be a list.")
    for row in value["previous_checks"]:
        if not isinstance(row,dict) or set(row)!={"finding_key","decision","explanation","citations"}:
            raise ValueError("Invalid previous finding check fields.")
        key=row["finding_key"]
        if not isinstance(key,str) or key not in required or key in seen: raise ValueError("Unknown or repeated previous finding check.")
        if row["decision"]!=("retain" if key in keys else "remove"): raise ValueError("Previous finding disposition contradicts the final list.")
        _explanation(row); citations=resolve_references(row["citations"],packet)
        if len(citations)>4: raise ValueError("At most four previous-finding citations.")
        seen.add(key);previous_audit.append({**row,"citations":citations})
    if seen!=required: raise ValueError("Every previous finding requires an explicit disposition.")
    return resolved,{"fact_checks":fact_audit,"pair_checks":pair_audit,"previous_checks":previous_audit,
        "unresolved_pairs":[r["pair_id"] for r in pair_audit if r["decision"]=="insufficient_evidence"]}

def resolve_pair_final(value,packet,facts):
    """A conflict-only guard cannot remove findings or create new absence claims."""
    resolved,audit=resolve_final(value,packet,facts,[])
    topics={r['topic'] for r in evidence_pairs(facts)}
    if any(r['code']!='conflict' or r['topic'] not in topics for r in resolved['findings']):
        raise ValueError('This guard can only report conflicts from supplied evidence pairs.')
    declared={p['topic'] for p in evidence_pairs(facts) if any(c['pair_id']==p['pair_id'] and c['decision']=='conflict' for c in audit['pair_checks'])}
    if {r['topic'] for r in resolved['findings']}!=declared:
        raise ValueError('Every conflict needs an explicit supporting pair decision.')
    return resolved,audit
