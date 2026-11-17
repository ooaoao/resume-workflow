"""Merge explicitly selected saved新版业务 roots without changing identities."""
from pathlib import Path
from . import storage as s, recovery, legacy

DOMAINS = ('主资料', '历史', '索引', '来源', '岗位', '任务', '筛选', '业务记录', '资料补充', '交付', '迁入', '参照', '来源保存')


def import_saved(root, identity, spec):
    if spec.get('mode') == 'preserve-source':
        from .source_workspaces import preserve
        return preserve(root, identity, spec)
    if spec.get('mode', 'merge') != 'merge':
        raise ValueError('saved root mode must be merge or preserve-source')
    from .jobs import business_id
    identity = business_id(identity)
    root = Path(root).absolute()
    source = Path(spec['source_root']).absolute()
    s.path(source, '')
    if not source.is_dir() or root == source or root in source.parents or source in root.parents:
        raise ValueError('existing, disjoint saved data root required')
    kind = spec['source_kind']
    if kind not in ('real', 'synthetic') or spec['target_kind'] != kind:
        raise ValueError('real and synthetic current facts must not be merged')
    for location in (root, source):
        label = s.path(location, '资料身份.json')
        if label.exists() and s.read_json(label)['kind'] != kind:
            raise ValueError('declared data kind conflicts with the saved root identity')
    for name in ('.pending', '.preparing'):
        folder = s.path(source, name)
        if folder.exists() and any(folder.iterdir()):
            raise ValueError('finish source saves before merging')
    if s.conflicts(source) or s.path(source, '.restoring').exists():
        raise ValueError('resolve source conflicts/restore before merging')
    choices = spec.get('materials', {})
    if any(name not in ('背景', '兴趣', '偏好') or value not in ('keep', 'incoming') for name, value in choices.items()):
        raise ValueError('materials must map 背景/兴趣/偏好 to keep or incoming')
    files = []
    for domain in DOMAINS:
        for p in s.path(source, domain).rglob('*'):
            s.path(source, str(p.relative_to(source)))
            if p.is_file():
                files.append(p.relative_to(source).as_posix())
    incoming_files = set(files)
    originals = {row['path'] for row in legacy.source_records(source) if row.get('mode') == 'in-place'}
    record = f'迁入/{identity}/新版归并.json'
    if record in incoming_files:
        raise s.Conflict('choose a merge ID distinct from the incoming migration identity')
    with s.lock(root):
        recovery._ready(root)
        saved = s.path(root, record)
        if saved.exists():
            previous = s.read_json(saved)
            if previous['source_root'] != str(source) or previous['materials'] != choices:
                raise s.Conflict('merge identity/input differs; retain the original specification')
        skipped = set()
        conflicts = []
        expected_materials = {}
        for name in ('背景', '兴趣', '偏好'):
            main = f'主资料/{name}.md'
            if main not in incoming_files:
                continue
            current = s.path(root, main)
            same = current.exists() and recovery.same_file(s.path(source, main), current)
            archived = s.path(root, f'历史/归并/{identity}/existing/{name}.md')
            if (choices.get(name) == 'incoming' and archived.exists() and current.exists()
                    and not same and not recovery.same_file(archived, current)):
                raise s.Conflict('current material edited since partial merge: ' + main)
            if current.exists() and not same and name not in choices:
                conflicts.append(main)
            choose_keep = current.exists() and (choices.get(name) == 'keep' or (same and name not in choices))
            if choose_keep:
                skipped.update((main, f'历史/主资料/{name}/current.json', f'索引/资料/{name}.json'))
        transfers = [relative for relative in files if relative not in skipped]
        missing = []
        for relative in sorted(originals.difference(transfers)):
            original, target = s.path(source, relative), s.path(root, relative)
            if not original.is_file() or not target.is_file():
                missing.append(relative)
            elif not recovery.same_file(original, target):
                conflicts.append(relative)
        if missing:
            raise ValueError('source and target historical originals must already be present; affected sources: ' + str(missing))
        replaceable = {f'{prefix}/{name}{suffix}' for name, choice in choices.items() if choice == 'incoming'
                       for prefix, suffix in (('主资料', '.md'), ('历史/主资料', '/current.json'), ('索引/资料', '.json'))}
        for relative in replaceable:
            expected_materials[relative] = s.raw(s.path(root, relative))
        for relative in transfers:
            target = s.path(root, relative)
            if relative not in replaceable and target.exists() and not recovery.same_file(s.path(source, relative), target):
                conflicts.append(relative)
        if conflicts:
            return {'status': 'conflict', 'conflicts': sorted(set(conflicts)),
                    'meaning': 'no business files copied; resolve material choices, baseline or identity conflicts'}
        state = {'id': identity, 'source_root': str(source), 'materials': choices,
                 'kind': kind, 'state': 'copying saved business', 'copied': 0, 'reused': 0}
        s.commit(root, {record: s.json_bytes(state), '资料身份.json': s.json_bytes({'kind': kind})})
        # Both raw current files are retained before choosing the unique current.
        for name in ('背景', '兴趣', '偏好'):
            for label, location in (('incoming', source), ('existing', root)):
                p = s.path(location, f'主资料/{name}.md')
                archived = s.path(root, f'历史/归并/{identity}/{label}/{name}.md')
                if p.is_file() and not archived.exists():
                    s.commit(root, {archived.relative_to(root).as_posix(): p.read_bytes()},
                             {archived.relative_to(root).as_posix(): None})
        try:
            for relative in transfers:
                incoming = s.path(source, relative)
                target = s.path(root, relative)
                observed = s.raw(target)
                if relative in expected_materials and observed != expected_materials[relative]:
                    raise s.Conflict('material changed during merge: ' + relative)
                if target.exists() and recovery.same_file(incoming, target):
                    state['reused'] += 1
                    continue
                if observed is not None and relative not in replaceable:
                    raise s.Conflict('destination changed during merge: ' + relative)
                stamp = incoming.stat()
                data = incoming.read_bytes()
                after = incoming.stat()
                if (stamp.st_size, stamp.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise s.Conflict('source changed during merge: ' + relative)
                # One object at a time, through the existing save/conflict boundary.
                s.commit(root, {relative: data}, {relative: observed})
                state['copied'] += 1
            state['state'] = 'saved business merged; identities and historical meanings retained'
        except (OSError, ValueError) as exc:
            state.update(state='partial; retry the same specification', error=str(exc))
            s.commit(root, {record: s.json_bytes(state)})
            raise
        s.commit(root, {record: s.json_bytes(state)})
    return state
