"""Local file transactions; recovery compares actual bytes, never release state."""
import json
import os
import stat as stat_module
import uuid
from contextlib import contextmanager
from pathlib import Path

_EXTENDED_PATH_THRESHOLD = 248
_REPARSE_POINT = 1024

class Conflict(ValueError):
    """An observed external edit prevents overwriting a file."""

def uid():
    return uuid.uuid4().hex

def _needs_extended_path(p):
    return os.name == 'nt' and len(str(p)) >= _EXTENDED_PATH_THRESHOLD

def _io_path(p):
    value = str(p)
    if not _needs_extended_path(p) or value.startswith('\\\\?\\'):
        return value
    absolute = str(Path(value).absolute())
    if absolute.startswith('\\\\'):
        return '\\\\?\\UNC\\' + absolute[2:]
    return '\\\\?\\' + absolute

def stat_path(path, *, follow_symlinks=True):
    return os.stat(_io_path(path), follow_symlinks=follow_symlinks)

def is_linked(metadata):
    return stat_module.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, 'st_file_attributes', 0) & _REPARSE_POINT)

def is_file(path):
    try:
        return stat_module.S_ISREG(stat_path(path).st_mode)
    except FileNotFoundError:
        return False

def is_dir(path):
    try:
        return stat_module.S_ISDIR(stat_path(path).st_mode)
    except FileNotFoundError:
        return False

def exists(path):
    try:
        stat_path(path)
        return True
    except FileNotFoundError:
        return False

def scandir(path):
    return os.scandir(_io_path(path))

def open_file(path, mode, **kwargs):
    if not _needs_extended_path(path):
        return Path(path).open(mode, **kwargs)
    return open(_io_path(path), mode, **kwargs)

def read_bytes(path):
    if not _needs_extended_path(path):
        return Path(path).read_bytes()
    with open_file(path, 'rb') as handle:
        return handle.read()

def read_text(path, encoding='utf-8'):
    if not _needs_extended_path(path):
        return Path(path).read_text(encoding=encoding)
    with open_file(path, 'r', encoding=encoding) as handle:
        return handle.read()

def path(root, relative):
    root = Path(root).absolute()
    member = Path(relative)
    if member.anchor or '..' in member.parts or ':' in str(relative):
        raise ValueError('relative local path required: ' + str(relative))
    q = root / member
    current = root
    candidates = [*reversed(root.parents), root]
    for part in member.parts:
        current = current / part
        candidates.append(current)
    for candidate in candidates:
        try:
            metadata = os.lstat(_io_path(candidate))
        except FileNotFoundError:
            continue
        if is_linked(metadata):
            raise ValueError('linked path forbidden: ' + str(candidate))
        if stat_module.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
            raise ValueError('hard-linked file forbidden: ' + str(candidate))
    return q

def raw(p):
    return read_bytes(p) if exists(p) else None

def json_bytes(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode('utf8')

def read_json(p):
    try:
        return json.loads(read_text(p, encoding='utf-8-sig'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'{p}: JSON/UTF-8: {exc}') from exc

def atomic(p, data):
    os.makedirs(_io_path(p.parent), exist_ok=True)
    tmp = p.with_name('.' + p.name + '.' + uid() + '.tmp')
    try:
        with open_file(tmp, 'xb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(_io_path(tmp), _io_path(p))
    finally:
        if os.path.lexists(_io_path(tmp)):
            os.unlink(_io_path(tmp))

@contextmanager
def lock(root):
    root = Path(root)
    os.makedirs(_io_path(root), exist_ok=True)
    p = path(root, '.save.lock')
    f = open_file(p, 'a+b')
    try:
        if f.tell() == 0:
            f.write(b'0')
            f.flush()
        f.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()

def prepare(root, changes, expected=None):
    """Caller holds lock. Preserve before/after bytes before any replacement."""
    pending = path(root, '.pending')
    if pending.exists() and any(pending.iterdir()):
        raise ValueError('unfinished save; run resume first')
    targets = set(changes)
    for issue in conflicts(root):
        affected = targets.intersection(row['path'] for row in issue['rows'])
        if affected:
            raise Conflict('partial save requires explicit resolution: ' + issue['id'] + '; ' + ', '.join(sorted(affected)))
    if expected is not None:
        for rel, before in expected.items():
            if raw(path(root, rel)) != before:
                raise Conflict('external edit conflict: ' + rel)
    tx = path(root, '.preparing') / uid()
    tx.mkdir(parents=True)
    rows = []
    try:
        for i, (rel, after) in enumerate(changes.items()):
            p = path(root, rel)
            before = raw(p)
            if expected is not None and rel in expected and before != expected[rel]:
                raise Conflict('external edit conflict: ' + rel)
            if before is not None:
                atomic(tx / f'{i}.before', before)
            atomic(tx / f'{i}.after', after)
            rows.append({'path': rel, 'index': i, 'existed': before is not None})
        atomic(tx / 'save.json', json_bytes(rows))
        pending.mkdir(exist_ok=True)
        ready = pending / tx.name
        os.replace(tx, ready)
        return ready
    except (OSError, ValueError) as exc:
        # No target has been touched during preparation. Retain even the proposal
        # whose staging write failed, then retire this attempt instead of blocking.
        for i, (rel, data) in enumerate(changes.items()):
            atomic(tx / 'proposed' / f'{i}.bin', data)
        planned = [{'path': rel, 'index': i} for i, rel in enumerate(changes)]
        _retire(root, tx, 'aborted-preparation', str(exc), planned)
        raise


def _retire(root, tx, state, reason, rows):
    identity = uid()
    relative = f'历史/保存冲突/{identity}'
    result = {
        'id': identity,
        'path': relative,
        'transaction': tx.name,
        'state': state,
        'reason': reason,
        'rows': rows,
        'transaction_completed': False,
    }
    atomic(tx / 'result.json', json_bytes(result))
    destination = path(root, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tx, destination)
    return result


def _retire_conflict(root, tx, rows, message):
    observations = []
    for row in rows:
        i = row['index']
        current = raw(path(root, row['path']))
        before = (tx / f'{i}.before').read_bytes() if row['existed'] else None
        after = (tx / f'{i}.after').read_bytes()
        if current == before:
            state = 'before'
        elif current == after:
            state = 'applied'
        else:
            state = 'external-edit'
        if current is not None:
            atomic(tx / f'{i}.current', current)
        observations.append({**row, 'observed': state, 'current_exists': current is not None})
    partial = any(row['observed'] == 'applied' for row in observations)
    result = _retire(root, tx, 'partial-conflict' if partial else 'aborted-conflict', message, observations)
    raise Conflict(message + '; retained at ' + result['path'])


def conflicts(root):
    """Only unresolved partial applications hold their own target paths."""
    folder = path(root, '历史/保存冲突')
    if not folder.exists():
        return []
    rows = [read_json(p) for p in folder.glob('*/result.json')]
    return [row for row in rows if row['state'] == 'partial-conflict']


def resolve_conflict(root, identity, decision):
    if decision != 'keep-current':
        raise ValueError('explicit keep-current decision required; original transaction is not completed')
    with lock(root):
        relative = f'历史/保存冲突/{identity}'
        folder = path(root, relative)
        result = read_json(folder / 'result.json')
        if result['state'] != 'partial-conflict':
            raise ValueError('only unresolved partial saves require this decision')
        for row in result['rows']:
            current = raw(path(root, row['path']))
            if current is not None:
                atomic(folder / 'resolution' / f'{row["index"]}.current', current)
        result['state'] = 'resolved-keep-current'
        result['transaction_completed'] = False
        atomic(folder / 'result.json', json_bytes(result))
        return result

def finish(root, tx):
    import shutil
    rows = read_json(tx / 'save.json')
    for row in rows:
        i = row['index']
        p = path(root, row['path'])
        before = (tx / f'{i}.before').read_bytes() if row['existed'] else None
        after = (tx / f'{i}.after').read_bytes()
        if raw(p) not in (before, after):
            _retire_conflict(root, tx, rows, 'unfinished save conflict: ' + str(p))
    for row in rows:
        i = row['index']
        p = path(root, row['path'])
        before = (tx / f'{i}.before').read_bytes() if row['existed'] else None
        after = (tx / f'{i}.after').read_bytes()
        current = raw(p)
        if current == after:
            continue
        if current != before:
            _retire_conflict(root, tx, rows, 'external edit conflict: ' + str(p))
        atomic(p, after)
    shutil.rmtree(tx)

def commit(root, changes, expected=None):
    finish(root, prepare(root, changes, expected))

def resume(root):
    with lock(root):
        pending = path(root, '.pending')
        recovered = []
        aborted = []
        preparing = path(root, '.preparing')
        if preparing.exists():
            for tx in preparing.iterdir():
                aborted.append(_retire(root, tx, 'aborted-preparation', 'preparation never entered application queue', []))
        if pending.exists():
            for tx in pending.iterdir():
                if not (tx / 'save.json').exists():
                    aborted.append(_retire(root, tx, 'aborted-preparation', 'incomplete preparation; no application header', []))
                    continue
                finish(root, tx)
                recovered.append(tx.name)
        unresolved = conflicts(root)
        return {'recovered': recovered, 'aborted': aborted, 'conflicts': unresolved, 'status': 'conflict' if unresolved else 'ready'}
