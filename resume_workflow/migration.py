"""Legacy business inventory and explicit editable continuation; no old runtime."""
from pathlib import Path
import hashlib
import posixpath
import re
from . import storage as s, legacy, jobs, materials, tasks
from . import records

ROOTS = {
    '02_事实与偏好': 'materials', '03_证明材料': 'evidence',
    '04_字体照片与参照': 'resources', '05_岗位任务': 'tasks',
    '06_正式输出': 'deliveries', '07_工作记录': 'records',
    '09_岗位池': 'batches', '10_秋招进度': 'progress',
    '_archive': 'archive',
}


def _relative(path):
    return str(path).replace('\\', '/').strip('/')


def _registered(registry):
    paths = set()
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('path', 'task_dir', 'batch_path', 'relative_path') and isinstance(child, str):
                    paths.add(_relative(child))
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    # Root registrations alone do not prove registration of every descendant.
    for key in ('tasks', 'deliveries', 'recruiting_batches', 'sources'):
        visit(registry.get(key, []))
    normalized = set()
    for path in paths:
        parts = path.split('/')
        positions = [i for i, part in enumerate(parts) if part in ROOTS]
        normalized.add('/'.join(parts[positions[0]:]) if len(positions) == 1 else path)
    return normalized


def inspect(source_root, registry_file=None):
    root = Path(source_root).absolute()
    if not s.is_dir(root):
        raise FileNotFoundError(root)
    s.path(root, '')
    registry = Path(registry_file) if registry_file else root / 'data_integrity_registry.json'
    known = _registered(s.read_json(registry)) if s.is_file(registry) else set()
    entries = {}
    task_rows = []
    batches = []
    direct_counts = {}
    excluded_runtime = []
    def files_below(path):
        result = []
        for candidate, is_directory in legacy._tree_entries(root, path, skip_runtime=True):
            if is_directory:
                continue
            candidate = s.path(root, _relative(candidate.relative_to(root)))
            if s.is_file(candidate):
                result.append(candidate)
        return result
    for directory, category in ROOTS.items():
        base = s.path(root, directory)
        if s.is_dir(base):
            with s.scandir(base) as scan:
                direct = sorted((base / entry.name for entry in scan), key=lambda path: path.name)
        else:
            direct = []
        direct_counts[directory] = len(direct)
        for p in direct:
            relative = _relative(p.relative_to(root))
            if legacy.runtime_path(Path(relative)):
                excluded_runtime.append(relative)
                continue
            p = s.path(root, relative)
            p_is_directory = s.is_dir(p)
            files = files_below(p) if p_is_directory else [p]
            entries[relative] = {'path': relative, 'category': category, 'exists': True,
                                 'kind': 'directory' if p_is_directory else 'file', 'registered': relative in known,
                                 'business_files': len(files)}
            if category == 'tasks' and p_is_directory:
                identity_file = p / '任务身份.json'
                identity = s.read_json(identity_file) if s.is_file(identity_file) else {'task_id': p.name}
                pairs = []
                file_set = set(files)
                for content in sorted(f for f in files if f.name == 'content.json'):
                    config = content.with_name('config.json')
                    if config in file_set:
                        pairs.append({'content': _relative(content.relative_to(p)), 'config': _relative(config.relative_to(p))})
                for content in sorted(f for f in files if f.parent == p and f.name.startswith('内容_') and f.suffix == '.json'):
                    config = content.with_name(content.name.replace('内容_', '配置_', 1))
                    if config in file_set:
                        pairs.append({'content': content.name, 'config': config.name})
                task_rows.append({'path': relative, 'identity': identity, 'editable_candidates': pairs,
                                  'historical_artifacts': [_relative(f.relative_to(p)) for f in files if f.suffix.lower() in ('.pdf', '.html')]})
        if category == 'batches' and s.is_dir(base):
            pools = []
            for pool, is_directory in legacy._tree_entries(root, base, skip_runtime=True):
                if is_directory or pool.name != '岗位清单.json':
                    continue
                relative = _relative(pool.relative_to(root))
                pools.append(s.path(root, relative))
            for pool in sorted(pools):
                relative = _relative(pool.relative_to(root))
                batches.append(relative)
                entries[relative] = {'path': relative, 'category': category, 'exists': True, 'kind': 'pool', 'registered': relative in known or _relative(pool.parent.relative_to(root)) in known}
    for rel in sorted(known):
        # Registry-only missing entries remain visible, never silently dropped.
        first = rel.split('/')[0]
        if first in ROOTS and rel not in entries:
            p = s.path(root, rel)
            entries[rel] = {'path': rel, 'category': ROOTS[first], 'exists': s.exists(p), 'kind': 'registered-reference', 'registered': True}
    return {'source_root': str(root), 'entries': list(entries.values()), 'tasks': task_rows,
            'batches': batches, 'direct_counts': direct_counts, 'excluded_runtime': excluded_runtime, 'registry_file': str(registry) if s.is_file(registry) else None}


def _path(identity):
    return f'迁入/{jobs.business_id(identity)}/迁入.json'


def load(root, identity):
    return s.read_json(s.path(root, _path(identity)))


def _save(root, identity, row):
    with s.lock(root):
        s.commit(root, {_path(identity): s.json_bytes(row)})


def _table(text):
    lines = text.splitlines()
    result = []
    headings = None
    for i, line in enumerate(lines):
        raw = line.strip()
        if not raw.startswith('|'):
            headings = None
            continue
        cells = [part.strip() for part in raw.strip('|').split('|')]
        separator = all(re.fullmatch(r':?-{3,}:?', cell) for cell in cells)
        if separator:
            continue
        next_cells = [part.strip() for part in lines[i + 1].strip().strip('|').split('|')] if i + 1 < len(lines) else []
        if next_cells and all(re.fullmatch(r':?-{3,}:?', cell) for cell in next_cells):
            headings = cells
        elif headings is not None:
            result.append({**dict(zip(headings, cells)), '_line': i + 1, '_raw': line, '_cells': cells})
    return result


def query(root, identity, category=None):
    row = load(root, identity)
    if not row.get('archive_complete'):
        row['sources'] = _source_mapping(root, row)
    result = []
    for entry in row['inventory']['entries']:
        if category is not None and entry['category'] != category:
            continue
        prefix = entry['path'].rstrip('/') + '/'
        originals = {key: value for key, value in row['sources'].items() if key == entry['path'] or key.startswith(prefix)}
        item = {**entry, 'originals': originals,
                'mode': 'registered-originals' if row.get('mode') == 'in-place' else 'archived-originals'}
        item['archive_complete'] = row.get('archive_complete', False)
        item['archived_files'] = len(originals)
        item['missing_originals'] = [local for local in originals.values() if not s.is_file(s.path(root, local))]
        for new_id, task in row['tasks'].items():
            if task['source'] == entry['path']:
                item.update(mode=task['state'], task_id=new_id, task=task)
        if entry['category'] == 'progress':
            item['table_rows'] = []
            for old, local in originals.items():
                if old.endswith('.md'):
                    item['table_rows'].extend(_table(s.read_text(s.path(root, local), encoding='utf-8-sig')))
        result.append(item)
    return result


def _source_mapping(root, row):
    source_root = Path(row['source_root'])
    result = {}
    for original in legacy.source_records(root):
        if original['legacy_id'] != row['id']:
            continue
        if original.get('state', 'complete') != 'complete':
            continue
        p = Path(original['original'])
        if original.get('mode') == 'in-place' and row.get('mode') == 'in-place':
            key = _source_key(Path(original['path']), Path(row['source_prefix']), outside=str(p))
        else:
            key = _relative(p.relative_to(source_root)) if p.is_relative_to(source_root) else str(p)
        if row.get('mode') == 'in-place' and legacy.runtime_path(Path(key)):
            continue
        result[key] = original['path']
    return result


def _source_key(path, source_root, *, outside=None):
    return _relative(path.relative_to(source_root)) if path.is_relative_to(source_root) else (outside or str(path))


def _coverage_errors(inventory, mapping, *, local_source_root=None):
    counts = {}
    for key in mapping:
        direct = '/'.join(key.split('/')[:2])
        counts[key] = counts.get(key, 0) + 1
        if direct != key:
            counts[direct] = counts.get(direct, 0) + 1
    errors = []
    for entry in inventory['entries']:
        if entry.get('exists') and 'business_files' in entry:
            key = entry['path']
            if (local_source_root is not None and entry['kind'] == 'explicit-supplement'
                    and Path(key).is_absolute()):
                key = _source_key(Path(key), local_source_root)
            actual = counts.get(key, 0)
            if actual != entry['business_files']:
                errors.append({'path': entry['path'], 'observed': entry['business_files'], 'archived': actual})
    return errors


def _member(mapping, raw):
    absolute = str(Path(raw).absolute()) if Path(raw).is_absolute() else None
    if absolute in mapping:
        return absolute
    normalized = _relative(raw)
    if normalized in mapping:
        return normalized
    # Absolute old roots may have moved. Only an exact full business-root suffix
    # is converted; never search by basename or read that old absolute path.
    parts = normalized.split('/')
    matches = ['/'.join(parts[i:]) for i, part in enumerate(parts) if part in ROOTS and '/'.join(parts[i:]) in mapping]
    if len(matches) != 1:
        raise ValueError('resource/source mapping required within declared business roots: ' + str(raw))
    return matches[0]


def _verify_old_binding(root, local, declared):
    if declared:
        # Existing exact source/resource identity, only for migration integrity.
        digest = hashlib.sha256()
        with s.open_file(s.path(root, local), 'rb') as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != declared:
            raise ValueError('legacy source/resource bytes differ from their existing binding: ' + local)


def import_workspace(root, identity, spec):
    listing_links = spec.get('link_legacy_listing_jobs', [])
    if not isinstance(listing_links, list) or any(not isinstance(job, str) for job in listing_links):
        raise ValueError('link_legacy_listing_jobs must be an explicit list of business job IDs')
    mode = spec.get('mode', 'copy')
    if mode not in ('copy', 'in-place'):
        raise ValueError('migration mode must be copy or in-place')
    source_root = Path(spec['source_root']).absolute()
    if mode == 'in-place' and not source_root.is_relative_to(Path(root).absolute()):
        raise ValueError('in-place source must be within the data root')
    plan_path = s.path(root, _path(identity))
    if s.exists(plan_path):
        row = load(root, identity)
        same_local = (mode == row.get('mode') == 'in-place'
                      and row['source_prefix'] == source_root.relative_to(root).as_posix())
        if not same_local and row['source_root'] != str(source_root):
            raise s.Conflict('migration identity belongs to another source root')
        if row.get('mode', 'copy') != mode:
            raise s.Conflict('migration mode cannot change within an existing identity')
    else:
        inventory = inspect(spec['source_root'], spec.get('registry_file'))
        for name, material in spec.get('materials', {}).items():
            if isinstance(material, dict):
                p = Path(material['path']).absolute()
                key = _source_key(p, source_root) if mode == 'in-place' else str(p)
                inventory['entries'].append({'path': key, 'category': 'materials', 'kind': 'explicit-supplement',
                                             'exists': s.exists(p), 'registered': False, 'business_files': 1, 'material': name})
        row = {'id': identity, 'source_root': str(Path(spec['source_root']).absolute()), 'inventory': inventory,
               'sources': {}, 'materials': {}, 'tasks': {}, 'archive_complete': False, 'state': 'archiving originals'}
        row['mode'] = mode
        if mode == 'in-place':
            row['source_prefix'] = source_root.relative_to(root).as_posix()
        _save(root, identity, row)
    if not row.get('archive_complete'):
        sources = []
        source_root = Path(spec['source_root']).absolute()
        for directory in ROOTS:
            if s.is_dir(source_root / directory):
                sources.append({'path': str(source_root / directory), 'id': identity, 'role': 'legacy-' + ROOTS[directory]})
        if row['inventory']['registry_file']:
            sources.append({'path': row['inventory']['registry_file'], 'id': identity, 'role': 'legacy-registry-history'})
        for name, material in spec.get('materials', {}).items():
            if isinstance(material, dict):
                sources.append({'path': material['path'], 'id': identity, 'role': 'explicit-material-' + name})
        try:
            if mode == 'in-place':
                legacy.register_local(root, sources, skip_runtime=True)
            else:
                legacy.import_legacy(root, sources, reuse_existing=True, skip_runtime=True)
            completed = _source_mapping(root, row)
            # Previous partial in-place records stored explicit paths as absolute.
            gaps = _coverage_errors(row['inventory'], completed,
                                    local_source_root=Path(row['source_root']) if mode == 'in-place' else None)
            if gaps:
                raise ValueError('source coverage changed since discovery: ' + str(gaps))
        except (OSError, ValueError) as exc:
            row.update(sources=_source_mapping(root, row), state='partial original intake', error=str(exc))
            _save(root, identity, row)
            raise
        row.pop('error', None)
        row.update(sources=completed, archive_complete=True, state='archived; conversion pending')
        _save(root, identity, row)
    mapping = row['sources']

    def material_key(value):
        if not isinstance(value, dict):
            return value
        raw = value['path']
        return _member(mapping, _source_key(Path(raw).absolute(), source_root) if mode == 'in-place' else raw)

    try:
        if mode == 'in-place':
            missing = [local for local in mapping.values() if not s.is_file(s.path(root, local))]
            if missing:
                raise FileNotFoundError('registered originals missing: ' + str(missing))
        for name, original in spec.get('materials', {}).items():
            if name in row['materials']:
                continue
            key = material_key(original)
            text = s.read_text(s.path(root, mapping[key]), encoding='utf-8-sig')
            if not s.exists(s.path(root, materials.rel(name))):
                accepted = materials.initialize(root, name, text)
                revision = accepted['revision']
            else:
                accepted = materials.read_material(root, name)
                if accepted['text'] == text.replace('\r\n', '\n'):
                    revision = accepted['revision']
                else:
                    revision = s.uid()
                    with s.lock(root):
                        s.commit(root, {f'历史/主资料/{name}/{revision}/内容.md': text.encode('utf8'),
                                        f'历史/主资料/{name}/{revision}/来源.json': s.json_bytes({'source': mapping[key], 'role': 'historical; current material preserved'})})
            row['materials'][name] = revision
            _save(root, identity, row)
        for requested in spec.get('tasks', []):
            new_id = jobs.business_id(requested['id'])
            if new_id in row['tasks']:
                continue
            old = next(task for task in row['inventory']['tasks'] if task['path'] == requested['path'])
            prefix = old['path'] + '/'
            content = s.read_json(s.path(root, mapping[prefix + requested['content']]))
            config = s.read_json(s.path(root, mapping[prefix + requested['config']]))
            render_prefix = prefix + requested['render'].strip('/') + '/' if requested.get('render') else prefix + str(Path(requested['content']).parent).replace('\\', '/').strip('.')
            render_prefix = render_prefix.rstrip('/') + '/'
            resources_file = render_prefix + 'resources.json'
            resource_records = s.read_json(s.path(root, mapping[resources_file])) if resources_file in mapping else {}
            resources = {}
            for key, original in content.get('resources', {}).items():
                source_key = _member(mapping, requested.get('resources', {}).get(key, original))
                resources[key] = mapping[source_key]
                _verify_old_binding(root, resources[key], resource_records.get(key, {}).get('sha256'))
            if resources:
                content['resources'] = resources
            if 'resource_purpose' in requested:
                content['resource_purpose'] = requested['resource_purpose']
            jd_key = prefix + requested.get('jd', 'JD_原始.md')
            if jd_key not in mapping:
                raise ValueError('old task requires explicit JD mapping for resume continuation')
            job_id = requested.get('job_id', identity + '--' + new_id)
            if not s.exists(s.path(root, f'岗位/{job_id}/岗位.json')):
                jobs.import_job(root, job_id, s.path(root, mapping[jd_key]), {
                    'jd_status': requested.get('jd_status', 'UNKNOWN'),
                    'legacy_task_id': old['identity'].get('task_id'), 'legacy_subject': old['identity'],
                }, local_jd=mode == 'in-place')
            legacy_id = old['identity'].get('task_id', Path(old['path']).name)
            if not s.exists(s.path(root, tasks.task_path(new_id) + '/任务.json')):
                tasks.create_task(root, new_id, job_id, requested.get('author', 'legacy-source-author'), legacy_id=legacy_id)
            existing = tasks.load(root, new_id)
            history = {'migration': identity, 'source': old['path'], 'originals': {key: value for key, value in mapping.items() if key.startswith(prefix)},
                       'artifacts': old['historical_artifacts'], 'review_status': 'historical scope only; not a new approval'}
            if existing['current_revision'] and existing.get('legacy_history', {}).get('migration') != identity:
                raise s.Conflict('existing editable task belongs to another origin')
            tasks.configure(root, new_id, {'legacy_id': legacy_id, 'legacy_history': history})
            if existing['current_revision']:
                saved = {'revision': existing['current_revision']}
            else:
                education = requested.get('education', content.get('education', []))
                education = s.read_json(s.path(root, mapping[requested['education_file']])) if 'education_file' in requested else education
                if not education and render_prefix + 'education.json' in mapping:
                    education = s.read_json(s.path(root, mapping[render_prefix + 'education.json']))
                if content.get('education_record_ids') and not education:
                    raise ValueError('select the actual render/education.json for source-bound legacy education')
                for entry in education:
                    source = entry.get('source') or entry.get('source_binding')
                    if not source:
                        raise ValueError('legacy education requires its actual source binding')
                    source_key = _member(mapping, source['path'])
                    quote = source.get('quote') or source.get('source_quote') or entry.get('source_quote')
                    if not quote:
                        raise ValueError('legacy education source_quote missing')
                    _verify_old_binding(root, mapping[source_key], source.get('sha256'))
                    entry['source'] = {'path': mapping[source_key], 'quote': quote}
                # Only textual sources feed author/reviewer evidence. Large
                # self-contained HTML/PDF stay once in the historical archive.
                sources = [value for key, value in mapping.items() if (key.startswith(prefix) and Path(key).suffix.lower() in ('.md', '.json', '.txt')) or key in spec.get('sources', [])]
                sources += [mapping[material_key(value)] for value in spec.get('materials', {}).values()]
                sources += [entry['source']['path'] for entry in education]
                saved = tasks.save_revision(root, new_id, {
                    'content': content, 'config': config, 'sources': list(dict.fromkeys(sources)),
                    'basis_materials': row['materials'], 'education': education,
                    'selection_notes': requested.get('selection_notes', 'Historical input selected explicitly: ' + requested['content']),
                }, requested.get('author', 'legacy-source-author'))
            row['tasks'][new_id] = {'state': 'editable', 'source': old['path'], 'revision': saved['revision']}
            _save(root, identity, row)
        if spec.get('import_batches'):
            for pool in row['inventory']['batches']:
                payload = s.read_json(s.path(root, mapping[pool]))
                for position in payload['positions']:
                    jd_key = posixpath.normpath(posixpath.join(posixpath.dirname(pool), position['jd_file'])) if position.get('jd_file') else None
                    job_id = str(payload.get('batch', Path(pool).parent.name)) + '--' + str(position['seq'])
                    jd_file = s.path(root, mapping[jd_key]) if jd_key in mapping else None
                    jobs.import_job(root, job_id, jd_file,
                                    {**position, 'batch': payload.get('batch'), 'pool_original': pool},
                                    resume_existing=True,
                                    source_identity={'migration': identity, 'pool': pool, 'member': mapping.get(jd_key)},
                                    link_legacy_listing=job_id in listing_links,
                                    local_jd=mode == 'in-place')
        tables = [key for key in mapping if key.endswith('.md') and key.split('/')[0] in ('07_工作记录', '10_秋招进度')]
        if 'record_tables' not in row:
            row['record_tables'] = {key: s.uid() for key in tables}
            _save(root, identity, row)
        imported_records = []
        for key in tables:
            for parsed in _table(s.read_text(s.path(root, mapping[key]), encoding='utf-8-sig')):
                record_id = identity + '--' + row['record_tables'][key] + '-' + str(parsed['_line'])
                if not s.exists(s.path(root, f'业务记录/{record_id}/当前.json')):
                    records.create(root, record_id, {
                        'kind': 'legacy-record', 'actor': 'legacy-source-intake',
                        'data': {'fields': parsed, 'legacy_source': mapping[key], 'historical': True,
                                 'meaning': 'original columns; no new submission/adoption inferred'},
                        'dependencies': [{'kind': 'source', 'path': mapping[key], 'quote': parsed['_raw']}],
                    })
                imported_records.append(record_id)
        row['records'] = imported_records
        row['state'] = ('requested conversions complete; other originals remain registered'
                        if mode == 'in-place' else 'requested conversions complete; other originals remain archived')
        row.pop('error', None)
        _save(root, identity, row)
        return row
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        row['state'] = 'partial; requested conversion failed'
        row['error'] = str(exc)
        _save(root, identity, row)
        raise
