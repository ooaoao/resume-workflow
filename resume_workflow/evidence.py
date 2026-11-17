"""Capture the actual cited inputs; semantic interpretation stays with its author."""
import re
from . import storage as s, materials


def section(text, heading):
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.strip() == heading]
    if len(starts) != 1:
        raise ValueError('section missing or ambiguous: ' + heading)
    start = starts[0]
    level = len(heading) - len(heading.lstrip('#'))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        match = re.match(r'^(#+)\s', lines[i])
        if match and len(match[1]) <= level:
            end = i
            break
    return ''.join(lines[start:end])


def value(root, ref, cache=None):
    cache = {} if cache is None else cache
    kind = ref['kind']
    if kind == 'material':
        key = ('material', ref['name'])
        if key not in cache:
            cache[key] = materials._read(root, ref['name'])
        current = cache[key]
        result = current['text']
    elif kind == 'source':
        key = ('source', ref['path'])
        if key not in cache:
            cache[key] = s.path(root, ref['path']).read_text(encoding='utf-8-sig')
        result = cache[key]
    elif kind == 'job':
        from . import jobs
        key = ('job', ref['id'])
        if key not in cache:
            cache[key] = jobs.open_job(root, ref['id'])
        job = cache[key]
        result = {}
        for field in ref.get('fields', ['jd']):
            if field == 'jd':
                if job['jd'] is None:
                    result[field] = None
                    continue
                key = ('source', job['jd'])
                if key not in cache:
                    cache[key] = s.path(root, job['jd']).read_text(encoding='utf-8-sig')
                result[field] = cache[key]
            else:
                item = job
                for part in field.split('.'):
                    if not isinstance(item, dict) or part not in item:
                        raise ValueError('job field missing: ' + field)
                    item = item[part]
                result[field] = item
    elif kind == 'policy':
        from .jobs import business_id
        content = s.read_json(s.path(root, f'岗位/共同条件/{business_id(ref["id"])}.json'))['content']
        result = {key: child for key, child in content.items() if key != 'evidence'}
        result['source_inputs'] = [{'ref': entry['ref'], 'value': value(root, entry['ref'], cache)} for entry in content['evidence']]
    else:
        raise ValueError('unsupported evidence kind: ' + kind)
    if 'section' in ref:
        if not isinstance(result, str):
            raise ValueError('section requires text evidence')
        result = section(result, ref['section'])
    if 'quote' in ref:
        texts = [result] if isinstance(result, str) else list(result.values())
        if not any(isinstance(text, str) and ref['quote'] in text for text in texts):
            raise ValueError('quoted evidence absent: ' + str(ref))
    return result


def capture(root, refs, cache=None):
    """Caller holds storage lock; expected is optional prior consumer evidence."""
    cache = {} if cache is None else cache
    result = []
    for ref in refs:
        current = value(root, ref, cache)
        if 'expected' in ref and current != ref['expected']:
            raise s.Conflict('evidence changed since it was read: ' + str(ref))
        entry = {'ref': {k: v for k, v in ref.items() if k != 'expected'}, 'value': current}
        if ref['kind'] == 'material':
            entry['material_revision'] = cache[('material', ref['name'])]['revision']
        result.append(entry)
    return result


def changes(root, captured, cache=None):
    result = []
    for entry in captured:
        try:
            current = value(root, entry['ref'], cache)
            if current != entry['value']:
                result.append({'ref': entry['ref'], 'reason': 'cited input changed'})
        except (OSError, ValueError) as exc:
            result.append({'ref': entry['ref'], 'reason': str(exc)})
    return result
