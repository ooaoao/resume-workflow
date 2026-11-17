"""Persist model-authored screening, deterministic logic and scoped dependencies."""
from datetime import datetime, timezone
from time import perf_counter
from . import storage as s, evidence, jobs
from .qualification_logic import evaluate_logic, logic_refs

STAGES = ('qualification', 'matching', 'deep', 'research')
SCORE_KEYS = {'score', 'total_score', 'weighted_score', 'fit_score', '总分', '加权分'}


def _no_scores(value):
    if isinstance(value, dict):
        if SCORE_KEYS.intersection(value):
            raise ValueError('cross-job/keyword scores are not a screening judgment')
        for child in value.values():
            _no_scores(child)
    elif isinstance(value, list):
        for child in value:
            _no_scores(child)


def _path(identity):
    return f'筛选/{jobs.business_id(identity)}/记录.json'


def load(root, identity):
    return s.read_json(s.path(root, _path(identity)))


def create(root, identity, ids):
    with s.lock(root):
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate scoped job')
        for job_id in ids:
            jobs.open_job(root, job_id)
        if s.path(root, _path(identity)).exists():
            raise FileExistsError(identity)
        row = {'id': identity, 'jobs': ids, 'events': [], 'comparisons': [], 'selections': []}
        s.commit(root, {_path(identity): s.json_bytes(row)})
        return row


def _qualify(root, item):
    rows = item['requirements']
    if not rows:
        raise ValueError('declare sourced requirements, including explicit not-applicable clauses')
    by_id = {}
    refs = []
    for row in rows:
        key = row['requirement_id']
        if key in by_id or not row.get('reason'):
            raise ValueError('duplicate requirement or missing semantic reason')
        if row['requirement_type'] not in ('mandatory', 'priority', 'ambiguous'):
            raise ValueError('requirement type must distinguish mandatory/priority/ambiguous')
        if row['applicability'] not in ('applicable', 'not_applicable', 'unknown'):
            raise ValueError('unknown requirement applicability')
        if row['candidate_status'] not in ('confirmed_met', 'confirmed_not_met', 'unknown'):
            raise ValueError('candidate status is not a three-state judgment')
        if row.get('scope') == 'open_related' and row['candidate_status'] == 'confirmed_not_met':
            raise ValueError('open related fields cannot fail merely because not listed')
        if not row['jd_ref'].get('quote'):
            raise ValueError('requirement needs actual JD/source quote')
        refs.append(row['jd_ref'])
        if row['candidate_status'] != 'unknown':
            if not row.get('fact_ref', {}).get('quote'):
                raise ValueError('confirmed candidate judgment needs a fact quote')
            refs.append(row['fact_ref'])
        by_id[key] = row
    logic = item['logic']
    declared = logic_refs(logic)
    if not declared.issubset(by_id):
        raise ValueError('logic references missing requirement')
    coverage = item['coverage']
    if coverage not in ('complete', 'partial', 'decisive'):
        raise ValueError('qualification coverage: complete/partial/decisive')
    if coverage == 'complete' and declared != {key for key, row in by_id.items() if row['requirement_type'] != 'priority'}:
        raise ValueError('complete logic must cover all declared hard/ambiguous clauses')
    local = evaluate_logic(logic, by_id)
    gate = local
    if coverage == 'complete':
        # This is a program input, not an optional author citation: completeness
        # determines whether an otherwise complete set may establish the gate.
        refs.append({'kind': 'job', 'id': item['job_id'], 'fields': ['jd_status']})
    if coverage == 'partial' or (coverage != 'decisive' and jobs.open_job(root, item['job_id'])['jd_status'] != '全文'):
        gate = 'UNCERTAIN'
    elif gate != 'FAIL' and coverage != 'complete':
        gate = 'UNCERTAIN'
    return gate, refs, local


def record(root, identity, spec):
    _no_scores(spec)
    if spec['stage'] not in STAGES or not spec.get('actor'):
        raise ValueError('explicit stage and semantic author required')
    started = datetime.now(timezone.utc).isoformat()
    clock = perf_counter()
    with s.lock(root):
        case = load(root, identity)
        cache = {}
        added = []
        for item in spec['items']:
            if item['job_id'] not in case['jobs']:
                raise ValueError('assessment outside declared scope')
            refs = list(item.get('dependencies', []))
            row = {**item, 'id': s.uid(), 'stage': spec['stage'], 'actor': spec['actor']}
            row['basis'] = item.get('basis', spec.get('basis', 'current'))
            if row['basis'] not in ('current', 'historical'):
                raise ValueError('assessment basis: current/historical')
            row['current_applicability'] = 'NOT_REASSESSED' if row['basis'] == 'historical' else 'authored against cited inputs'
            if spec['stage'] == 'qualification':
                row['gate'], qualification_refs, row['logic_result'] = _qualify(root, item)
                refs += qualification_refs
            elif not item.get('judgment'):
                raise ValueError('model-authored judgment/reasons required')
            if not any(ref['kind'] == 'job' and ref['id'] == item['job_id'] for ref in refs):
                refs.append({'kind': 'job', 'id': item['job_id']})
            row['evidence'] = evidence.capture(root, refs, cache)
            added.append(row)
        elapsed = perf_counter() - clock
        timing = {'started': started, 'ended': datetime.now(timezone.utc).isoformat(),
                  'persistence_seconds': elapsed, 'meaning': 'program time only; model/wait/retry time not inferred',
                  'author_timing': spec.get('timing')}
        for row in added:
            row['timing'] = timing
        case['events'].extend(added)
        s.commit(root, {_path(identity): s.json_bytes(case)})
        return added


def compare(root, identity, spec):
    _no_scores(spec)
    if not spec.get('actor') or not spec.get('reason'):
        raise ValueError('comparison needs actual author/reason')
    with s.lock(root):
        case = load(root, identity)
        members = [job for group in spec['groups'].values() for job in group]
        if len(members) != len(set(members)) or not set(members).issubset(case['jobs']):
            raise ValueError('comparison groups must be disjoint and within scope')
        event_ids = {event['id'] for event in case['events']}
        if not set(spec.get('assessment_ids', [])).issubset(event_ids):
            raise ValueError('comparison assessment reference missing')
        refs = list(spec.get('dependencies', []))
        for quota in spec.get('quotas', []):
            if quota['limit'] != 'UNKNOWN' and (type(quota['limit']) is not int or quota['limit'] < 0):
                raise ValueError('quota limit must be a sourced integer or UNKNOWN')
            if not set(quota['jobs']).issubset(case['jobs']):
                raise ValueError('quota scope outside case')
            refs.append(quota['source'])
        row = {**spec, 'id': s.uid(), 'evidence': evidence.capture(root, refs), 'boundary': 'model comparison; not submission authority'}
        case['comparisons'].append(row)
        s.commit(root, {_path(identity): s.json_bytes(case)})
        return row


def select(root, identity, ids, mode, comparison_id=None, metadata=None):
    if mode not in ('parallel', 'ordered'):
        raise ValueError('selection must preserve parallel/ordered meaning')
    with s.lock(root):
        case = load(root, identity)
        if len(ids) != len(set(ids)) or not set(ids).issubset(case['jobs']):
            raise ValueError('selection outside case or duplicated')
        checks = []
        selection_refs = list((metadata or {}).get('dependencies', []))
        selection_refs += [{'kind': 'job', 'id': job_id, 'fields': ['availability', 'jd_status']} for job_id in ids]
        if comparison_id:
            comparison = next(c for c in case['comparisons'] if c['id'] == comparison_id)
            for quota in comparison.get('quotas', []):
                count = len(set(ids).intersection(quota['jobs']))
                limit = quota['limit']
                count_state = 'UNKNOWN' if limit == 'UNKNOWN' else 'WITHIN' if count <= limit else 'EXCEEDED'
                occupied = quota.get('occupied', 'UNKNOWN')
                applicability = quota.get('applicability', 'UNKNOWN')
                if occupied != 'UNKNOWN' and (type(occupied) is not int or occupied < 0):
                    raise ValueError('occupied must be a factual nonnegative count or UNKNOWN')
                available = 'UNKNOWN' if limit == 'UNKNOWN' or occupied == 'UNKNOWN' else max(0, limit - occupied)
                state = 'UNKNOWN' if available == 'UNKNOWN' or applicability != 'confirmed' else 'WITHIN' if count <= available else 'EXCEEDED'
                checks.append({'name': quota['name'], 'selected': count, 'limit': limit, 'count_status': count_state,
                               'occupied': occupied, 'applicability': applicability, 'available_quota': available, 'status': state})
        row = {'id': s.uid(), 'jobs': ids, 'mode': mode, 'comparison_id': comparison_id,
               'quota_checks': checks, 'submission_status': 'NOT_ASSESSED',
               'role': (metadata or {}).get('role', 'user-selection'),
               'decision_text': (metadata or {}).get('decision_text'),
               'evidence': evidence.capture(root, selection_refs)}
        case['selections'].append(row)
        s.commit(root, {_path(identity): s.json_bytes(case)})
        return row


def status(root, identity):
    with s.lock(root):
        case = load(root, identity)
        cache = {}
        latest = {}
        for event in case['events']:
            latest[(event['job_id'], event['stage'])] = event['id']
        active = set(latest.values())
        states = {}
        for event in case['events']:
            reasons = evidence.changes(root, event['evidence'], cache)
            if event['stage'] == 'qualification' and event.get('coverage') == 'complete':
                captured = any(entry['ref']['kind'] == 'job'
                               and entry['ref']['id'] == event['job_id']
                               and 'jd_status' in entry['ref'].get('fields', [])
                               for entry in event['evidence'])
                if not captured:
                    reasons.append({'job': event['job_id'], 'reason': 'stored qualification lacks program JD-completeness evidence; targeted reassessment required'})
            states[event['id']] = reasons
        comparisons = []
        for row in case['comparisons']:
            reasons = evidence.changes(root, row['evidence'], cache)
            for event_id in row.get('assessment_ids', []):
                if states[event_id] or event_id not in active:
                    reasons.append({'assessment': event_id, 'reason': 'changed or superseded assessment'})
            comparisons.append({**row, 'needs_review': reasons})
        by_comparison = {row['id']: row for row in comparisons}
        choices = []
        for row in case['selections']:
            reasons = evidence.changes(root, row.get('evidence', []), cache)
            if row['comparison_id']:
                reasons += by_comparison[row['comparison_id']]['needs_review']
            for key, event_id in latest.items():
                if key[0] in row['jobs'] and states[event_id]:
                    reasons = reasons + [{'assessment': event_id, 'reason': 'selected job evidence changed'}]
            choices.append({**row, 'needs_review': reasons})
        assessed = {event['job_id'] for event in case['events']}
        historical_labels = {}
        for event in case['events']:
            label = event.get('judgment', {}).get('tier')
            if event['id'] in active and event['basis'] == 'historical' and label:
                historical_labels[label] = historical_labels.get(label, 0) + 1
        return {**case, 'events': [{**row, 'needs_review': states[row['id']], 'current': row['id'] in active} for row in case['events']],
                'comparisons': comparisons, 'selections': choices,
                'historical_labels': historical_labels,
                'coverage': {'total': len(case['jobs']), 'assessed': len(assessed),
                             'unassessed': [j for j in case['jobs'] if j not in assessed]}}


def brief(root, identity, material_names=None):
    """Read current scoped inputs once; equal JD bodies share a document slot."""
    with s.lock(root):
        case = load(root, identity)
        documents = []
        targets = []
        for job_id in case['jobs']:
            row = jobs.open_job(root, job_id)
            if row['jd'] is None:
                targets.append({'id': job_id, 'document': None, 'metadata': row})
                continue
            text = s.path(root, row['jd']).read_text(encoding='utf-8-sig')
            if text not in documents:
                documents.append(text)
            targets.append({'id': job_id, 'document': documents.index(text), 'metadata': row})
        from . import materials
        profile = [materials._read(root, name) for name in (material_names or [])]
        return {'case': identity, 'targets': targets, 'documents': documents, 'materials': profile,
                'instruction': 'Author semantic judgments with quotes; preserve unknowns/alternatives; no unified score.'}
