"""Complete copies are published once; failed staging never becomes a snapshot."""
import os
import shutil
import stat as stat_module
from pathlib import Path
from . import storage as s

SNAPSHOT = {'format': 'resume-workflow-snapshot/1', 'complete': True}


def same_file(left, right):
    if not s.is_file(right) or s.stat_path(left).st_size != s.stat_path(right).st_size:
        return False
    with s.open_file(left, 'rb') as a, s.open_file(right, 'rb') as b:
        while chunk := a.read(1024 * 1024):
            if chunk != b.read(len(chunk)):
                return False
    return True


def copy_file(source, destination):
    """Bounded memory; verify a transfer before publishing this one file."""
    stamp = s.stat_path(source)
    os.makedirs(s._io_path(destination.parent), exist_ok=True)
    temporary = destination.with_name('.' + destination.name + '.' + s.uid() + '.tmp')
    try:
        with s.open_file(source, 'rb') as src, s.open_file(temporary, 'xb') as dst:
            shutil.copyfileobj(src, dst)
        after = s.stat_path(source)
        if (stamp.st_size, stamp.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or not same_file(source, temporary):
            raise s.Conflict('source changed or copy differs: ' + str(source))
        os.replace(s._io_path(temporary), s._io_path(destination))
    finally:
        try:
            os.unlink(s._io_path(temporary))
        except FileNotFoundError:
            pass


def _checkpoint_source_root(source):
    source = Path(source).absolute()
    if not s.is_dir(source):
        raise FileNotFoundError('source directory does not exist: ' + str(source))
    s.path(source, '')
    if s.exists(source / '.copying'):
        raise ValueError('incomplete copy cannot be consumed: ' + str(source))
    return source


def _walk_tree(root):
    def walk(relative):
        directory = s.path(root, relative.as_posix())
        with s.scandir(directory) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
        directories = []
        filenames = []
        for entry in entries:
            child_relative = relative / entry.name
            child = s.path(root, child_relative.as_posix())
            metadata = s.stat_path(child, follow_symlinks=False)
            if stat_module.S_ISDIR(metadata.st_mode):
                directories.append(entry.name)
            else:
                filenames.append(entry.name)
        yield relative, directories, filenames
        for name in directories:
            yield from walk(relative / name)
    yield from walk(Path())


def _direct_children(root, relative):
    relative = Path(relative)
    directory = s.path(root, relative.as_posix())
    if not s.exists(directory):
        return []
    with s.scandir(directory) as scan:
        entries = sorted(scan, key=lambda entry: entry.name)
    return [s.path(root, (relative / entry.name).as_posix()) for entry in entries]


def _source_records(payload, files):
    rows = []
    index = '来源/来源关系.json'
    if index in files:
        rows.extend(s.read_json(payload / index))
    for relative in files:
        parts = Path(relative).parts
        if len(parts) == 4 and parts[:2] == ('来源', '关系') and parts[-1].endswith('.json'):
            rows.append(s.read_json(payload / relative))
    return rows


def _require_finished_saves(root):
    for name in ('.pending', '.preparing'):
        folder = s.path(root, name)
        if folder.exists() and any(folder.iterdir()):
            raise ValueError('unfinished save; run resume first')


def _ready(root):
    _require_finished_saves(root)
    if s.conflicts(root) or s.path(root, '.restoring').exists():
        raise ValueError('resolve unfinished save/restore before backup')


def _baseline_roots(root):
    from . import migration
    from .source_workspaces import catalog
    prefixes = {''}
    for folder in _direct_children(root, '迁入'):
        p = s.path(root, (folder.relative_to(root) / '迁入.json').as_posix())
        if not s.is_file(p):
            continue
        row = s.read_json(p)
        if row.get('mode') == 'in-place':
            prefixes.add(row['source_prefix'])
    return sorted({(Path(prefix) / name).as_posix() for prefix in prefixes for name in migration.ROOTS
                   if s.is_dir(s.path(root, str(Path(prefix) / name)))}
                  | {row['data_path'] for row in catalog(root)})


def _references(value):
    if isinstance(value, dict):
        if value.get('kind') == 'source' and 'path' in value:
            yield value['path']
        if value.get('jd_source') == 'root-original' and value.get('jd'):
            yield value['jd']
        for child in value.values():
            yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def checkpoint(root, destination):
    """Save current workflow state; historical originals stay in a named baseline."""
    root = _checkpoint_source_root(root)
    destination = Path(destination).absolute()
    s.path(destination, '')
    if root == destination or root in destination.parents or destination in root.parents:
        raise ValueError('checkpoint and data roots must be disjoint')
    if s.exists(destination):
        raise FileExistsError(destination)
    with s.lock(root):
        _ready(root)
        from .source_workspaces import catalog, baseline_missing
        origins = catalog(root)
        registered = {s.path(root, row['data_path']) for row in origins}
        for folder in _direct_children(root, '来源保存'):
            data = s.path(root, (folder.relative_to(root) / 'data').as_posix())
            if s.is_dir(data) and data not in registered:
                raise ValueError('unfinished preserved source; retry its workspace merge before checkpoint: ' + str(data))
        missing = [name for row in origins for name in baseline_missing(root, row)]
        if missing:
            raise ValueError('required historical baseline missing; affected paths: ' + str(missing))
        baselines = _baseline_roots(root)
        excluded = [s.path(root, p) for p in baselines]
        files = []
        refs = set()
        # Leave historical baseline directories outside the checkpoint payload.
        for relative_folder, names, filenames in _walk_tree(root):
            folder = root / relative_folder
            # Failed source-copy staging is diagnostic, never admitted history
            # or new workflow state to duplicate into a scoped checkpoint.
            names[:] = [name for name in names if folder / name not in excluded
                        and not (len(folder.relative_to(root).parts) == 2
                                 and folder.relative_to(root).parts[0] == '来源保存'
                                 and name.startswith('.data.copy-'))]
            for name in filenames:
                p = s.path(root, str((folder / name).relative_to(root)))
                if p == root / '.save.lock' or p == root / '.snapshot.json':
                    continue
                files.append(p.relative_to(root).as_posix())
                if p.suffix == '.json' and not p.is_relative_to(root / '来源'):
                    refs.update(_references(s.read_json(p)))
        captured = []
        for relative in sorted(refs):
            p = s.path(root, relative)
            if any(p.is_relative_to(base) for base in excluded):
                if not s.is_file(p):
                    raise FileNotFoundError('required source missing: ' + relative)
                captured.append(relative)
        staging = destination.with_name('.' + destination.name + '.checkpoint-' + s.uid())
        os.makedirs(s._io_path(staging), exist_ok=True)
        s.atomic(staging / '.copying', b'checkpoint not published\n')
        count = 0
        total = 0
        try:
            for relative in sorted(set(files + captured)):
                source = s.path(root, relative)
                copy_file(source, s.path(staging / 'data', relative))
                total += s.stat_path(source).st_size
                count += 1
            descriptor = {'format': 'resume-workflow-checkpoint/1', 'complete': True,
                          'baseline_roots': baselines, 'captured_sources': captured,
                          'coverage': 'saved workflow state and cited originals; remaining historical originals require the existing baseline',
                          'files': count, 'bytes': total}
            s.atomic(staging / '.checkpoint.json', s.json_bytes(descriptor))
            os.unlink(s._io_path(staging / '.copying'))
            os.replace(s._io_path(staging), s._io_path(destination))
        except (OSError, ValueError) as exc:
            exc.add_note('incomplete checkpoint retained at ' + str(staging))
            raise
    return {'root': str(destination), **descriptor}


def restore_checkpoint(snapshot, root):
    from . import legacy
    from .source_workspaces import catalog, baseline_missing
    snapshot = _checkpoint_source_root(snapshot)
    descriptor = s.read_json(snapshot / '.checkpoint.json')
    if descriptor.get('format') != 'resume-workflow-checkpoint/1' or descriptor.get('complete') is not True:
        raise ValueError('completed checkpoint required')
    root = Path(root).absolute()
    s.path(root, '')
    if root == snapshot or root in snapshot.parents or snapshot in root.parents:
        raise ValueError('checkpoint and data roots must be disjoint')
    payload = snapshot / 'data'
    files = sorted((relative / name).as_posix()
                   for relative, _, filenames in _walk_tree(payload) for name in filenames)
    if len(files) != descriptor['files'] or sum(s.stat_path(s.path(payload, p)).st_size for p in files) != descriptor['bytes']:
        raise ValueError('checkpoint payload incomplete; retain current data')
    missing = [relative for relative in descriptor['baseline_roots'] if not s.is_dir(s.path(root, relative))]
    for row in catalog(payload):
        missing.extend(baseline_missing(root, row))
    for row in _source_records(payload, files):
        if row.get('mode') == 'in-place' and not legacy.runtime_path(Path(row['path'])):
            relative = row['path']
            if not s.is_file(s.path(root, relative)) and not s.is_file(s.path(payload, relative)):
                missing.append(relative)
    if missing:
        raise ValueError('required historical baseline missing; affected paths: ' + str(missing))
    with s.lock(root):
        marker = s.path(root, '.restoring')
        if marker.exists() and s.read_json(marker)['snapshot'] != str(snapshot):
            raise ValueError('finish the existing checkpoint restore first')
        _require_finished_saves(root)
        if s.conflicts(root):
            raise ValueError('resolve unfinished save conflicts before restoring')
        # Preflight all collisions before modifying saved data or baseline files.
        conflicts = [relative for relative in files if s.exists(s.path(root, relative))
                     and not same_file(s.path(payload, relative), s.path(root, relative))]
        if conflicts:
            raise s.Conflict('restore would overwrite current data/baseline: ' + str(conflicts))
        s.atomic(marker, s.json_bytes({'snapshot': str(snapshot), 'state': 'restoring'}))
        restored = 0
        for relative in files:
            destination = s.path(root, relative)
            if not s.exists(destination):
                s.commit(root, {relative: s.read_bytes(s.path(payload, relative))}, {relative: None})
                restored += 1
        os.unlink(s._io_path(marker))
    return {'root': str(root), 'restored_files': restored,
            'baseline_roots': descriptor['baseline_roots'], 'coverage': descriptor['coverage'], 'status': 'restored'}


def _source_root(source):
    source = Path(source).absolute()
    if not source.is_dir():
        raise FileNotFoundError('source directory does not exist: ' + str(source))
    s.path(source, '')
    if (source / '.copying').exists():
        raise ValueError('incomplete copy cannot be consumed: ' + str(source))
    return source


def _copy(source, destination, *, snapshot):
    source = _source_root(source)
    destination = Path(destination).absolute()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('backup/restore roots must be disjoint')
    s.path(destination, '')
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError('empty destination required: ' + str(destination))
    files = sorted(p for p in source.rglob('*')
                   if p not in (source / '.save.lock', source / '.snapshot.json'))
    for p in files:
        s.path(source, str(p.relative_to(source)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name('.' + destination.name + '.copy-' + s.uid())
    staging.mkdir()
    s.atomic(staging / '.copying', b'copy not published\n')
    try:
        for p in files:
            dest = s.path(staging, str(p.relative_to(source)))
            if p.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, dest)
                if not same_file(p, dest):
                    raise OSError('copy differs from saved source: ' + str(p))
        if snapshot:
            # Completion flag only: no file inventory or integrity manifest.
            s.atomic(staging / '.snapshot.json', s.json_bytes(SNAPSHOT))
        else:
            state = s.resume(staging)
            if state['conflicts']:
                raise ValueError('snapshot contains unresolved partial saves')
        (staging / '.copying').unlink()
        if destination.exists():
            # Reject a target that became nonempty during copying.
            destination.rmdir()
        os.replace(staging, destination)
    except (OSError, ValueError) as exc:
        exc.add_note('copy staging retained at ' + str(staging))
        raise
    return {'root': str(destination), 'files': sum(p.is_file() for p in files)}


def backup(root, destination):
    root = _source_root(root)
    with s.lock(root):
        _ready(root)
        for name in ('.pending', '.preparing'):
            pending = s.path(root, name)
            if pending.exists() and any(pending.iterdir()):
                raise ValueError('resume unfinished save before backup')
        if s.conflicts(root):
            raise ValueError('resolve partial saves before creating a complete snapshot')
        return _copy(root, destination, snapshot=True)


def restore(snapshot, fresh_root):
    snapshot = Path(snapshot).absolute()
    if s.is_file(snapshot / '.checkpoint.json'):
        return restore_checkpoint(snapshot, fresh_root)
    snapshot = _source_root(snapshot)
    marker = snapshot / '.snapshot.json'
    if not marker.is_file() or s.read_json(marker) != SNAPSHOT:
        raise ValueError('completed workflow snapshot required: ' + str(snapshot))
    return _copy(snapshot, fresh_root, snapshot=False)
