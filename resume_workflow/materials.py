import difflib
from pathlib import Path
from . import storage as s

def rel(name):
    if name in ('背景', '兴趣', '偏好'):
        return f'主资料/{name}.md'
    parts = name.split('/')
    if len(parts) == 3 and parts[0] == '岗位' and parts[2] == '备注':
        from .jobs import business_id
        business_id(parts[1])
        return name + '.md'
    raise ValueError('material name must be 背景/兴趣/偏好 or 岗位/<id>/备注')

def version(root, name, revision):
    return s.path(root, f'历史/主资料/{name}/{revision}/内容.md').read_text(encoding='utf8')

def initialize(root, name, text):
    with s.lock(root):
        p = s.path(root, rel(name))
        if p.exists():
            raise FileExistsError(p)
        _validate(text, p)
        s.commit(root, {rel(name): text.encode('utf8')})
    return read_material(root, name)

def _validate(text, p):
    if not text.strip() or '\x00' in text:
        raise ValueError(str(p) + ': Markdown text is empty or contains NUL')


def _index(name, revision, text):
    paragraphs = []
    heading = ''
    block = []
    start = 1
    for number, line in enumerate(text.splitlines() + [''], 1):
        if line.startswith('#'):
            heading = line.strip()
        if line.strip():
            if not block:
                start = number
            block.append(line)
        elif block:
            paragraphs.append({'heading': heading, 'line': start, 'text': '\n'.join(block)})
            block = []
    return {'name': name, 'revision': revision, 'path': rel(name), 'paragraphs': paragraphs}


def _accepted(name, text, revision=None):
    revision = revision or s.uid()
    row = {'name': name, 'revision': revision, 'path': rel(name)}
    changes = {
        f'历史/主资料/{name}/{revision}/内容.md': text.encode('utf8'),
        f'历史/主资料/{name}/current.json': s.json_bytes(row),
        f'索引/资料/{name}.json': s.json_bytes(_index(name, revision, text)),
    }
    return row, changes

def _read(root, name):
    p = s.path(root, rel(name))
    raw = p.read_bytes()
    try:
        text = raw.decode('utf-8-sig').replace('\r\n', '\n')
        _validate(text, p)
    except (UnicodeError, ValueError) as exc:
        invalid = f'历史/主资料/{name}/invalid-{s.uid()}/原件.bin'
        s.commit(root, {invalid: raw})
        raise ValueError(f'{p}: Markdown/UTF-8: {exc}') from exc
    head_rel = f'历史/主资料/{name}/current.json'
    head = s.path(root, head_rel)
    old = s.read_json(head) if head.exists() else None
    if old and version(root, name, old['revision']) == text:
        index_path = s.path(root, f'索引/资料/{name}.json')
        index_bytes = s.json_bytes(_index(name, old['revision'], text))
        if s.raw(index_path) != index_bytes:
            s.commit(root, {f'索引/资料/{name}.json': index_bytes})
        return {**old, 'text': text}
    result, changes = _accepted(name, text)
    s.commit(root, changes)
    return {**result, 'text': text}

def read_material(root, name):
    with s.lock(root):
        return _read(root, name)

def merge(base, current, proposed):
    if current == base:
        return proposed
    if proposed == base or current == proposed:
        return current
    a = base.splitlines(keepends=True)

    def edits(value):
        b = value.splitlines(keepends=True)
        return [(i, j, b[x:y]) for tag, i, j, x, y in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if tag != 'equal']
    left, right = (edits(current), edits(proposed))
    for i, j, value in left:
        for x, y, other in right:
            if (i, j, value) == (x, y, other):
                continue
            if max(i, x) < min(j, y) or (i == j and x <= i <= y) or (x == y and i <= x <= j):
                return None
    changes = left + [e for e in right if e not in left]
    for i, j, value in sorted(changes, reverse=True):
        a[i:j] = value
    return ''.join(a)

def write_material(root, name, base_revision, proposed_text):
    with s.lock(root):
        return _write(root, name, base_revision, proposed_text)


def _write(root, name, base_revision, proposed_text, on_saved=None):
    """Caller holds lock; supplements add their result to this same save."""
    _validate(proposed_text, s.path(root, rel(name)))
    base = version(root, name, base_revision)
    current = _read(root, name)
    merged = merge(base, current['text'], proposed_text)
    if merged is None:
        return _conflict(root, name, base, proposed_text, current['revision'])
    before = s.path(root, rel(name)).read_bytes()
    if before.decode('utf-8-sig').replace('\r\n', '\n') != current['text']:
        return _conflict(root, name, base, proposed_text, current['revision'])
    row, accepted = _accepted(name, merged, current['revision'] if merged == current['text'] else None)
    result = {'status': 'saved', **row, 'text': merged}
    changes = {rel(name): merged.encode('utf8'), **accepted}
    if on_saved is not None:
        changes.update(on_saved(result))
    try:
        s.commit(root, changes, {rel(name): before})
    except s.Conflict:
        return _conflict(root, name, base, proposed_text, current['revision'])
    return result


def search(root, query, names=None):
    """Literal retrieval, no relevance scores or job-fit judgments."""
    names = names if names is not None else [p.stem for p in s.path(root, '主资料').glob('*.md')]
    terms = query.casefold().split()
    if not terms:
        raise ValueError('nonempty retrieval query required')
    result = []
    with s.lock(root):
        for name in names:
            current = _read(root, name)
            index = _index(name, current['revision'], current['text'])
            for paragraph in index['paragraphs']:
                if all(term in paragraph['text'].casefold() for term in terms):
                    result.append({'name': name, 'path': rel(name), 'revision': current['revision'], **paragraph})
    return result


def reindex(root, names=None):
    names = names if names is not None else [p.stem for p in s.path(root, '主资料').glob('*.md')]
    with s.lock(root):
        return [_read(root, name)['revision'] for name in names]

def _conflict(root, name, base, proposed_text, revision):
    folder = f'历史/主资料/{name}/conflict-{s.uid()}'
    current = s.path(root, rel(name)).read_bytes()
    for filename, data in [('base.md', base.encode('utf8')), ('current.md', current), ('proposed.md', proposed_text.encode('utf8'))]:
        s.atomic(s.path(root, f'{folder}/{filename}'), data)
    return {'status': 'conflict', 'conflict': folder, 'current_revision': revision}
