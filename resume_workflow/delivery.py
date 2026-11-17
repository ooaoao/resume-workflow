"""Reviews assert externally supplied identities; never manufacture a reviewer."""
from pathlib import Path
from . import storage as s, tasks, render

def record_review(root, task_id, revision, review):
    from .source_workspaces import require_current
    require_current(task_id)
    with s.lock(root):
        folder, work = tasks.get_revision(root, task_id, revision)
        kind = review['kind']
        if kind not in ('content-page', 'pdf'):
            raise ValueError('review kind: content-page/pdf')
        if not review.get('reviewer') or review['reviewer'] == work['author']:
            raise ValueError('independent reviewer identity required; author cannot self-review')
        if review['status'] not in ('accepted', 'rejected'):
            raise ValueError('review status: accepted/rejected')
        if not isinstance(review.get('findings'), list):
            raise ValueError('review findings list required')
        if review['status'] == 'accepted' and any((f.get('status') != 'resolved' for f in review['findings'])):
            raise ValueError('unresolved review findings')
        current = render.binding(root, task_id, revision, pdf=kind == 'pdf')
        if review['binding'] != current:
            raise ValueError('review binding does not match current work')
        technical = s.read_json(folder / ('pdf_check.json' if kind == 'pdf' else 'html_check.json'))
        if technical['failures'] and review['status'] == 'accepted':
            raise ValueError('technical checks failed; cannot record acceptance')
        if kind == 'pdf':
            require_review(root, task_id, revision, 'content-page', render.binding(root, task_id, revision))
        report = Path(review['report_file']).read_bytes()
        if not report.strip():
            raise ValueError('actual independent report is empty')
        ident = s.uid()
        row = {k: v for k, v in review.items() if k != 'report_file'}
        row['report'] = f'审查/{kind}-{ident}.md'
        row['report_digest'] = render.digest(report)
        row['identity_basis'] = 'caller-supplied external reviewer ID; controller verifies actual independence'
        base = str(folder.relative_to(Path(root))).replace('\\', '/')
        s.commit(root, {f"{base}/{row['report']}": report, f'{base}/审查/{kind}.json': s.json_bytes(row)})
        return row

def require_review(root, task_id, revision, kind, current):
    folder, _ = tasks.get_revision(root, task_id, revision)
    p = folder / '审查' / (kind + '.json')
    if not p.exists():
        raise ValueError('independent ' + kind + ' review required')
    row = s.read_json(p)
    if row['binding'] != current or row['status'] != 'accepted':
        raise ValueError('review binding changed or review not accepted: ' + kind)
    if render.digest(s.path(folder, row['report']).read_bytes()) != row['report_digest']:
        raise ValueError('review report changed: ' + kind)
    return row

def deliver(root, task_id, revision=None):
    from .source_workspaces import require_current
    require_current(task_id)
    with s.lock(root):
        if tasks.load(root, task_id)['job_id'] is None:
            raise ValueError('formal delivery requires actual job/JD')
        folder, work = tasks.get_revision(root, task_id, revision)
        revision = work['revision']
        if work['purpose'] == 'intermediate':
            raise ValueError('intermediate artifact cannot be formally delivered')
        html = render.binding(root, task_id, revision)
        pdf = render.binding(root, task_id, revision, pdf=True)
        require_review(root, task_id, revision, 'content-page', html)
        require_review(root, task_id, revision, 'pdf', pdf)
        for name in ('html_check.json', 'pdf_check.json'):
            if s.read_json(folder / name)['failures']:
                raise ValueError('technical check failed')
        base = f"交付/{tasks.task_path(task_id).split('/')[-1]}/{revision}"
        registration = s.path(root, base + '/登记.json')
        row = {'task': task_id, 'revision': revision, 'path': base, 'binding': pdf, 'state': 'delivered', 'submission': 'not implied by delivery'}
        if registration.exists():
            existing = s.read_json(registration)
            if existing != row:
                raise ValueError('delivered work changed')
            for name, expected in pdf['files'].items():
                if render.digest(s.path(root, base + '/' + name).read_bytes()) != expected:
                    raise ValueError('locked delivery changed: ' + name)
            return existing
        changes = {base + '/登记.json': s.json_bytes(row)}
        for f in folder.rglob('*'):
            if f.is_file():
                relative = str(f.relative_to(folder)).replace('\\', '/')
                s.path(folder, relative)
                changes[base + '/' + relative] = f.read_bytes()
        s.commit(root, changes)
        return row
