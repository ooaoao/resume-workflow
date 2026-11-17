from pathlib import Path
from . import storage as s, legacy

def business_id(value):
    value = str(value)
    if not value or value in ('.', '..') or any((c in value for c in '/\\:<>"|?*\x00')) or value.endswith((' ', '.')):
        raise ValueError('invalid business ID: ' + value)
    return value

def open_job(root, job_id):
    row = s.read_json(s.path(root, f'岗位/{business_id(job_id)}/岗位.json'))
    # First-package jobs did not record availability. Absence was never proof
    # that recruiting remained open.
    row.setdefault('availability', 'UNKNOWN')
    return row

def import_job(root, job_id, jd_file, metadata, *, resume_existing=False,
               source_identity=None, link_legacy_listing=False, local_jd=False):
    job_id = business_id(job_id)
    raw = Path(jd_file).read_bytes() if jd_file is not None else None
    if raw is None and metadata.get('jd_status') == '全文':
        raise ValueError('全文 JD requires the actual source text')
    try:
        text = raw.decode('utf-8-sig') if raw is not None else None
    except UnicodeError as exc:
        raise ValueError(f'{jd_file}: UTF-8 JD required') from exc
    if text is not None and not text.strip():
        raise ValueError('empty JD: ' + str(jd_file))
    base = f'岗位/{job_id}'
    row = {'id': job_id, 'jd': f'{base}/JD_原始.md' if raw is not None else None, 'jd_status': metadata.get('jd_status', 'UNKNOWN'), 'eligibility': metadata.get('eligibility', 'UNKNOWN'), 'legacy': metadata, 'original': str(Path(jd_file).absolute()) if jd_file is not None else None}
    if local_jd and jd_file is not None:
        relative = Path(jd_file).absolute().relative_to(Path(root).absolute()).as_posix()
        s.path(root, relative)
        row['jd'] = relative
        row['jd_source'] = 'root-original'
    if source_identity is not None:
        member = source_identity['member']
        if member is None:
            if jd_file is not None:
                raise ValueError('source identity does not identify the supplied JD')
        elif ((not local_jd and not str(member).replace('\\', '/').startswith('来源/'))
              or s.path(root, member) != Path(jd_file).absolute()):
            raise ValueError('source identity must identify this accepted local archive member')
        row['source_identity'] = source_identity
    with s.lock(root):
        if s.path(root, f'{base}/岗位.json').exists():
            if resume_existing:
                existing = open_job(root, job_id)
                if source_identity is None:
                    same_origin = existing['original'] == row['original']
                elif 'source_identity' in existing:
                    same_origin = existing['source_identity'] == source_identity
                elif link_legacy_listing and source_identity['member'] is None:
                    # Old listing-only jobs have no archived JD member to prove
                    # origin. Require the caller's explicit association to this
                    # migration, plus the retained pool and business identity.
                    old_metadata = existing['legacy']
                    same_origin = (
                        existing['jd'] is None
                        and existing['original'] is None
                        and jd_file is None
                        and old_metadata.get('pool_original') == source_identity['pool']
                        and job_id == str(old_metadata.get('batch')) + '--' + str(old_metadata.get('seq'))
                    )
                else:
                    # 506fd753 stored the received member only within an absolute
                    # original path. Match the exact archived member from the
                    # migration's retained mapping, including its intake UUID.
                    # Do not guess by filename or discard the old trace location.
                    origin = Path(existing['original']).parts if existing['original'] else ()
                    relative = Path(source_identity['member']).parts if source_identity['member'] else ()
                    same_origin = bool(relative) and tuple(part.casefold() for part in origin[-len(relative):]) == tuple(part.casefold() for part in relative)
                same_source = same_origin and existing['legacy'] == metadata
                accepted = s.path(root, existing['jd']).read_bytes() if existing['jd'] is not None else None
                if same_source and accepted == raw:
                    # A completed earlier item is reused; never rewrite notes,
                    # qualification decisions or other current user state.
                    return existing
                raise ValueError('existing batch job identity/content conflict: ' + job_id)
            raise FileExistsError(job_id)
        changes = {f'{base}/岗位.json': s.json_bytes(row), f'{base}/备注.md': b'# Notes\n'}
        if raw is not None and not local_jd:
            changes[row['jd']] = raw
        s.commit(root, changes)
    return row

def import_pool(root, pool_file):
    p = Path(pool_file).absolute()
    pool = s.read_json(p)
    positions = pool.get('positions')
    if not isinstance(positions, list):
        raise ValueError(f'{p}: positions list required')
    rows = []
    for position in positions:
        jd = position.get('jd_file')
        if not jd and position.get('jd_status') == '全文':
            raise ValueError(f"{p}: seq={position.get('seq')}: jd_file missing")
        jdp = s.path(p.parent, jd) if jd else None
        if jdp is not None and not jdp.exists() and position.get('jd_status') != '全文':
            jdp = None
        ident = f"{pool.get('batch', p.parent.name)}--{position['seq']}"
        rows.append(import_job(root, ident, jdp, {**position, 'batch': pool.get('batch', p.parent.name), 'pool_file': str(p), 'simulation_only': pool.get('simulation_only', False)}, resume_existing=True))
    legacy.import_legacy(root, [{'path': str(p.parent), 'id': pool.get('batch', p.parent.name), 'role': 'job-batch-originals'}], reuse_existing=True)
    return rows

def select(root, ids, mode='parallel'):
    if mode not in ('parallel', 'ordered'):
        raise ValueError('selection mode: parallel/ordered')
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate selected job')
    for j in ids:
        open_job(root, j)
    row = {'mode': mode, 'jobs': ids, 'meaning': 'user selection; eligibility/open status unchanged'}
    with s.lock(root):
        s.commit(root, {'岗位/选择.json': s.json_bytes(row)})
    return row

def selection(root):
    return s.read_json(s.path(root, '岗位/选择.json'))


def list_jobs(root):
    return [s.read_json(p) for p in sorted(s.path(root, '岗位').glob('*/岗位.json'))]


def update(root, job_id, spec):
    with s.lock(root):
        old = open_job(root, job_id)
        if spec['base'] != old:
            raise s.Conflict('job changed since open: ' + job_id)
        if not spec.get('actor'):
            raise ValueError('source/actor required for job update')
        base = f'岗位/{business_id(job_id)}'
        before = s.path(root, old['jd']).read_bytes() if old['jd'] is not None else None
        changes = {}
        history = base + '/历史/' + s.uid()
        changes[history + '/岗位.json'] = s.json_bytes(old)
        if before is not None:
            changes[history + '/JD.md'] = before
        row = {**old, 'legacy': {**old['legacy'], **spec.get('metadata', {})}}
        if 'jd_status' in spec.get('metadata', {}):
            row['jd_status'] = spec['metadata']['jd_status']
        if 'jd_status' in spec:
            if 'jd_status' in spec.get('metadata', {}) and spec['jd_status'] != spec['metadata']['jd_status']:
                raise ValueError('conflicting JD completeness declarations')
            row['jd_status'] = spec['jd_status']
        row['legacy']['jd_status'] = row['jd_status']
        if 'availability' in spec:
            if spec['availability'] not in ('active', 'closed', 'UNKNOWN'):
                raise ValueError('availability: active/closed/UNKNOWN')
            row['availability'] = spec['availability']
        if 'jd_text' in spec:
            prior_text = before.decode('utf-8-sig').replace('\r\n', '\n') if before is not None else None
            if prior_text != spec['base_jd']:
                raise s.Conflict('JD changed since open')
            if not spec['jd_text'].strip():
                raise ValueError('empty updated JD')
            row['jd'] = base + '/JD_原始.md' if old.get('jd_source') == 'root-original' else old['jd'] or base + '/JD_原始.md'
            row.pop('jd_source', None)
            changes[row['jd']] = spec['jd_text'].encode('utf8')
        if row['jd_status'] == '全文' and row['jd'] is None:
            raise ValueError('全文 JD requires actual source text')
        row['last_update'] = {'actor': spec['actor'], 'reason': spec.get('reason'), 'source': spec.get('source')}
        changes[base + '/岗位.json'] = s.json_bytes(row)
        s.commit(root, changes, {old['jd']: before} if old['jd'] is not None else None)
        return row


def policy(root, identity, spec):
    from . import evidence
    relative = f'岗位/共同条件/{business_id(identity)}.json'
    with s.lock(root):
        for job_id in spec['jobs']:
            open_job(root, job_id)
        current = s.read_json(s.path(root, relative)) if s.path(root, relative).exists() else None
        if current is not None and spec.get('base_revision') != current['revision']:
            raise s.Conflict('policy revision conflict')
        if not spec.get('text') or not spec.get('sources'):
            raise ValueError('policy requires actual text, explicit scope and sources')
        if any(ref['kind'] not in ('source', 'job') for ref in spec['sources']):
            raise ValueError('official policy must cite original source/JD, not recursively cite policies')
        row = {'id': identity, 'revision': s.uid(), 'content': {
            'jobs': spec['jobs'], 'text': spec['text'], 'constraints': spec.get('constraints', []),
            'evidence': evidence.capture(root, spec['sources']),
        }}
        changes = {relative: s.json_bytes(row)}
        if current is not None:
            changes[f'岗位/共同条件/历史/{identity}/{current["revision"]}.json'] = s.json_bytes(current)
        s.commit(root, changes)
        return row
