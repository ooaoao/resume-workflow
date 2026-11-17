"""Source-bound personal A4 rendering. Machine checks never assert prose quality."""
import hashlib
from collections import Counter
import json
import re
import shutil
import subprocess
from pathlib import Path
import pymupdf
from . import storage as s, tasks, presentation as p
PERSONAL_FONTS = {'font_w04': 'TsangerJinKai02 W04', 'font_w05': 'TsangerJinKai02 W05'}

def digest(data):
    return hashlib.sha256(data).hexdigest()

def validate_font_names(names, purpose):
    if purpose not in ('personal', 'synthetic'):
        raise ValueError('resource_purpose must be personal/synthetic')
    if purpose == 'personal':
        for key, expected in PERSONAL_FONTS.items():
            if p._font_identity_norm(names[key]) != p._font_identity_norm(expected):
                raise ValueError('font identity mismatch: ' + key)

def font_failures(dom, expected):
    return p.platform_font_failures(dom, expected)

def validate_passive(raw):
    parsed = p.PassiveMarkup()
    parsed.feed(raw)
    parsed.close()
    if parsed.section:
        raise ValueError('unclosed HTML style/script')
    for url in parsed.urls:
        if not re.fullmatch('data:(font/(ttf|otf)|image/(jpeg|png));base64,[A-Za-z0-9+/=]+', url):
            raise ValueError('HTML must embed local fonts/images; external resource forbidden')

def _education(folder, work):
    rows = []
    for row in work.get('education', []):
        source = row.get('source', {})
        source_path = source.get('path')
        quote = source.get('quote')
        snapshots = {ref['source']: ref['snapshot'] for ref in work['sources']}
        if source_path not in snapshots or not quote:
            raise ValueError('education source path/quote required')
        text = s.path(folder, snapshots[source_path]).read_text(encoding='utf-8-sig')
        if quote not in text:
            raise ValueError('education quote absent from saved source: ' + source_path)
        for key in ('school_id', 'school', 'major', 'date', 'degree'):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError('education field missing: ' + key)
        if len(row.get('notes', [])) > 3:
            raise ValueError('education notes: at most three factual notes')
        rows.append({**row, 'badge': row.get('badge', ''), 'notes': row.get('notes', [])})
    return rows

def _chrome(explicit=None):
    candidates = [explicit] if explicit else [shutil.which('chrome'), 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe']
    for value in candidates:
        if value and Path(value).is_file():
            return str(Path(value).absolute())
    raise FileNotFoundError('Chromium not found; supply --chrome')

def _backend(folder, stage, chrome=None):
    node = shutil.which('node')
    if not node:
        raise FileNotFoundError('Node.js required')
    result = subprocess.run([node, str(Path(__file__).parent / 'renderer/render.mjs'), str(folder / 'resume.html'), str(folder), _chrome(chrome), stage], capture_output=True, text=True, encoding='utf8', timeout=115)
    s.atomic(folder / (stage + '_stage.log'), (result.stdout + '\n' + result.stderr).encode('utf8'))
    if result.returncode:
        raise RuntimeError('renderer failed; see ' + str(folder / (stage + '_stage.log')))
    if stage == 'html':
        probe = folder / '.html-pagination.pdf'
        with pymupdf.open(probe) as document:
            pagination = {'pages': len(document), 'pageSizesPt': [list(page.rect) for page in document],
                          'source': 'Chromium Page.printToPDF with the PDF-stage options; measurement only'}
        dom = s.read_json(folder / 'html_dom.json')
        dom['printPagination'] = pagination
        s.atomic(folder / 'html_dom.json', s.json_bytes(dom))
        probe.unlink()

def _basis(folder):
    files = ['作品.json', 'resume.html', 'html_dom.json', 'html_preview.png', 'html_check.json']
    files += [str(f.relative_to(folder)).replace('\\', '/') for sub in ('依据', '资源') if (folder / sub).exists() for f in (folder / sub).rglob('*') if f.is_file()]
    files += [name for name in ('content.json', 'config.json') if (folder / name).exists()]
    return {name: digest(s.path(folder, name).read_bytes()) for name in sorted(files)}

def binding(root, task_id, revision=None, pdf=False):
    from .source_workspaces import resolve
    _, local_id, _ = resolve(root, task_id)
    folder, work = tasks.get_revision(root, task_id, revision)
    result = {'task': local_id, 'revision': work['revision'], 'files': _basis(folder)}
    if pdf:
        for name in ('resume.pdf', 'pdf_check.json', 'pdf_dom.json', 'pdf_preview.png'):
            result['files'][name] = digest((folder / name).read_bytes())
    return result

def _check_dom(folder, dom, expected):
    failures = font_failures(dom, expected)
    if dom['overflow'] or dom['clipping']:
        failures.append('A4 content overflow/clipping')
    if dom['bodyHeight'] > dom['page']['printArea']['height'] + 1:
        failures.append('A4 content exceeds one print area')
    if 'printPagination' in dom and dom['printPagination']['pages'] != 1:
        failures.append('HTML print pagination must have one A4 page')
    if any((not i['complete'] or not i['naturalWidth'] for i in dom['images'])):
        failures.append('image incomplete')
    return {'failures': failures, 'font_names': expected, 'scope': 'technical print area only; content and actual page await independent review'}

def render_html(root, task_id, revision=None, chrome=None):
    from .source_workspaces import require_current
    require_current(task_id)
    with s.lock(root):
        folder, work = tasks.get_revision(root, task_id, revision)
        if (folder / 'html_binding.json').exists():
            saved = s.read_json(folder / 'html_binding.json')
            if binding(root, task_id, work['revision']) != saved:
                raise ValueError('rendered work changed; save/import a new revision')
            return {'path': str(folder), 'binding': saved, 'check': s.read_json(folder / 'html_check.json')}
        if work['kind'] == 'structured':
            content = s.read_json(folder / 'content.json')
            config = s.read_json(folder / 'config.json')
            if content.get('education_record_ids') and (not work.get('education')):
                raise ValueError('explicit education rows bound to accepted source required')
            resources = content.get('resources', {})
            if set(resources) != {'font_w04', 'font_w05', 'photo'}:
                raise ValueError('explicit font_w04/font_w05/photo required')
            for key, rel in resources.items():
                content['resources'][key] = str(s.path(folder, rel))
            names = {key: pymupdf.Font(fontbuffer=Path(content['resources'][key]).read_bytes()).name for key in ('font_w04', 'font_w05')}
            validate_font_names(names, content.get('resource_purpose', 'personal'))
            raw, _, _, _, _ = p.generated_document(content, config, folder, preview=True, education=_education(folder, work))
            s.atomic(folder / 'resume.html', raw.encode('utf8'))
            expected = {400: names['font_w04'], 500: names['font_w05']}
        else:
            raw = (folder / 'resume.html').read_text(encoding='utf-8-sig')
            from base64 import b64decode
            fonts = re.findall('data:font/(?:ttf|otf);base64,([A-Za-z0-9+/=]+)', raw)
            if len(fonts) != 2:
                raise ValueError('imported HTML needs two embedded fonts for actual identity checks')
            names = dict(zip(('font_w04', 'font_w05'), [pymupdf.Font(fontbuffer=b64decode(b)).name for b in fonts]))
            validate_font_names(names, 'personal' if work['purpose'] == 'resume' else 'synthetic')
            expected = {400: names['font_w04'], 500: names['font_w05']}
        validate_passive(raw)
        _backend(folder, 'html', chrome)
        dom = s.read_json(folder / 'html_dom.json')
        check = _check_dom(folder, dom, expected)
        s.atomic(folder / 'html_check.json', s.json_bytes(check))
        bound = binding(root, task_id, work['revision'])
        s.atomic(folder / 'html_binding.json', s.json_bytes(bound))
        return {'path': str(folder), 'binding': bound, 'check': check}

def render_pdf(root, task_id, revision=None, chrome=None, formal=False):
    from .source_workspaces import require_current
    require_current(task_id)
    from . import delivery
    with s.lock(root):
        if formal and tasks.load(root, task_id)['job_id'] is None:
            raise ValueError('formal resume PDF requires actual job/JD')
        folder, work = tasks.get_revision(root, task_id, revision)
        current = binding(root, task_id, work['revision'])
        if s.read_json(folder / 'html_binding.json') != current:
            raise ValueError('HTML/source changed after page rendering')
        check = s.read_json(folder / 'html_check.json')
        if check['failures']:
            raise ValueError('HTML technical checks failed: ' + '; '.join(check['failures']))
        if formal:
            delivery.require_review(root, task_id, work['revision'], 'content-page', current)
        if (folder / 'pdf_binding.json').exists():
            saved = s.read_json(folder / 'pdf_binding.json')
            if saved != binding(root, task_id, work['revision'], pdf=True):
                raise ValueError('PDF changed; new revision required')
            return {
                'path': str(folder),
                'binding': saved,
                'check': s.read_json(folder / 'pdf_check.json'),
                'purpose': 'formal-candidate' if formal else 'intermediate; not delivered',
            }
        before = s.read_json(folder / 'html_dom.json')
        if 'visibleText' not in before:
            raise ValueError('HTML lacks visible layout text evidence; save/import a new revision and render HTML before PDF')
        _backend(folder, 'pdf', chrome)
        dom = s.read_json(folder / 'pdf_dom.json')
        expected = {int(k): v for k, v in check['font_names'].items()}
        result = _check_dom(folder, dom, expected)
        if dom['text'] != before['text'] or dom['nodes'] != before['nodes'] or dom['visibleText'] != before['visibleText']:
            result['failures'].append('PDF-stage DOM differs from reviewed HTML stage')
        with pymupdf.open(folder / 'resume.pdf') as doc:
            result['pages'] = len(doc)
            if len(doc) != 1:
                result['failures'].append('PDF must have one A4 page')
            # Geometric sorting can reorder inline text with differing glyph
            # metrics. Preserve PDF stream order for contiguous-node checks.
            text = ''.join((page.get_text(sort=False) for page in doc))
            norm = lambda value: re.sub('\\s+', '', value)
            pdf_text = norm(text)
            if Counter(norm(dom['visibleText'])) != Counter(pdf_text):
                result['failures'].append('PDF complete visible text differs (including mixed emphasis text)')
            for node in dom['nodes']:
                if norm(node['text']) not in pdf_text:
                    result['failures'].append('PDF missing visible text: ' + node['text'][:100])
            entries = []
            for page in doc:
                if abs(page.rect.width - 595.28) > 1 or abs(page.rect.height - 841.89) > 1:
                    result['failures'].append('PDF page size is not A4')
                for font in page.get_fonts(full=True):
                    name, extension, kind, data = doc.extract_font(font[0])
                    entries.append({'name': name, 'bytes': len(data), 'buf': data})
            result['failures'] += p._pdf_font_identity_failures(entries, expected)
            doc[0].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).save(folder / 'pdf_preview.png')
        s.atomic(folder / 'pdf_text.txt', text.encode('utf8'))
        s.atomic(folder / 'pdf_check.json', s.json_bytes(result))
        bound = binding(root, task_id, work['revision'], pdf=True)
        s.atomic(folder / 'pdf_binding.json', s.json_bytes(bound))
        return {'path': str(folder), 'binding': bound, 'check': result, 'purpose': 'formal-candidate' if formal else 'intermediate; not delivered'}
