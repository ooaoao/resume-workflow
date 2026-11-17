"""Run the normal CLI in a new directory with fictional inputs and user-supplied fonts."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import pymupdf

CASES = Path(__file__).resolve().parent
ACTOR = 'synthetic-example'


def call(*args):
    cp = subprocess.run(
        [sys.executable, '-X', 'utf8', '-B', '-m', 'resume_workflow', *map(str, args)],
        cwd=CASES.parent, capture_output=True, text=True, encoding='utf8',
    )
    if cp.returncode:
        details = cp.stderr.strip()
        if cp.stdout:
            try:
                output = json.loads(cp.stdout)
                details += '\n' + str(output.get('check', output).get('failures', ''))
            except (ValueError, AttributeError):
                details += '\n' + cp.stdout
        raise SystemExit(f'{args}: exit {cp.returncode}\n{details}')
    return json.loads(cp.stdout)


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=('operations', 'data'), default='operations')
    parser.add_argument('--fonts', required=True, type=Path, help='LXGW WenKai GB Lite static Regular and Medium TTFs')
    parser.add_argument('--run-root', required=True, type=Path, help='a directory that does not already exist')
    parser.add_argument('--chrome', type=Path, help='optional installed Edge/Chrome executable')
    args = parser.parse_args()
    source = CASES / args.case
    fonts = {key: (args.fonts / name).resolve(strict=True) for key, name in (
        ('font_w04', 'LXGWWenKaiGBLite-Regular.ttf'), ('font_w05', 'LXGWWenKaiGBLite-Medium.ttf'))}
    chrome = ['--chrome', args.chrome.resolve(strict=True)] if args.chrome else []
    root = args.run_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    data, cfg, inputs = root / 'data', root / 'workspace.json', root / 'inputs'
    inputs.mkdir()
    call('--workspace', cfg, '--data-root', data, 'workspace', 'init')

    def run(*command):
        return call('--workspace', cfg, *command)

    for name in ('背景', '兴趣', '偏好'):
        run('material', 'init', name, '--file', source / (name + '.md'))
    origins = run('legacy', 'import', '--spec', write_json(inputs / 'sources.json', [
        {'path': str(source / '背景.md'), 'id': 'fictional-facts', 'role': 'synthetic-facts'}
    ]))
    fact_path = origins[0]['path']
    case = json.loads((source / 'example.json').read_text(encoding='utf8'))
    job = case['job']
    job_id = 'fictional-' + args.case
    run('job', 'import', '--id', job_id, '--jd', source / '岗位.md',
        '--metadata', write_json(inputs / 'job.json', {**job, 'jd_status': '全文', 'simulation_only': True}))
    scope = write_json(inputs / 'scope.json', [job_id])
    run('screen', 'create', 'fictional-case', '--jobs', scope)
    run('screen', 'brief', 'fictional-case', '--materials', '背景', '兴趣', '偏好')

    education = case['education']
    quote = education.pop('quote')
    requirement = {
        'requirement_id': 'degree', 'requirement_type': 'mandatory',
        'applicability': 'applicable', 'candidate_status': 'confirmed_met',
        'reason': '示例的教育原文明确记载本科。',
        'jd_ref': {'kind': 'job', 'id': job_id, 'quote': job['requirement_quote']},
        'fact_ref': {'kind': 'source', 'path': fact_path, 'quote': quote},
    }
    rows = run('screen', 'record', 'fictional-case', '--spec', write_json(inputs / 'screen.json', {
        'stage': 'qualification', 'actor': ACTOR, 'items': [{
            'job_id': job_id, 'requirements': [requirement], 'logic': {'ref': 'degree'}, 'coverage': 'complete',
        }],
    }))
    if rows[0]['gate'] != 'PASS':
        raise RuntimeError(f"Fictional eligibility readback: {rows[0]['gate']}")
    run('screen', 'select', 'fictional-case', '--jobs', scope, '--mode', 'parallel', '--spec',
        write_json(inputs / 'selection.json', {'role': 'demo-shortlist', 'decision_text': case['selection_notes']}))
    task = 'fictional-resume'
    run('task', 'create', task, '--job', job_id, '--author', ACTOR)
    run('task', 'inputs', task)
    photo = inputs / 'placeholder.jpg'
    with pymupdf.open() as doc:
        page = doc.new_page(width=90, height=110)
        page.draw_rect(page.rect, color=(.4, .5, .57), fill=(.86, .9, .93), width=2)
        page.insert_text((12, 61), 'SAMPLE', fontsize=14, color=(.26, .36, .43))
        photo.write_bytes(page.get_pixmap().tobytes('jpeg'))
    spec = {
        'content': {
            **case['content'], 'resource_purpose': 'synthetic',
            'resources': {**{k: str(v) for k, v in fonts.items()}, 'photo': str(photo)},
        },
        'config': case['config'], 'materials': ['背景', '兴趣', '偏好'], 'sources': [fact_path],
        'selection_notes': case['selection_notes'],
        'education': [{**education, 'source': {'path': fact_path, 'quote': quote}}],
    }
    saved = run('task', 'save', task, '--author', ACTOR, '--spec', write_json(inputs / 'draft.json', spec))
    print('Saved fictional draft; rendering real A4 HTML and PDF', file=sys.stderr, flush=True)
    html = run('render', 'html', task, *chrome)
    pdf = run('render', 'pdf', task, *chrome)
    run('record', 'create', 'fictional-application', '--spec', write_json(inputs / 'record.json', {
        'kind': 'application', 'actor': ACTOR, 'data': {
            'state': 'prepared', 'simulation_only': True,
            'job_id': job_id, 'task_id': task, 'revision': saved['revision'],
        },
    }))

    checkpoint, restored = root / 'checkpoint', root / 'restored'
    run('checkpoint', checkpoint)
    call('--data-root', restored, 'restore', checkpoint)
    restored_cfg = root / 'restored.workspace.json'
    call('--workspace', restored_cfg, '--data-root', restored, 'workspace', 'init')
    def continue_run(*command):
        return call('--workspace', restored_cfg, *command)

    opened = continue_run('task', 'open', task)
    record = continue_run('record', 'open', 'fictional-application')
    if opened['current_revision'] != saved['revision'] or record['data']['state'] != 'prepared':
        raise RuntimeError('Restored task or practice application does not match the saved state')
    next_task = continue_run('task', 'continue', task, '--revision', saved['revision'], '--author', ACTOR)
    preview = Path(pdf['path']) / 'pdf_preview.png'
    page_file = Path(pdf['path']) / 'resume.pdf'
    with pymupdf.open(page_file) as output:
        pages = len(output)
        a4_points = [round(output[0].rect.width, 2), round(output[0].rect.height, 2)]
    result = {
        'case': args.case, 'workspace': str(cfg), 'qualification': rows[0]['gate'],
        'revision': saved['revision'], 'html': str(Path(html['path']) / 'resume.html'),
        'pdf': str(page_file), 'preview': str(preview), 'pages': pages, 'page_points': a4_points,
        'html_failures': html['check']['failures'], 'pdf_failures': pdf['check']['failures'],
        'application_state': record['data']['state'], 'restored_workspace': str(restored_cfg),
        'continued_revision': next_task['revision'],
        'scope': 'fictional local practice; no site, submission, review, or official delivery',
    }
    write_json(root / 'result.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
