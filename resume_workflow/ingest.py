"""Import a reviewed standardized job package without changing its originals."""
import re
from pathlib import Path

import yaml
from yaml.constructor import ConstructorError

from . import jobs, storage as s


class UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ConstructorError(
                'mapping', node.start_mark, f'duplicate key {key}',
                key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _front_matter(path):
    text = path.read_text(encoding='utf-8-sig')
    if not text.startswith('---\n'):
        raise ValueError(f'{path}: YAML front matter required')
    parts = text.split('---\n', 2)
    if len(parts) != 3:
        raise ValueError(f'{path}: unterminated YAML front matter')
    data = yaml.load(parts[1], Loader=UniqueLoader)
    if not isinstance(data, dict):
        raise ValueError(f'{path}: front matter must be a mapping')
    return data


def _normalize(value):
    return re.sub(r'\s+', '', str(value))


def _validate(path, data):
    required = (
        'job_id', 'job_title', 'company', 'source_url',
        'raw_description', 'source_visible_text',
    )
    missing = [key for key in required if not isinstance(data.get(key), str) or not data[key].strip()]
    if missing:
        raise ValueError(f'{path}: missing required text fields: {", ".join(missing)}')
    if data['raw_description'] not in data['source_visible_text']:
        if _normalize(data['raw_description']) not in _normalize(data['source_visible_text']):
            raise ValueError(f'{path}: raw_description is not present in source_visible_text')
    if not data['source_url'].startswith('https://'):
        raise ValueError(f'{path}: HTTPS source_url required')


def _identities(source, ids_file):
    files = sorted((source / 'jobs').glob('*.md'))
    if not files:
        raise ValueError(f'{source}: jobs/*.md not found')
    records = []
    seen = set()
    for path in files:
        data = _front_matter(path)
        _validate(path, data)
        if data['job_id'] in seen:
            raise ValueError(f'{path}: duplicate job_id {data["job_id"]}')
        seen.add(data['job_id'])
        records.append((path, data))
    if ids_file is None:
        return records
    selected = s.read_json(ids_file)
    if not isinstance(selected, list) or not all(isinstance(item, str) and item for item in selected):
        raise ValueError(f'{ids_file}: JSON array of job_id strings required')
    by_id = {data['job_id']: (path, data) for path, data in records}
    missing = [job_id for job_id in selected if job_id not in by_id]
    if missing:
        raise ValueError(f'{ids_file}: unknown job_id(s): {", ".join(missing)}')
    return [by_id[job_id] for job_id in selected]


def standardized(root, source, ids_file=None, id_prefix='', actor='standardized-ingest',
                 jd_status='全文'):
    """Import standardized Markdown jobs and retain every source field in legacy."""
    if not actor:
        raise ValueError('ingest actor required')
    source = Path(source).absolute()
    if not source.is_dir():
        raise ValueError(f'{source}: standardized source directory required')
    root = Path(root).absolute()
    imported = []
    for path, data in _identities(source, ids_file):
        job_id = id_prefix + data['job_id']
        existed = s.path(root, f'岗位/{job_id}/岗位.json').exists()
        metadata = dict(data)
        metadata.update({
            'code': data['job_id'],
            'eligibility': 'UNKNOWN',
            'jd_status': jd_status,
            'cohort': data.get('cohort_year'),
            'batch_status': data.get('campaign_status', 'unknown'),
            'ingest': {
                'pipeline': 'standardized-package',
                'actor': actor,
                'source_package': str(source),
                'source_file': str(path),
                'source_format': 'YAML front matter + Markdown',
                'source_identity_field': 'job_id',
                'jd_status_basis': (
                    'source_visible_text and raw_description from the standardized capture; '
                    'hidden or official webpage completeness is not independently asserted'),
                'structured_field_status': (
                    'machine-extracted fields remain prompts and require semantic review '
                    'against raw_description/source_visible_text'),
            },
        })
        row = jobs.import_job(
            root, job_id, path, metadata, resume_existing=True)
        imported.append({
            'id': job_id,
            'source_job_id': data['job_id'],
            'title': data['job_title'],
            'cohort_year': data.get('cohort_year'),
            'campaign_status': data.get('campaign_status'),
            'locations': data.get('locations'),
            'existed': existed,
            'jd': row['jd'],
            'jd_status': row['jd_status'],
        })
    return {
        'source': str(source),
        'id_prefix': id_prefix,
        'count': len(imported),
        'created': sum(not row['existed'] for row in imported),
        'reused': sum(row['existed'] for row in imported),
        'jobs': imported,
    }
