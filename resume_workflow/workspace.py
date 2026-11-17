"""Locate one current data root from the user's system directory."""
import os
from pathlib import Path
from . import storage as s


def config_path(explicit=None):
    return Path(explicit).absolute() if explicit else Path.cwd() / 'workspace.json'


def _data_root(root):
    # Check the supplied path before collapsing '..' so links cannot disappear.
    s.path(root, '')
    return Path(os.path.abspath(root))


def configure(config, root):
    config = config_path(config)
    if config.exists():
        raise FileExistsError('workspace configuration already exists: ' + str(config))
    root = _data_root(root)
    s.atomic(config, s.json_bytes({'data_root': str(root)}))
    return {'config': str(config), 'data_root': str(root)}


def resolve(explicit_root=None, config=None):
    if explicit_root is not None:
        root = explicit_root
    else:
        location = config_path(config)
        declared = s.read_json(location)['data_root']
        root = location.parent / declared
    return _data_root(root)


def quality_references(root):
    """Optional root-local page examples, never candidate facts or draft basis."""
    config = s.path(root, '参照/登记.json')
    if not config.exists():
        return []
    rows = s.read_json(config)
    if not isinstance(rows, list):
        raise ValueError(str(config) + ': reference list required')
    import pymupdf
    result = []
    for row in rows:
        if not isinstance(row, dict) or any(
                not isinstance(row.get(key), str) or not row[key].strip()
                for key in ('name', 'purpose', 'pdf', 'preview')):
            raise ValueError(str(config) + ': each reference requires name/purpose/pdf/preview strings')
        files = {}
        for key in ('pdf', 'preview'):
            file = s.path(root, row[key])
            if not file.is_file():
                raise FileNotFoundError('quality reference file missing: ' + str(file))
            try:
                if key == 'pdf':
                    with pymupdf.open(file) as document:
                        if not document.is_pdf or document.needs_pass or document.page_count < 1:
                            raise ValueError('readable, unencrypted PDF required')
                        document.load_page(0)
                else:
                    pymupdf.Pixmap(file)
            except (OSError, RuntimeError, ValueError, pymupdf.mupdf.FzErrorBase) as exc:
                raise ValueError('unreadable quality reference: ' + str(file) + ': ' + str(exc)) from exc
            files[key] = str(file)
        result.append({**row, **files})
    return result


def show(root):
    from . import materials, tasks, migration
    from .source_workspaces import historical_tasks
    references = quality_references(root)
    def artifacts(folder):
        return {name: str(folder / name) for name in ('resume.html', 'resume.pdf', 'html_preview.png', 'pdf_preview.png')
                if (folder / name).is_file()}

    opened = []
    for p in s.path(root, '任务').glob('*/任务.json'):
        try:
            task = tasks.open_task(root, p.parent.name)
        except FileNotFoundError as exc:
            task = tasks.load(root, p.parent.name)
            task.update(status='unavailable', error=str(exc),
                        needs_review=[{'missing_path': exc.filename}])
        if task['current_revision']:
            folder = s.path(root, tasks.revision_path(task['id'], task['current_revision']))
            if (folder / '作品.json').is_file():
                task['artifacts'] = artifacts(folder)
        opened.append(task)
    return {
        'data_root': str(root),
        'references': references,
        'materials': {name: str(s.path(root, materials.rel(name))) for name in ('背景', '兴趣', '偏好')},
        'tasks': opened,
        'historical_tasks': historical_tasks(root),
        'deliveries': [{'registration': str(p), 'artifacts': artifacts(p.parent)}
                       for p in s.path(root, '交付').glob('*/*/登记.json')],
        'legacy_roots': [str(p) for p in root.iterdir() if p.is_dir() and p.name in migration.ROOTS] if root.exists() else [],
    }
