"""Explicit original intake, preserving legacy meaning rather than old authority."""
from pathlib import Path
from datetime import datetime, timezone
from . import storage as s

def runtime_path(path):
    return any(part in ('__pycache__', '_mechanism', '_cache', 'memory-bank', 'node_modules', 'chrome-print-profile')
               or part.startswith(('browser-profile', 'chrome-profile', '.browser-')) for part in Path(path).parts)


def _tree_entries(root, directory, *, skip_runtime=False):
    root = Path(root).absolute()
    directory = Path(directory).absolute()
    relative = directory.relative_to(root) if directory.is_relative_to(root) else None
    if skip_runtime and relative is not None and runtime_path(relative):
        return
    with s.scandir(directory) as scan:
        children = []
        for entry in sorted(scan, key=lambda item: item.name):
            child = directory / entry.name
            relative = child.relative_to(root) if child.is_relative_to(root) else None
            if skip_runtime and relative is not None and runtime_path(relative):
                continue
            metadata = entry.stat(follow_symlinks=False)
            is_directory = not s.is_linked(metadata) and entry.is_dir(follow_symlinks=False)
            children.append((child, is_directory))
    for child, is_real_directory in children:
        yield child, is_real_directory
        if is_real_directory:
            yield from _tree_entries(root, child, skip_runtime=skip_runtime)


def source_records(root):
    """Read first-package relations plus immutable per-object intake records."""
    index = s.path(root, '来源/来源关系.json')
    rows = s.read_json(index) if s.exists(index) else []
    rows.extend(s.read_json(p) for p in s.path(root, '来源/关系').glob('*/*.json'))
    return rows


def register_local(root, sources, *, skip_runtime=False):
    """Register existing root-local originals without copying their contents."""
    root = Path(root).absolute()
    rows = []
    with s.lock(root):
        existing = {(row['path'], row['legacy_id'], row['role']): row
                    for row in source_records(root) if row.get('mode') == 'in-place'}
        for source in sources:
            directory = Path(source['path']).absolute()
            if not directory.is_relative_to(root):
                raise ValueError('in-place originals must be within the data root')
            relative_directory = directory.relative_to(root)
            if skip_runtime and runtime_path(relative_directory):
                continue
            directory = s.path(root, relative_directory.as_posix())
            files = _tree_entries(root, directory, skip_runtime=skip_runtime) if s.is_dir(directory) else [(directory, False)]
            for file, is_directory in files:
                relative = file.relative_to(root).as_posix()
                if skip_runtime and runtime_path(Path(relative)):
                    continue
                if is_directory:
                    continue
                file = s.path(root, relative)
                stamp = s.stat_path(file)
                key = (relative, source.get('id'), source.get('role', 'original'))
                if key in existing:
                    rows.append(existing[key])
                    continue
                identity = s.uid()
                relation = f'来源/关系/{identity[:2]}/{identity}.json'
                row = {'path': relative, 'original': str(file), 'legacy_id': source.get('id'),
                       'role': key[2], 'identity': identity, 'mode': 'in-place',
                       'source_stat': {'size': stamp.st_size, 'mtime_ns': stamp.st_mtime_ns},
                       'recorded_at': datetime.now(timezone.utc).isoformat(),
                       'state': 'complete', 'relation': relation,
                       'meaning': 'registered existing original; not an independent backup'}
                s.commit(root, {relation: s.json_bytes(row)})
                existing[key] = row
                rows.append(row)
    return rows


def import_legacy(root, sources, *, reuse_existing=False, skip_runtime=False):
    rows = []
    with s.lock(root):
        existing = source_records(root)
        by_key = {}
        for row in existing:
            key = (row['original'], row['legacy_id'], row['role'])
            if key not in by_key or row.get('recorded_at', '') > by_key[key].get('recorded_at', ''):
                by_key[key] = row
        for source in sources:
            p = Path(source['path']).absolute()
            if skip_runtime and runtime_path(Path(p.name)):
                continue
            s.path(p.parent, p.name)
            source_is_directory = s.is_dir(p)
            files = (_tree_entries(p, p, skip_runtime=skip_runtime)
                     if source_is_directory else [(p, False)])
            identity = s.uid()
            for f, is_entry_directory in files:
                if is_entry_directory:
                    continue
                if skip_runtime and runtime_path(f.relative_to(p)):
                    continue
                s.path(f.parent, f.name)
                relative = str(f.relative_to(p)) if source_is_directory else f.name
                dest = f'来源/{identity}/{relative}'
                member_identity = identity
                stamp = s.stat_path(f)
                if reuse_existing:
                    key = (str(f), source.get('id'), source.get('role', 'original'))
                    if key in by_key:
                        match = by_key[key]
                        accepted = s.path(root, match['path'])
                        signature = {'size': stamp.st_size, 'mtime_ns': stamp.st_mtime_ns}
                        if match.get('state', 'complete') == 'complete' and match.get('source_stat') == signature and s.is_file(accepted) and s.stat_path(accepted).st_size == stamp.st_size:
                            rows.append(match)
                            continue
                        if match.get('state', 'complete') == 'complete':
                            data = s.read_bytes(f)
                            if s.read_bytes(accepted) != data:
                                raise ValueError('existing batch source content conflict: ' + str(f))
                            rows.append(match)
                            continue
                        dest = match['path']
                        member_identity = match['identity']
                # Only one source object's bytes are resident/staged per commit.
                data = s.read_bytes(f)
                after = s.stat_path(f)
                if (after.st_size, after.st_mtime_ns) != (stamp.st_size, stamp.st_mtime_ns):
                    raise s.Conflict('source changed during intake: ' + str(f))
                row = {'path': dest, 'original': str(f), 'legacy_id': source.get('id'), 'role': source.get('role', 'original'), 'inherited_review_scope': source.get('review_scope'), 'identity': member_identity,
                       'source_stat': {'size': stamp.st_size, 'mtime_ns': stamp.st_mtime_ns},
                       'recorded_at': datetime.now(timezone.utc).isoformat(), 'state': 'written-unverified'}
                if reuse_existing and key in by_key and by_key[key].get('state') == 'written-unverified':
                    relation = by_key[key]['relation']
                else:
                    record_id = s.uid()
                    relation = f'来源/关系/{record_id[:2]}/{record_id}.json'
                row['relation'] = relation
                s.commit(root, {dest: data, relation: s.json_bytes(row)})
                if s.read_bytes(s.path(root, dest)) != data:
                    raise OSError('source copy differs: ' + dest)
                row['state'] = 'complete'
                s.commit(root, {relation: s.json_bytes(row)})
                rows.append(row)
                existing.append(row)
                by_key[(str(f), source.get('id'), source.get('role', 'original'))] = row
    return rows
