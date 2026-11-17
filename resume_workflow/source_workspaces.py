"""Immutable saved origins and explicit continuations in one current workspace."""
from pathlib import Path
from . import storage as s, jobs, recovery


def catalog(root):
    rows = []
    for folder in recovery._direct_children(root, '来源保存'):
        record = s.path(root, (folder.relative_to(root) / '登记.json').as_posix())
        if s.is_file(record):
            rows.append(s.read_json(record))
    return rows


def resolve(root, handle):
    root = Path(root).absolute()
    if '::' not in str(handle):
        return root, jobs.business_id(handle), None
    parts = str(handle).split('::')
    if len(parts) != 2:
        raise ValueError('historical handle must be source::local-ID')
    source, local = (jobs.business_id(part) for part in parts)
    row = s.read_json(s.path(root, f'来源保存/{source}/登记.json'))
    data = s.path(root, row['data_path'])
    if s.read_json(s.path(root, row['data_path'] + '/.snapshot.json')) != recovery.SNAPSHOT:
        raise ValueError('completed historical source baseline required: ' + str(data))
    return data, local, row


def require_current(handle):
    if '::' in str(handle):
        raise ValueError('historical source is immutable; task continue creates current work')


def _files(root):
    return {p.relative_to(root).as_posix() for p in root.rglob('*')
            if p.is_file() and p not in (root / '.save.lock', root / '.snapshot.json')}


def preserve(root, identity, spec):
    identity = jobs.business_id(identity)
    source = Path(spec['source_root']).absolute()
    root = Path(root).absolute()
    s.path(source, '')
    kind = spec['source_kind']
    if kind not in ('real', 'synthetic') or spec['target_kind'] != kind:
        raise ValueError('real and synthetic current facts must not be merged')
    if source == root or source in root.parents or root in source.parents:
        raise ValueError('existing, disjoint saved data root required')
    for location in (root, source):
        label = s.path(location, '资料身份.json')
        if label.exists() and s.read_json(label)['kind'] != kind:
            raise ValueError('declared data kind conflicts with saved identity')
    relative = f'来源保存/{identity}/data'
    destination = s.path(root, relative)
    record = f'来源保存/{identity}/登记.json'
    with s.lock(root):
        recovery._ready(root)
        recovery._ready(source)
        saved = s.path(root, record)
        if saved.exists():
            row = s.read_json(saved)
            if row['original'] != str(source) or row['kind'] != kind:
                raise s.Conflict('historical source identity/input differs')
        if destination.exists():
            if s.read_json(s.path(root, relative + '/.snapshot.json')) != recovery.SNAPSHOT:
                raise ValueError('completed historical source baseline required')
            incoming = _files(source)
            if incoming != _files(destination) or any(
                    not recovery.same_file(s.path(source, name), s.path(destination, name))
                    for name in incoming):
                raise s.Conflict('source differs from preserved history; use a distinct source identity')
        else:
            recovery.backup(source, destination)
        if saved.exists():
            return row
        tasks = [s.read_json(s.path(destination, p.relative_to(destination).as_posix()))
                 for p in s.path(destination, '任务').glob('*/任务.json')]
        row = {'id': identity, 'kind': kind, 'original': str(source), 'data_path': relative,
               'tasks': [{'id': task['id'], 'job_id': task['job_id'],
                          'current_revision': task['current_revision']} for task in tasks],
               'state': 'preserved source; local identities and signed bytes unchanged'}
        s.commit(root, {record: s.json_bytes(row), '资料身份.json': s.json_bytes({'kind': kind})})
        return row


def baseline_missing(root, row):
    data = row['data_path']
    required = [data + '/.snapshot.json']

    def declared_dependencies(folder, declaration):
        binding = declaration.get('binding', declaration)
        for name in binding['files']:
            required.append(s.path(folder, name).relative_to(root).as_posix())
        if 'report' in declaration:
            required.append(s.path(folder, declaration['report']).relative_to(root).as_posix())

    def version_declarations(folder, delivered=False):
        for relative in ('html_binding.json', 'pdf_binding.json',
                         '审查/content-page.json', '审查/pdf.json'):
            file = s.path(folder, relative)
            if delivered and relative.startswith('审查/'):
                required.append(file.relative_to(root).as_posix())
            if file.is_file():
                declared_dependencies(folder, s.read_json(file))

    for task in row['tasks']:
        base = data + '/任务/' + jobs.business_id(task['id'])
        required.append(base + '/任务.json')
        # Reuse existing declarations for all saved versions, not just the
        # current structured input. Do not copy or invent a file manifest.
        for folder in s.path(root, base + '/版本').glob('*'):
            folder = s.path(root, folder.relative_to(root).as_posix())
            if folder.is_dir():
                version_declarations(folder)
        if task['current_revision']:
            version = base + '/版本/' + jobs.business_id(task['current_revision'])
            required.append(version + '/作品.json')
            work_file = s.path(root, version + '/作品.json')
            if work_file.is_file():
                work = s.read_json(work_file)
                if work['kind'] == 'structured':
                    required.extend((version + '/content.json', version + '/config.json'))
                    content_file = s.path(root, version + '/content.json')
                    if content_file.is_file():
                        required.extend(version + '/' + relative for relative in
                                        s.read_json(content_file).get('resources', {}).values())
                else:
                    required.append(version + '/resume.html')
                required.extend(version + '/' + source['snapshot'] for source in work['sources'])
        if task['job_id'] is not None:
            job_base = data + '/岗位/' + jobs.business_id(task['job_id'])
            required.extend((job_base + '/岗位.json', job_base + '/备注.md'))
            job_file = s.path(root, job_base + '/岗位.json')
            if job_file.is_file():
                job = s.read_json(job_file)
                if job['jd'] is not None:
                    required.append(data + '/' + job['jd'])
    for registration in s.path(root, data + '/交付').glob('*/*/登记.json'):
        registration = s.path(root, registration.relative_to(root).as_posix())
        saved = s.read_json(registration)
        original = s.path(root, data + '/任务/' + jobs.business_id(saved['task'])
                          + '/版本/' + jobs.business_id(saved['revision']))
        # A saved delivery declares the original signed object and its locked
        # copy, including the two review records and their retained reports.
        for folder in (original, registration.parent):
            declared_dependencies(folder, saved)
            version_declarations(folder, delivered=True)
    missing = [name for name in sorted(set(required)) if not s.path(root, name).is_file()]
    marker = s.path(root, data + '/.snapshot.json')
    if marker.is_file() and s.read_json(marker) != recovery.SNAPSHOT:
        missing.append(data + '/.snapshot.json')
    return missing


def describe_task(root, handle):
    from . import tasks
    data, local, row = resolve(root, handle)
    task = tasks.load(data, local)
    result = {**task, 'handle': handle, 'scope': 'historical',
              'source_data': str(data), 'path': str(s.path(data, tasks.task_path(local)))}
    mapping = s.path(root, f"来源保存/{row['id']}/接续/{local}.json")
    result['current_task'] = s.read_json(mapping)['task'] if mapping.exists() else None
    if task['current_revision']:
        folder, _ = tasks.get_revision(data, local)
        result['version_path'] = str(folder)
        result['artifacts'] = {name: str(folder / name) for name in
                               ('resume.html', 'html_preview.png', 'resume.pdf', 'pdf_preview.png')
                               if (folder / name).is_file()}
        result['reviews'] = {kind: str(folder / '审查' / (kind + '.json'))
                             for kind in ('content-page', 'pdf')
                             if (folder / '审查' / (kind + '.json')).is_file()}
    return result


def historical_tasks(root):
    return [describe_task(root, row['id'] + '::' + task['id'])
            for row in catalog(root) for task in row['tasks']]


def _current_task(root, handle, author):
    from . import tasks
    data, local, row = resolve(root, handle)
    original = tasks.load(data, local)
    record = f"来源保存/{row['id']}/接续/{local}.json"
    with s.lock(root):
        recovery._ready(root)
        if s.path(root, record).exists():
            target = s.read_json(s.path(root, record))
            task = tasks.load(root, target['task'])
            if task['origin']['handle'] != handle:
                raise s.Conflict('continuation mapping differs from current task')
            return target['task']
        task_id = row['id'] + '--' + s.uid()
        job_id = None
        changes = {}
        if original['job_id'] is not None:
            source_job = jobs.open_job(data, original['job_id'])
            job_record = f"来源保存/{row['id']}/岗位映射/{original['job_id']}.json"
            if s.path(root, job_record).exists():
                job_id = s.read_json(s.path(root, job_record))['job']
                jobs.open_job(root, job_id)
            else:
                job_id = row['id'] + '--' + s.uid()
                base = f'岗位/{job_id}'
                jd = base + '/JD_原始.md' if source_job['jd'] is not None else None
                job = {'id': job_id, 'jd': jd, 'jd_status': source_job['jd_status'],
                       'eligibility': 'UNKNOWN', 'availability': 'UNKNOWN',
                       'legacy': source_job['legacy'], 'original': source_job.get('original'),
                       'origin': {'handle': row['id'] + '::' + original['job_id']}}
                if jd is not None:
                    changes[jd] = s.path(data, source_job['jd']).read_bytes()
                changes[base + '/岗位.json'] = s.json_bytes(job)
                changes[base + '/备注.md'] = s.path(data, f"岗位/{original['job_id']}/备注.md").read_bytes()
                changes[job_record] = s.json_bytes({'job': job_id})
        task = {'id': task_id, 'job_id': job_id, 'author': author,
                'purpose': original['purpose'], 'legacy_id': original.get('legacy_id'),
                'current_revision': None, 'progress': [], 'adoption': 'prepared',
                'origin': {'handle': handle, 'source_path': row['data_path'],
                           'meaning': 'historical progress/dependencies stay with the source; new work is unreviewed'}}
        changes[tasks.task_path(task_id) + '/任务.json'] = s.json_bytes(task)
        changes[record] = s.json_bytes({'handle': handle, 'task': task_id, 'job': job_id})
        s.commit(root, changes)
        return task_id


def continue_task(root, handle, revision, author, refresh_materials=False):
    from . import tasks, materials
    data, local, row = resolve(root, handle)
    mapping = s.path(root, f"来源保存/{row['id']}/接续/{local}.json")
    current = s.read_json(mapping)['task'] if mapping.exists() else None
    use_current = revision is None and current is not None and tasks.load(root, current)['current_revision'] is not None
    selected = current if use_current else handle
    folder, work = tasks.get_revision(root, selected, revision)
    # Read before allocating identities, so unsupported/missing inputs fail honestly.
    spec = tasks.export_spec(root, selected, revision, refresh_materials and use_current) if work['kind'] == 'structured' else None
    if not author:
        raise ValueError('author required')
    target = _current_task(root, handle, author)
    if spec is not None:
        if not use_current:
            spec.pop('basis_materials', None)
            spec['materials'] = [name for name in ('背景', '兴趣', '偏好')
                                 if s.path(root, materials.rel(name)).is_file()]
            spec['source_work'] = {'kind': 'source', 'handle': handle, 'revision': work['revision'],
                                   'path': (folder / '作品.json').relative_to(root).as_posix()}
        spec['base_revision'] = tasks.load(root, target)['current_revision']
        result = tasks.save_revision(root, target, spec, author)
    else:
        refs = [(folder / ref['snapshot']).relative_to(root).as_posix() for ref in work['sources']]
        refs += [materials.rel(name) for name in ('背景', '兴趣', '偏好')
                 if s.path(root, materials.rel(name)).is_file()]
        source_work = {'kind': 'source', 'handle': handle, 'revision': work['revision'],
                       'path': (folder / '作品.json').relative_to(root).as_posix()} if not use_current else None
        result = tasks.import_artifact(root, target, folder / 'resume.html', author, work['purpose'], refs,
                                       source_work=source_work)
    return {**result, 'task': target, 'origin': handle}
