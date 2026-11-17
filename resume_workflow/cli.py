import argparse
import json
import sys
from pathlib import Path
from . import storage as s, materials, jobs, legacy, tasks, render, delivery, recovery
from . import screening, migration, supplements, records, ingest
from . import workspace, merge

def parser():
    p = argparse.ArgumentParser(description='独立简历工作流：明确资料根，接续已保存作品')
    p.add_argument('--data-root', type=Path, help='explicit root overrides workspace configuration')
    p.add_argument('--workspace', type=Path, help='workspace.json; defaults to the current system directory')
    commands = p.add_subparsers(dest='command', required=True)
    work = commands.add_parser('workspace').add_subparsers(dest='action', required=True)
    work.add_parser('init')
    work.add_parser('open')
    q = work.add_parser('merge')
    q.add_argument('id')
    q.add_argument('--spec', type=Path, required=True)
    material = commands.add_parser('material').add_subparsers(dest='action', required=True)
    for name in ('init', 'read', 'update'):
        q = material.add_parser(name)
        q.add_argument('name', help='背景/兴趣/偏好 or 岗位/<id>/备注')
        if name != 'read':
            q.add_argument('--file', type=Path, required=True)
        if name == 'update':
            q.add_argument('--base-revision', required=True)
    q = material.add_parser('search')
    q.add_argument('query')
    q.add_argument('--names', nargs='*')
    q = material.add_parser('reindex')
    q.add_argument('--names', nargs='*')
    old = commands.add_parser('legacy').add_subparsers(dest='action', required=True)
    q = old.add_parser('import')
    q.add_argument('--spec', type=Path, required=True, help='JSON list: path, id, role, optional review_scope')
    q = old.add_parser('inspect')
    q.add_argument('source_root', type=Path)
    q.add_argument('--registry', type=Path)
    q = old.add_parser('migrate')
    q.add_argument('id')
    q.add_argument('--spec', type=Path, required=True)
    q = old.add_parser('query')
    q.add_argument('id')
    q.add_argument('--category', choices=list(migration.ROOTS.values()))
    q = old.add_parser('sources')
    q.add_argument('--id', help='optional source legacy/migration identity')
    job = commands.add_parser('job').add_subparsers(dest='action', required=True)
    q = job.add_parser('import')
    q.add_argument('--id')
    q.add_argument('--jd', type=Path)
    q.add_argument('--metadata', type=Path)
    q.add_argument('--pool', type=Path)
    q = job.add_parser('open')
    q.add_argument('id')
    q = job.add_parser('select')
    q.add_argument('ids', nargs='+')
    q.add_argument('--mode', choices=['parallel', 'ordered'], default='parallel')
    job.add_parser('list')
    for name in ('update', 'policy'):
        q = job.add_parser(name)
        q.add_argument('id')
        q.add_argument('--spec', type=Path, required=True)
    q = job.add_parser('policy-open')
    q.add_argument('id')
    q = job.add_parser('ingest')
    q.add_argument('--source', type=Path, required=True)
    q.add_argument('--ids', type=Path)
    q.add_argument('--id-prefix', default='')
    q.add_argument('--actor', default='standardized-ingest')
    q.add_argument('--jd-status', default='全文')
    task = commands.add_parser('task').add_subparsers(dest='action', required=True)
    q = task.add_parser('create')
    q.add_argument('id')
    q.add_argument('--job')
    q.add_argument('--author', required=True)
    q.add_argument('--legacy-id')
    q.add_argument('--purpose', choices=['resume', 'intermediate'], default='resume')
    q = task.add_parser('open')
    q.add_argument('id')
    q = task.add_parser('inputs')
    q.add_argument('id')
    q.add_argument('--from-task', help='source task for an explicitly selected historical revision')
    q.add_argument('--revision', help='explicit historical revision; omitted means current facts only')
    q.add_argument('--reuse', choices=['layout', 'draft'], default='layout',
                   help='layout returns configuration/resources only; draft also returns old content and basis')
    q = task.add_parser('save')
    q.add_argument('id')
    q.add_argument('--spec', type=Path, required=True)
    q.add_argument('--author', required=True)
    q = task.add_parser('import-artifact')
    q.add_argument('id')
    q.add_argument('--html', type=Path, required=True)
    q.add_argument('--author', required=True)
    q.add_argument('--purpose', choices=['resume', 'intermediate'], required=True)
    q.add_argument('--sources', type=Path, help='JSON list of accepted relative source files')
    q = task.add_parser('progress')
    q.add_argument('id')
    q.add_argument('--state', required=True)
    q.add_argument('--note', required=True)
    q = task.add_parser('versions')
    q.add_argument('id')
    for name in ('export-spec', 'continue'):
        q = task.add_parser(name)
        q.add_argument('id')
        q.add_argument('--revision')
        q.add_argument('--refresh-materials', action='store_true')
        if name == 'continue':
            q.add_argument('--author', required=True)
    q = task.add_parser('configure')
    q.add_argument('id')
    q.add_argument('--spec', type=Path, required=True)
    q = commands.add_parser('convert')
    q.add_argument('id')
    q.add_argument('--html', type=Path, required=True)
    q.add_argument('--author', required=True)
    q.add_argument('--sources', type=Path)
    q.add_argument('--chrome')
    screen = commands.add_parser('screen').add_subparsers(dest='action', required=True)
    q = screen.add_parser('create')
    q.add_argument('id')
    q.add_argument('--jobs', type=Path, required=True, help='JSON array of current business job IDs')
    for name in ('record', 'compare'):
        q = screen.add_parser(name)
        q.add_argument('id')
        q.add_argument('--spec', type=Path, required=True)
    q = screen.add_parser('select')
    q.add_argument('id')
    q.add_argument('--jobs', type=Path, required=True)
    q.add_argument('--mode', choices=['parallel', 'ordered'], required=True)
    q.add_argument('--comparison')
    q.add_argument('--spec', type=Path, help='selection role, decision_text, dependencies')
    q = screen.add_parser('status')
    q.add_argument('id')
    q = screen.add_parser('brief')
    q.add_argument('id')
    q.add_argument('--materials', nargs='*', default=[])
    supplemental = commands.add_parser('supplement').add_subparsers(dest='action', required=True)
    for name in ('add', 'revise'):
        q = supplemental.add_parser(name)
        q.add_argument('id')
        q.add_argument('--spec', type=Path, required=True)
    for name in ('apply', 'show'):
        q = supplemental.add_parser(name)
        q.add_argument('id')
    record = commands.add_parser('record').add_subparsers(dest='action', required=True)
    for name in ('create', 'update'):
        q = record.add_parser(name)
        q.add_argument('id')
        q.add_argument('--spec', type=Path, required=True)
    q = record.add_parser('open')
    q.add_argument('id')
    record.add_parser('list')
    rendering = commands.add_parser('render').add_subparsers(dest='action', required=True)
    for name in ('html', 'pdf'):
        q = rendering.add_parser(name)
        q.add_argument('id')
        q.add_argument('--revision')
        q.add_argument('--chrome')
        if name == 'pdf':
            q.add_argument('--formal', action='store_true')
    q = commands.add_parser('review').add_subparsers(dest='action', required=True).add_parser('record')
    q.add_argument('id')
    q.add_argument('--revision', required=True)
    q.add_argument('--file', type=Path, required=True)
    q = commands.add_parser('deliver')
    q.add_argument('id')
    q.add_argument('--revision')
    q = commands.add_parser('binding')
    q.add_argument('id')
    q.add_argument('--revision')
    q.add_argument('--pdf', action='store_true')
    q = commands.add_parser('backup')
    q.add_argument('destination', type=Path)
    q = commands.add_parser('checkpoint')
    q.add_argument('destination', type=Path)
    q = commands.add_parser('restore')
    q.add_argument('snapshot', type=Path)
    q = commands.add_parser('resume')
    q.add_argument('--resolve', help='retained partial save ID')
    q.add_argument('--keep-current', action='store_true', help='explicitly accept current files and abandon the incomplete transaction')
    commands.add_parser('status')
    return p

def execute(a):
    if a.command == 'workspace' and a.action == 'init':
        if a.data_root is None:
            raise ValueError('workspace init requires explicit --data-root')
        return workspace.configure(a.workspace, a.data_root)
    root = workspace.resolve(a.data_root, a.workspace)
    if (root / '.restoring').exists() and a.command not in ('restore', 'resume'):
        raise ValueError('unfinished checkpoint restore; repeat restore with its original checkpoint')
    if a.command == 'workspace':
        if a.action == 'merge':
            return merge.import_saved(root, a.id, s.read_json(a.spec))
        return workspace.show(root)
    if a.command == 'material':
        if a.action == 'search':
            return materials.search(root, a.query, a.names)
        if a.action == 'reindex':
            return materials.reindex(root, a.names)
        if a.action == 'read':
            return materials.read_material(root, a.name)
        text = a.file.read_text(encoding='utf-8-sig')
        if a.action == 'init':
            return materials.initialize(root, a.name, text)
        return materials.write_material(root, a.name, a.base_revision, text)
    if a.command == 'legacy':
        if a.action == 'sources':
            rows = legacy.source_records(root)
            return rows if a.id is None else [row for row in rows if row['legacy_id'] == a.id]
        if a.action == 'inspect':
            return migration.inspect(a.source_root, a.registry)
        if a.action == 'migrate':
            return migration.import_workspace(root, a.id, s.read_json(a.spec))
        if a.action == 'query':
            return migration.query(root, a.id, a.category)
        return legacy.import_legacy(root, s.read_json(a.spec))
    if a.command == 'job':
        if a.action == 'list':
            return jobs.list_jobs(root)
        if a.action == 'update':
            return jobs.update(root, a.id, s.read_json(a.spec))
        if a.action == 'policy':
            return jobs.policy(root, a.id, s.read_json(a.spec))
        if a.action == 'policy-open':
            return s.read_json(s.path(root, f'岗位/共同条件/{jobs.business_id(a.id)}.json'))
        if a.action == 'ingest':
            return ingest.standardized(
                root, a.source, a.ids, a.id_prefix, a.actor, a.jd_status)
        if a.action == 'open':
            if '::' in a.id:
                from .source_workspaces import resolve
                source_root, local_id, _ = resolve(root, a.id)
                job = jobs.open_job(source_root, local_id)
                return {**job, 'handle': a.id, 'scope': 'historical',
                        'files': {'jd': str(s.path(source_root, job['jd'])) if job['jd'] else None,
                                  'notes': str(s.path(source_root, f'岗位/{local_id}/备注.md'))}}
            return jobs.open_job(root, a.id)
        if a.action == 'select':
            return jobs.select(root, a.ids, a.mode)
        if a.pool:
            return jobs.import_pool(root, a.pool)
        if not a.id or not a.jd:
            raise ValueError('job import needs --pool or --id + --jd')
        return jobs.import_job(root, a.id, a.jd, s.read_json(a.metadata) if a.metadata else {})
    if a.command == 'task':
        if a.action == 'inputs':
            return tasks.inputs(root, a.id, a.revision, a.from_task, a.reuse)
        if a.action == 'versions':
            return tasks.versions(root, a.id)
        if a.action in ('export-spec', 'continue'):
            if a.action == 'continue' and '::' in a.id:
                from .source_workspaces import continue_task
                return continue_task(root, a.id, a.revision, a.author, a.refresh_materials)
            spec = tasks.export_spec(root, a.id, a.revision, a.refresh_materials)
            return spec if a.action == 'export-spec' else tasks.save_revision(root, a.id, spec, a.author)
        if a.action == 'configure':
            return tasks.configure(root, a.id, s.read_json(a.spec))
        if a.action == 'create':
            return tasks.create_task(root, a.id, a.job, a.author, a.purpose, a.legacy_id)
        if a.action == 'open':
            return tasks.open_task(root, a.id)
        if a.action == 'save':
            return tasks.save_revision(root, a.id, s.read_json(a.spec), a.author)
        if a.action == 'import-artifact':
            return tasks.import_artifact(root, a.id, a.html, a.author, a.purpose, s.read_json(a.sources) if a.sources else [])
        return tasks.progress(root, a.id, a.state, a.note)
    if a.command == 'convert':
        return tasks.convert_html(root, a.id, a.html, a.author, s.read_json(a.sources) if a.sources else [], a.chrome)
    if a.command == 'screen':
        if a.action == 'create':
            return screening.create(root, a.id, s.read_json(a.jobs))
        if a.action == 'record':
            return screening.record(root, a.id, s.read_json(a.spec))
        if a.action == 'compare':
            return screening.compare(root, a.id, s.read_json(a.spec))
        if a.action == 'select':
            return screening.select(root, a.id, s.read_json(a.jobs), a.mode, a.comparison, s.read_json(a.spec) if a.spec else None)
        if a.action == 'brief':
            return screening.brief(root, a.id, a.materials)
        return screening.status(root, a.id)
    if a.command == 'supplement':
        if a.action == 'add':
            return supplements.add(root, a.id, s.read_json(a.spec))
        if a.action == 'apply':
            return supplements.apply(root, a.id)
        if a.action == 'revise':
            spec = s.read_json(a.spec)
            return supplements.revise(root, a.id, spec['base_revision'], spec['proposed_text'])
        return supplements.show(root, a.id)
    if a.command == 'record':
        if a.action == 'create':
            return records.create(root, a.id, s.read_json(a.spec))
        if a.action == 'update':
            return records.update(root, a.id, s.read_json(a.spec))
        if a.action == 'open':
            return records.show(root, a.id)
        return records.list_records(root)
    if a.command == 'render':
        if a.action == 'html':
            return render.render_html(root, a.id, a.revision, a.chrome)
        return render.render_pdf(root, a.id, a.revision, a.chrome, a.formal)
    if a.command == 'review':
        return delivery.record_review(root, a.id, a.revision, s.read_json(a.file))
    if a.command == 'deliver':
        return delivery.deliver(root, a.id, a.revision)
    if a.command == 'binding':
        return render.binding(root, a.id, a.revision, a.pdf)
    if a.command == 'backup':
        return recovery.backup(root, a.destination)
    if a.command == 'checkpoint':
        return recovery.checkpoint(root, a.destination)
    if a.command == 'restore':
        return recovery.restore(a.snapshot, root)
    if a.command == 'resume':
        if a.resolve:
            if not a.keep_current:
                raise ValueError('--resolve requires --keep-current; inspect retained before/after/current first')
            return s.resolve_conflict(root, a.resolve, 'keep-current')
        if a.keep_current:
            raise ValueError('--keep-current requires --resolve <partial-save-id>')
        return s.resume(root)
    if a.command == 'status':
        issues = s.conflicts(root)
        return {'data_root': str(root), 'tasks': [tasks.open_task(root, f.parent.name) for f in (root / '任务').glob('*/任务.json')], 'deliveries': [str(f.relative_to(root)) for f in (root / '交付').glob('*/*/登记.json')], 'save_conflicts': issues, 'status': 'conflict' if issues else 'ready'}
    raise ValueError('unknown command')

def main():
    a = parser().parse_args()
    try:
        result = execute(a)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and result.get('status') == 'conflict':
            return 3
        if isinstance(result, dict) and result.get('check', {}).get('failures'):
            return 4
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        error = {'error': str(exc), 'type': type(exc).__name__}
        if getattr(exc, '__notes__', None):
            error['notes'] = exc.__notes__
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2
