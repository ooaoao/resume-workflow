from pathlib import Path
from . import storage as s, materials, jobs

def task_path(task_id):
    return f'任务/{jobs.business_id(task_id)}'

def create_task(root, task_id, job_id, author, purpose='resume', legacy_id=None):
    if purpose not in ('resume', 'intermediate'):
        raise ValueError('task purpose: resume/intermediate')
    if job_id is None:
        if purpose != 'intermediate':
            raise ValueError('formal resume requires actual job/JD')
    else:
        job = jobs.open_job(root, job_id)
        if job['jd_status'] != '全文':
            raise ValueError('制作任务需要全文JD；当前 ' + job['jd_status'])
    if not author:
        raise ValueError('author required')
    row = {'id': jobs.business_id(task_id), 'job_id': job_id, 'author': author, 'purpose': purpose, 'legacy_id': legacy_id, 'current_revision': None, 'progress': [], 'adoption': 'prepared'}
    rel = task_path(task_id) + '/任务.json'
    with s.lock(root):
        if s.path(root, rel).exists():
            raise FileExistsError(task_id)
        s.commit(root, {rel: s.json_bytes(row)})
    return row

def load(root, task_id):
    from .source_workspaces import resolve
    root, task_id, _ = resolve(root, task_id)
    return s.read_json(s.path(root, task_path(task_id) + '/任务.json'))

def revision_path(task_id, revision):
    return task_path(task_id) + '/版本/' + jobs.business_id(revision)

def get_revision(root, task_id, revision=None):
    from .source_workspaces import resolve
    root, task_id, _ = resolve(root, task_id)
    if revision is None:
        revision = load(root, task_id)['current_revision']
    if revision is None:
        raise ValueError('task has no saved work')
    rel = revision_path(task_id, revision)
    return (s.path(root, rel), s.read_json(s.path(root, rel + '/作品.json')))

def open_task(root, task_id):
    if '::' in str(task_id):
        from .source_workspaces import describe_task
        return describe_task(root, task_id)
    task = load(root, task_id)
    task['needs_review'] = []
    if task['current_revision']:
        _, work = get_revision(root, task_id)
        for name, revision in work.get('material_revisions', {}).items():
            try:
                if materials.read_material(root, name)['revision'] != revision:
                    task['needs_review'].append(name)
            except ValueError as exc:
                task['needs_review'].append({'material': name, 'error': str(exc)})
        if task['job_id'] is not None:
            job = jobs.open_job(root, task['job_id'])
            current_jd = s.path(root, job['jd']).read_bytes()
            p, _ = get_revision(root, task_id)
            if current_jd != (p / '依据/JD.md').read_bytes():
                task['needs_review'].append('JD')
    return task

def save_revision(root, task_id, spec, author):
    """Save structured content/config and actual source snapshots, without judging prose."""
    with s.lock(root):
        task = load(root, task_id)
        if task['job_id'] is None:
            raise ValueError('structured resume requires job/JD; use import-artifact for standalone HTML')
        if jobs.open_job(root, task['job_id'])['jd_status'] != '全文':
            raise ValueError('正式简历保存需要当前全文JD；旧作品仍可查询')
        current = task['current_revision']
        if 'base_revision' in spec and spec['base_revision'] != current:
            raise ValueError('task revision conflict')
        if not author:
            raise ValueError('author required')
        revision = s.uid()
        base = revision_path(task_id, revision)
        changes = {}
        bindings = {}
        for name in spec.get('materials', []):
            mat = materials._read(root, name)
            bindings[name] = mat['revision']
            changes[f'{base}/依据/主资料/{name}.md'] = mat['text'].encode('utf8')
        for name, historical in spec.get('basis_materials', {}).items():
            if name in bindings:
                raise ValueError('choose current or historical basis for each material')
            text = materials.version(root, name, historical)
            bindings[name] = historical
            changes[f'{base}/依据/主资料/{name}.md'] = text.encode('utf8')
        sources = []
        for i, source in enumerate(spec.get('sources', [])):
            p = s.path(root, source)
            if not p.is_file():
                raise ValueError('source must be a local accepted file: ' + source)
            dest = f'依据/来源/{i}-{p.name}'
            changes[f'{base}/{dest}'] = p.read_bytes()
            sources.append({'source': source, 'snapshot': dest})
        content = dict(spec['content'])
        resources = content.get('resources', {})
        copied = {}
        for key, source in resources.items():
            p = Path(source)
            if not p.is_absolute():
                p = s.path(root, source)
            s.path(p.parent, p.name)
            dest = f'资源/{key}{p.suffix}'
            changes[f'{base}/{dest}'] = p.read_bytes()
            copied[key] = dest
        if resources:
            content['resources'] = copied
        job = jobs.open_job(root, task['job_id'])
        changes[f'{base}/依据/JD.md'] = s.path(root, job['jd']).read_bytes()
        note_name = f"岗位/{task['job_id']}/备注"
        if note_name in bindings:
            note = {'revision': bindings[note_name], 'text': materials.version(root, note_name, bindings[note_name])}
        else:
            note = materials._read(root, note_name)
        bindings[note_name] = note['revision']
        changes[f'{base}/依据/岗位备注.md'] = note['text'].encode('utf8')
        changes[f'{base}/依据/岗位.json'] = s.json_bytes(job)
        work = {'revision': revision, 'author': author, 'kind': 'structured', 'material_revisions': bindings, 'sources': sources, 'education': spec.get('education', []), 'selection_notes': spec.get('selection_notes', ''), 'previous_revision': current, 'purpose': task['purpose']}
        if 'source_work' in spec:
            work['source_work'] = spec['source_work']
        changes[f'{base}/content.json'] = s.json_bytes(content)
        changes[f'{base}/config.json'] = s.json_bytes(spec['config'])
        changes[f'{base}/作品.json'] = s.json_bytes(work)
        task['current_revision'] = revision
        changes[task_path(task_id) + '/任务.json'] = s.json_bytes(task)
        s.commit(root, changes)
        return {'revision': revision, 'path': base, 'stage': 'saved; not independently reviewed'}

def import_artifact(root, task_id, html_file, author, purpose, sources, source_work=None):
    if purpose not in ('intermediate', 'resume'):
        raise ValueError('purpose must be intermediate/resume')
    raw = Path(html_file).read_bytes()
    raw.decode('utf-8-sig')
    with s.lock(root):
        task = load(root, task_id)
        if task['job_id'] is None and purpose != 'intermediate':
            raise ValueError('no-JD task supports intermediate artifacts only')
        if purpose == 'resume' and jobs.open_job(root, task['job_id'])['jd_status'] != '全文':
            raise ValueError('formal resume import requires current full JD')
        revision = s.uid()
        base = revision_path(task_id, revision)
        changes = {f'{base}/resume.html': raw}
        refs = []
        for i, source in enumerate(sources):
            p = s.path(root, source)
            dest = f'依据/来源/{i}-{p.name}'
            changes[f'{base}/{dest}'] = p.read_bytes()
            refs.append({'source': source, 'snapshot': dest})
        if task['job_id'] is not None:
            job = jobs.open_job(root, task['job_id'])
            changes[f'{base}/依据/JD.md'] = s.path(root, job['jd']).read_bytes()
        work = {'revision': revision, 'kind': 'html', 'author': author, 'purpose': purpose,
                'sources': refs, 'material_revisions': {}, 'previous_revision': task['current_revision']}
        if source_work is not None:
            work['source_work'] = source_work
        changes[f'{base}/作品.json'] = s.json_bytes(work)
        task['current_revision'] = revision
        changes[task_path(task_id) + '/任务.json'] = s.json_bytes(task)
        s.commit(root, changes)
        return {'revision': revision, 'path': base, 'stage': 'imported; not independently reviewed'}

def progress(root, task_id, state, note):
    if state not in ('prepared', 'adopted', 'submitted', 'follow-up'):
        raise ValueError('invalid progress state')
    with s.lock(root):
        task = load(root, task_id)
        task['progress'].append({'state': state, 'note': note})
        if state in ('prepared', 'adopted'):
            task['adoption'] = state
        if state == 'submitted':
            task['submission'] = 'submitted'
        s.commit(root, {task_path(task_id) + '/任务.json': s.json_bytes(task)})
    return task


def versions(root, task_id):
    from .source_workspaces import resolve
    workspace_root = Path(root).absolute()
    root, task_id, _ = resolve(root, task_id)
    task = load(root, task_id)
    result = []
    for p in sorted(s.path(root, task_path(task_id) + '/版本').glob('*/作品.json')):
        work = s.read_json(p)
        result.append({**work, 'path': str(p.parent.relative_to(workspace_root)), 'current': work['revision'] == task['current_revision']})
    return result


def export_spec(root, task_id, revision=None, refresh_materials=False):
    folder, work = get_revision(root, task_id, revision)
    if work['kind'] != 'structured':
        raise ValueError('HTML work is continued by importing a new HTML revision')
    content = s.read_json(folder / 'content.json')
    content['resources'] = {key: str((folder / rel).relative_to(root)) for key, rel in content.get('resources', {}).items()}
    mapping = {row['source']: str((folder / row['snapshot']).relative_to(root)) for row in work['sources']}
    education = s.read_json(folder / '作品.json').get('education', [])
    for row in education:
        row['source']['path'] = mapping[row['source']['path']]
    result = {
        'base_revision': load(root, task_id)['current_revision'],
        'source_revision': work['revision'], 'content': content,
        'config': s.read_json(folder / 'config.json'), 'sources': list(mapping.values()),
        'education': education, 'selection_notes': work.get('selection_notes', ''),
    }
    if refresh_materials:
        result['materials'] = list(work.get('material_revisions', {}))
    else:
        result['basis_materials'] = work.get('material_revisions', {})
    return result


def inputs(root, task_id, revision=None, from_task=None, reuse='layout'):
    """Current authoring facts and optional, explicitly selected historical input."""
    if reuse not in ('layout', 'draft'):
        raise ValueError('reuse must be layout or draft')
    if revision is None and (from_task is not None or reuse == 'draft'):
        raise ValueError('explicit revision required for historical input')
    from .workspace import quality_references
    from .source_workspaces import resolve
    task_root, local_task, origin = resolve(root, task_id)
    references = quality_references(root)
    with s.lock(root):
        task = load(root, task_id)

        def current_material(name):
            relative = materials.rel(name)
            if not s.path(root, relative).exists():
                return {'name': name, 'path': relative, 'status': 'missing'}
            return materials._read(root, name)

        job = jobs.open_job(task_root, task['job_id']) if task['job_id'] is not None else None
        if job is not None:
            job = {**job, 'jd_text': s.path(task_root, job['jd']).read_text(encoding='utf-8-sig')
                   if job['jd'] is not None else None}
            if origin and job['jd'] is not None:
                job['jd'] = s.path(task_root, job['jd']).relative_to(root).as_posix()
        if origin and job is not None:
            name = f"岗位/{task['job_id']}/备注"
            note = s.path(task_root, materials.rel(name))
            notes = {'name': name, 'path': note.relative_to(root).as_posix(),
                     'text': note.read_text(encoding='utf-8-sig'), 'scope': 'historical-source'}
            head = s.path(task_root, f'历史/主资料/{name}/current.json')
            if head.exists():
                accepted_revision = s.read_json(head)['revision']
                if materials.version(task_root, name, accepted_revision) == notes['text'].replace('\r\n', '\n'):
                    notes['revision'] = accepted_revision
        else:
            notes = current_material(f"岗位/{task['job_id']}/备注") if job is not None else None
        current = {
            'job': job,
            'materials': {name: current_material(name) for name in ('背景', '兴趣', '偏好')},
            'job_notes': notes,
        }
        history = None
        if revision is not None:
            source_task = from_task if from_task is not None else task_id
            folder, work = get_revision(root, source_task, revision)
            if work['kind'] != 'structured':
                raise ValueError('structured historical input required; HTML continues by import-artifact')
            history = {'task': source_task, 'revision': work['revision'], 'reuse': reuse}
            if reuse == 'layout':
                content = s.read_json(folder / 'content.json')
                history.update(
                    config=s.read_json(folder / 'config.json'),
                    resources={key: s.path(folder, relative).relative_to(root).as_posix()
                               for key, relative in content.get('resources', {}).items()},
                    resource_purpose=content.get('resource_purpose', 'personal'))
            else:
                history['spec'] = export_spec(root, source_task, revision)
                history['spec']['base_revision'] = task['current_revision']
                basis_materials = {}
                note_name = f"岗位/{load(root, source_task)['job_id']}/备注"
                for name, material_revision in work['material_revisions'].items():
                    relative = ('依据/岗位备注.md' if name == note_name
                                else f'依据/主资料/{name}.md')
                    p = s.path(folder, relative)
                    basis_materials[name] = {'revision': material_revision,
                                             'path': p.relative_to(root).as_posix(),
                                             'text': p.read_text(encoding='utf-8-sig')}
                jd = s.path(folder, '依据/JD.md')
                history['basis'] = {
                    'jd': {'path': jd.relative_to(root).as_posix(), 'text': jd.read_text(encoding='utf-8-sig')},
                    'materials': basis_materials,
                    'sources': history['spec']['sources'],
                }
        if origin:
            task = {**task, 'handle': task_id, 'scope': 'historical'}
        return {'task': task, 'current': current, 'history': history, 'references': references}


def configure(root, task_id, spec):
    if set(spec) - {'legacy_id', 'aliases', 'legacy_history'}:
        raise ValueError('only legacy identity/history fields may be configured')
    with s.lock(root):
        row = load(root, task_id)
        row['progress'].append({'state': 'identity-update', 'previous': {key: row.get(key) for key in spec}})
        row.update(spec)
        s.commit(root, {task_path(task_id) + '/任务.json': s.json_bytes(row)})
        return row


def convert_html(root, task_id, html_file, author, sources=None, chrome=None):
    from . import render
    raw = Path(html_file).read_bytes()
    render.validate_passive(raw.decode('utf-8-sig'))
    existing_path = s.path(root, task_path(task_id) + '/任务.json')
    if not existing_path.exists():
        create_task(root, task_id, None, author, 'intermediate')
    task = load(root, task_id)
    if task['job_id'] is not None or task['purpose'] != 'intermediate':
        raise ValueError('standalone conversion ID belongs to a different task')
    same = False
    if task['current_revision']:
        folder, work = get_revision(root, task_id)
        same = work['kind'] == 'html' and (folder / 'resume.html').read_bytes() == raw
    if not same:
        import_artifact(root, task_id, html_file, author, 'intermediate', sources or [])
    html = render.render_html(root, task_id, chrome=chrome)
    if html['check']['failures']:
        return {'task': task_id, 'purpose': 'intermediate', 'check': html['check'], 'html': html}
    pdf = render.render_pdf(root, task_id, chrome=chrome)
    return {'task': task_id, 'revision': load(root, task_id)['current_revision'],
            'purpose': 'intermediate; not formally delivered', 'check': pdf['check'], 'html': html, 'pdf': pdf}
