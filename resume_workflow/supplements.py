"""Task/user fact supplements and retryable writes through the material boundary."""
from . import storage as s, materials, evidence
from .jobs import business_id


def _path(identity):
    return f'资料补充/{business_id(identity)}.json'


def add(root, identity, spec):
    for key in ('statement', 'confirmed_by', 'confirmation'):
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            raise ValueError('supplement requires actual statement/confirmation: ' + key)
    materials.rel(spec['material'])
    with s.lock(root):
        p = s.path(root, _path(identity))
        if p.exists():
            raise FileExistsError(identity)
        materials.version(root, spec['material'], spec['base_revision'])
        if spec.get('task_id'):
            from .tasks import load
            load(root, spec['task_id'])
        row = {**spec, 'id': identity, 'state': 'pending', 'history': [],
               'evidence': evidence.capture(root, spec.get('dependencies', []))}
        s.commit(root, {_path(identity): s.json_bytes(row)})
        return row


def show(root, identity):
    return s.read_json(s.path(root, _path(identity)))


def revise(root, identity, base_revision, proposed_text):
    with s.lock(root):
        row = show(root, identity)
        if row['state'] == 'saved':
            raise ValueError('saved supplement is immutable; create another supplement')
        materials.version(root, row['material'], base_revision)
        row['history'].append({'base_revision': row['base_revision'], 'proposed_text': row['proposed_text']})
        row.update(base_revision=base_revision, proposed_text=proposed_text, state='pending')
        s.commit(root, {_path(identity): s.json_bytes(row)})
        return row


def apply(root, identity):
    with s.lock(root):
        row = show(root, identity)
        if row['state'] == 'saved':
            return row['result']
        def receipt(result):
            row.update(state='saved', result=result)
            return {_path(identity): s.json_bytes(row)}
        result = materials._write(root, row['material'], row['base_revision'], row['proposed_text'], receipt)
        if result['status'] == 'conflict':
            row.update(state='conflict', result=result)
            s.commit(root, {_path(identity): s.json_bytes(row)})
        return result
