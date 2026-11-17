"""Independent activity, adoption, application and follow-up records."""
from datetime import datetime, timezone
from . import storage as s, evidence
from .jobs import business_id


def _path(identity):
    return f'业务记录/{business_id(identity)}/当前.json'


def show(root, identity):
    return s.read_json(s.path(root, _path(identity)))


def _validate(root, row):
    if row['kind'] not in ('activity', 'adoption', 'application', 'followup', 'legacy-record') or not row.get('actor'):
        raise ValueError('explicit business kind and actor required')
    if row['kind'] == 'application' and row['data'].get('state') == 'submitted' and not row['data'].get('submitted_at'):
        raise ValueError('actual submitted_at required; delivery/adoption does not imply submission')
    for key in ('task_id', 'job_id'):
        if row['data'].get(key):
            if key == 'task_id':
                from .tasks import load
                load(root, row['data'][key])
            else:
                from .jobs import open_job
                open_job(root, row['data'][key])
    if row['data'].get('revision'):
        from .tasks import get_revision
        get_revision(root, row['data']['task_id'], row['data']['revision'])


def create(root, identity, spec):
    with s.lock(root):
        if s.path(root, _path(identity)).exists():
            raise FileExistsError(identity)
        row = {**spec, 'id': identity, 'revision': s.uid(),
               'recorded_at': datetime.now(timezone.utc).isoformat(),
               'evidence': evidence.capture(root, spec.get('dependencies', []))}
        _validate(root, row)
        s.commit(root, {_path(identity): s.json_bytes(row)})
        return row


def update(root, identity, spec):
    with s.lock(root):
        old = show(root, identity)
        if old['revision'] != spec['base_revision']:
            raise s.Conflict('business record changed since open')
        row = {**old, 'actor': spec['actor'], 'revision': s.uid(),
               'data': {**old['data'], **spec['data']}, 'recorded_at': datetime.now(timezone.utc).isoformat()}
        if 'dependencies' in spec:
            row['evidence'] = evidence.capture(root, spec['dependencies'])
        _validate(root, row)
        s.commit(root, {_path(identity): s.json_bytes(row),
                        f'业务记录/{identity}/历史/{old["revision"]}.json': s.json_bytes(old)})
        return row


def list_records(root):
    with s.lock(root):
        return [{**s.read_json(p), 'needs_review': evidence.changes(root, s.read_json(p)['evidence'])}
                for p in sorted(s.path(root, '业务记录').glob('*/当前.json'))]
