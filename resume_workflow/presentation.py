"""Render explicitly supplied resume content and resources using the bundled templates."""
from pathlib import Path
from html.parser import HTMLParser
import html, re, json, base64
import pymupdf
ROOT = Path(__file__).parent
GENERATED_HTML_MAX_BYTES = 64 * 1024 * 1024
RESOURCE_MEMBERS = ('font_w04', 'font_w05', 'photo')

def read(p):
    return json.loads(p.read_text(encoding='utf8'))

def file_content(p):
    return p.read_bytes()

def safe_path(p, **kw):
    return Path(p)

def preview_resource_content(key, p):
    return p.read_bytes()

class PassiveMarkup(HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []
        self.section = None
        self.configs = []
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        if len(dict(attrs)) != len(attrs):
            raise ValueError('duplicate HTML attributes')
        attrs = dict(attrs)
        if tag not in {'html', 'head', 'meta', 'title', 'style', 'body', 'header', 'div', 'span', 'img', 'section', 'script', 'br', 'strong', 'b', 'a'}:
            raise ValueError('active/unsupported HTML element: ' + tag)
        if any((k.startswith('on') or k in {'srcdoc', 'srcset', 'href', 'action', 'formaction', 'style'} for k in attrs)):
            raise ValueError('active HTML attribute')
        if tag == 'meta' and (set(attrs) != {'charset'} or attrs['charset'].lower() != 'utf-8'):
            raise ValueError('active meta forbidden')
        if tag == 'script':
            if attrs != {'type': 'application/json', 'id': 'page-config'}:
                raise ValueError('active script forbidden')
            self.section = 'script'
            self.buffer = []
        elif tag == 'style':
            self.section = 'style'
            self.buffer = []
        if 'src' in attrs:
            if tag != 'img':
                raise ValueError('HTML resource forbidden')
            self.urls.append(attrs['src'])

    def handle_data(self, value):
        if self.section:
            self.buffer.append(value)

    def handle_endtag(self, tag):
        if tag == self.section:
            value = ''.join(self.buffer)
            if tag == 'script':
                self.configs.append(json.loads(value))
            else:
                if re.search('@import|expression\\s*\\(|javascript:|\\\\|<', value, re.I):
                    raise ValueError('active CSS forbidden')
                self.urls.extend(re.findall('url\\(\\s*["\\\']?([^"\\\')\\s]+)', value, re.I))
            self.section = None
            self.buffer = []

def validate_html(source, config, approved_urls):
    parsed = PassiveMarkup()
    parsed.feed(source)
    parsed.close()
    if parsed.section is not None or source.count('<head>') != 1 or parsed.configs != [config] or (len(parsed.urls) != 3) or (set(parsed.urls) != set(approved_urls)):
        raise ValueError('HTML config/resource binding mismatch')

def esc(s):
    return html.escape(str(s), quote=True)

def rich(s, fragment=None):
    if fragment is None:
        fragment, _ = load_template()
    return re.sub('\\*\\*(.+?)\\*\\*', lambda m: fragment('EMPHASIS', TEXT=m[1]), esc(s))

def validate_config(config):
    presets = read(ROOT / '02_模板/页面预设.json')
    if config['preset'] not in presets:
        raise ValueError('unknown preset')
    vals = {**presets[config['preset']], **config.get('values', {})}
    if set(vals) != set(presets['STANDARD']):
        raise ValueError('unknown config keys')
    for k, v in vals.items():
        if not re.fullmatch('\\d+(?:\\.\\d+)?(?:pt|mm)?', str(v)):
            raise ValueError('invalid value ' + k)
        f = float(re.match('\\d+(?:\\.\\d+)?', str(v)).group())
        if k.endswith('line') and (str(v).endswith(('pt', 'mm')) or f <= 0):
            raise ValueError('line height must be positive unitless')
        if k.endswith('size') and (not str(v).endswith('pt')):
            raise ValueError('font sizes require pt')
    for k in ['body-size', 'summary-size', 'detail-size']:
        if float(vals[k][:-2]) < 8.6:
            raise ValueError('font below 8.6pt')
    baseline = config.get('education_baseline')
    if baseline is not None:
        if not isinstance(baseline, dict) or set(baseline) != {'locale', 'note_size_pt', 'report_sha256', 'config_sha256'}:
            raise ValueError('invalid education baseline record')
        if baseline['locale'] not in LOCALES or not isinstance(baseline['note_size_pt'], (int, float)) or baseline['note_size_pt'] <= 0:
            raise ValueError('invalid education baseline locale or note size')
        if any((not isinstance(baseline[k], str) or not re.fullmatch('[0-9a-f]{64}', baseline[k]) for k in ['report_sha256', 'config_sha256'])):
            raise ValueError('invalid education baseline SHA256')
    result = {'preset': config['preset'], 'values': vals}
    if baseline is not None:
        result['education_baseline'] = baseline
    return result

def education_baseline_failures(config, locale):
    baseline = config.get('education_baseline')
    if baseline is None:
        return []
    if baseline['locale'] != locale:
        return ['education baseline locale mismatch']
    current = float(config['values']['note-size'][:-2])
    if current + 1e-06 < baseline['note_size_pt']:
        return ['education note font shrunk below first compliant baseline: %.2fpt < %.2fpt' % (current, baseline['note_size_pt'])]
    return []
LOCALES = {'zh-CN': {'LANG': 'zh-CN', 'PROFILE_HEADING': '个人简介', 'EDUCATION_HEADING': '教育背景', 'SKILLS_HEADING': '能力与技能', 'PORTRAIT_ALT': '证件照'}, 'en': {'LANG': 'en', 'PROFILE_HEADING': 'Profile', 'EDUCATION_HEADING': 'Education', 'SKILLS_HEADING': 'Skills', 'PORTRAIT_ALT': 'Portrait'}}

def locale_values(locale='zh-CN'):
    if not isinstance(locale, str) or locale not in LOCALES:
        raise ValueError('unknown locale')
    return LOCALES[locale].copy()

def content_locale_values(data):
    """Only the Chinese skills title may differ from the fixed locale names."""
    locale = data.get('locale', 'zh-CN')
    values = locale_values(locale)
    if 'locale_values' not in data:
        return values
    supplied = data['locale_values']
    if not isinstance(supplied, dict) or not set(supplied) <= set(values):
        raise ValueError('unknown locale named values')
    for key, value in supplied.items():
        if key == 'SKILLS_HEADING' and locale == 'zh-CN':
            if not isinstance(value, str) or not value or value != value.strip() or (not value.isprintable()) or any((c in value for c in '<>&{}')):
                raise ValueError('skills heading must be plain single-line text')
        elif value != values[key]:
            raise ValueError('locale named value override forbidden: ' + key)
        values[key] = value
    return values

class LocaleMarkup(HTMLParser):

    def __init__(self):
        super().__init__()
        self.langs = []
        self.headings = []
        self.alts = []
        self.school_ids = []
        self.heading_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'html':
            self.langs.append(a.get('lang'))
        if tag == 'img':
            self.alts.append(a.get('alt'))
        if 'edu' in a.get('class', '').split():
            self.school_ids.append(a.get('data-school-id'))
        if 'section-title' in a.get('class', '').split():
            self.headings.append('')
            self.heading_depth = 1
        elif self.heading_depth:
            self.heading_depth += 1

    def handle_endtag(self, tag):
        if self.heading_depth:
            self.heading_depth -= 1

    def handle_data(self, data):
        if self.heading_depth:
            self.headings[-1] += data

def locale_markup_failures(source, locale, education, named_values=None, *, education_selected=False):
    data = {'locale': locale}
    if named_values is not None:
        data['locale_values'] = named_values
    values = content_locale_values(data)
    parsed = LocaleMarkup()
    parsed.feed(source)
    failures = []
    if parsed.langs != [values['LANG']]:
        failures.append('document locale mismatch')
    headings = [x.strip() for x in parsed.headings]
    keys = ['PROFILE_HEADING', *(['EDUCATION_HEADING'] if education or not education_selected else []), 'SKILLS_HEADING']
    shown = [headings[0], *([headings[1]] if len(keys) == 3 and len(headings) > 1 else []), headings[-1]] if headings else []
    if len(headings) < len(keys) or shown != [values[k] for k in keys] or (education_selected and (not education) and (values['EDUCATION_HEADING'] in headings)):
        failures.append('locale heading mismatch')
    if parsed.alts != [values['PORTRAIT_ALT']]:
        failures.append('portrait locale mismatch')
    if parsed.school_ids != [e['school_id'] for e in validate_education(education, locale, selected=education_selected)]:
        failures.append('canonical school identity markup mismatch')
    return failures
REQUIRED_TEMPLATE_FRAGMENTS = {'PROJECT_NAME': ['V0'], 'PROJECT_OPTION': ['V0', 'V1'], 'PROJECT_HEADER': ['V0'], 'PAGE_OPEN': ['V0', 'V1', 'V2', 'LANG'], 'CONTACT_OPEN': ['V0', 'V1', 'V2', 'V3'], 'METRIC': ['V0', 'V1'], 'PROFILE_EDUCATION_OPEN': ['V0', 'V1', 'PORTRAIT_ALT', 'PROFILE_HEADING', 'EDUCATION_HEADING'], 'EDUCATION_ROW': ['V0', 'V1', 'V2', 'V3', 'SCHOOL_ID'], 'EDUCATION_NOTE': ['V0'], 'EDUCATION_CLOSE': [], 'EDUCATION_SECTION_CLOSE': [], 'SECTION_OPEN': ['V0', 'V1'], 'SECTION_OPEN_PLAIN': ['V0'], 'PROJECT_OPEN': ['V0', 'V1'], 'PROJECT_ROW': ['V0', 'V1'], 'PROJECT_CLOSE': [], 'SECTION_CLOSE': [], 'SKILLS_OPEN': ['SKILLS_HEADING'], 'SKILL_ROW': ['V0', 'V1'], 'PAGE_CLOSE': ['V0'], 'EMPHASIS': ['TEXT']}

def load_template():
    path = ROOT / '02_模板/简历结构.html'
    source = file_content(path).decode('utf8').replace('\r\n', '\n')
    matches = list(re.finditer('<!-- BEGIN ([A-Z0-9_]+) -->\\n(.*?)\\n<!-- END \\1 -->', source, re.S))
    fragments = {m[1]: m[2] for m in matches}
    required = REQUIRED_TEMPLATE_FRAGMENTS
    if len(fragments) != len(matches) or set(fragments) != set(required):
        raise ValueError('template fragments missing, duplicated or unknown')
    for name, slots in required.items():
        if set(re.findall('\\{\\{([A-Z0-9_]+)\\}\\}', fragments[name])) != set(slots):
            raise ValueError('template slots missing or unknown: ' + name)

    def fragment(name, **values):
        if set(values) != set(required[name]):
            raise ValueError('template arguments: ' + name)
        result = re.sub('\\{\\{([A-Z0-9_]+)\\}\\}', lambda m: str(values[m[1]]), fragments[name])
        if '{{' in result:
            raise ValueError('unresolved template placeholder: ' + name)
        return result
    return (fragment, {'path': str(path.resolve())})

def header_text(value):
    return str(value or '').strip(' ·｜|')

def project_header(p, fragment):
    parts = [fragment('PROJECT_NAME', V0=esc(p['name']))]
    for key, cls in [('kind', 'proj-kind'), ('role', 'proj-role')]:
        value = header_text(p.get(key, ''))
        if value:
            parts.append(fragment('PROJECT_OPTION', V0=cls, V1=esc(value)))
    return fragment('PROJECT_HEADER', V0=''.join(parts))

def note_has_content(value):
    return isinstance(value, str) and bool(re.search('[\\u3400-\\u9fffA-Za-z0-9]', value)) and (not re.search('待填写|待补充|占位|亮点\\s*\\d|TODO', value, re.I))

def generated_document(data, config, data_root, *, resource_context=None, preview=False, education=None):
    """One generation seam for compose and backend reconstruction, with no writes."""
    fragment, template_info = load_template()
    locale = data.get('locale', 'zh-CN')
    localized = content_locale_values(data)
    config = validate_config(config)
    baseline_fails = education_baseline_failures(config, locale)
    if baseline_fails:
        raise ValueError('; '.join(baseline_fails))
    selected = education is not None
    if not selected:
        if 'education_record_ids' in data:
            raise ValueError('education requires bound task source')
        education = localized_education(data.get('education'), locale)
    else:
        education = validate_education(education, locale, selected=True)
    resources = {'structure_template': template_info}
    read_members = {}

    def member_bytes(key, mime):
        """每个成员同一次生成只读盘一次；字体记录实际字节的内部名。"""
        if key not in read_members:
            paths = data.get('resources')
            if not isinstance(paths, dict) or key not in paths:
                missing = [name for name in RESOURCE_MEMBERS if not isinstance(paths, dict) or name not in paths]
                raise ValueError('经典 A4 的 content.json 缺少 resources 路径：' + ', '.join(missing))
            try:
                p = safe_path(paths[key], file=True)
            except (ValueError, TypeError) as exc:
                raise ValueError('content.json resources.' + key + ': ' + str(exc)) from exc
            content = preview_resource_content(key, p)
            record = {'path': str(p)}
            if key != 'photo':
                record['font_name'] = pymupdf.Font(fontbuffer=content).name
            read_members[key] = (content, record)
        return read_members[key]

    def embed(key, mime):
        content, record = member_bytes(key, mime)
        resources[key] = record
        return 'data:' + mime + ';base64,' + base64.b64encode(content).decode()
    css = file_content(ROOT / '02_模板/简历样式.css').decode('utf8')
    css = css.replace('{{FONT_W04}}', embed('font_w04', 'font/ttf')).replace('{{FONT_W05}}', embed('font_w05', 'font/ttf'))
    css = css.replace('{{PAGE_CONFIG}}', ';'.join(('--' + k + ':' + str(v) for k, v in config['values'].items())))
    photo = embed('photo', 'image/jpeg')
    h = [fragment('PAGE_OPEN', V0=esc(data['title']), V1=css, V2=config['preset'].lower(), LANG=localized['LANG'])]
    h += [fragment('CONTACT_OPEN', V0=esc(data['name']), V1=esc(data['alias']), V2=esc(data['role']), V3=esc(data['contact']))]
    for value, label in data['metrics']:
        h += [fragment('METRIC', V0=esc(value), V1=esc(label))]
    intro = fragment('PROFILE_EDUCATION_OPEN', V0=photo, V1=rich(data['summary'], fragment), **{k: localized[k] for k in ['PORTRAIT_ALT', 'PROFILE_HEADING', 'EDUCATION_HEADING']})
    if selected and (not education):
        tail = '<section><div class="section-title">' + localized['EDUCATION_HEADING'] + '</div>'
        if not intro.endswith(tail):
            raise ValueError('controlled optional education template mismatch')
        intro = intro[:-len(tail)]
    h += [intro]
    for e in education:
        if len(e.get('notes', [])) > 3:
            raise ValueError('education >3 facts')
        degree = e.get('degree', '')
        major = ' · '.join((esc(value) for value in (degree, e['major']) if value.strip())) if selected else (esc(degree) + ' · ' if degree.strip() else '') + esc(e['major'])
        h += [fragment('EDUCATION_ROW', V0=esc(e['school']), V1=esc(e['badge']), V2=major, V3=esc(e['date']), SCHOOL_ID=esc(e['school_id']))]
        if e.get('notes'):
            h += [fragment('EDUCATION_NOTE', V0=' ｜ '.join((rich(s, fragment) for s in e['notes'])))]
        h += [fragment('EDUCATION_CLOSE')]
    if education or not selected:
        h += [fragment('EDUCATION_SECTION_CLOSE')]
    for section in data['sections']:
        sub = header_text(section.get('sub', ''))
        if sub:
            h += [fragment('SECTION_OPEN', V0=esc(section['title']), V1=esc(sub))]
        else:
            h += [fragment('SECTION_OPEN_PLAIN', V0=esc(section['title']))]
        for p in section['projects']:
            if p['form'] not in ['Full', 'Compact']:
                raise ValueError('invalid form')
            h += [fragment('PROJECT_OPEN', V0=p['form'], V1=project_header(p, fragment))]
            for label, text in p['rows']:
                if label == '能力':
                    raise ValueError('fixed ability row')
                h += [fragment('PROJECT_ROW', V0=esc(label), V1=rich(text, fragment))]
            h += [fragment('PROJECT_CLOSE')]
        h += [fragment('SECTION_CLOSE')]
    h += [fragment('SKILLS_OPEN', SKILLS_HEADING=esc(localized['SKILLS_HEADING']))]
    for label, text in data['skills']:
        h += [fragment('SKILL_ROW', V0=esc(label), V1=rich(text, fragment))]
    h += [fragment('PAGE_CLOSE', V0=json.dumps(config, ensure_ascii=False))]
    raw = '\n'.join(h)
    if len(raw.encode('utf8')) > GENERATED_HTML_MAX_BYTES:
        raise ValueError('generated HTML too large')
    locale_fails = locale_markup_failures(raw, locale, education, localized, education_selected=selected)
    if locale_fails:
        raise ValueError('; '.join(locale_fails))
    if re.search('待填写|信号值|岗位价值标签\\s*[1-4]|应聘[：:]XX|\\{\\{', raw):
        raise ValueError('placeholder')
    approved = ['data:' + mime + ';base64,' + base64.b64encode(read_members[key][0]).decode() for key, mime in [('font_w04', 'font/ttf'), ('font_w05', 'font/ttf'), ('photo', 'image/jpeg')]]
    validate_html(raw, config, approved)
    return (raw, config, education, localized, resources)

def _font_identity_candidate(f):
    """CDP identity name for one font entry: the PostScript name
    disambiguates W04/W05 family-prefix overlap; a bare family name alone
    never proves which declared font rendered."""
    ps = f.get('postScriptName')
    return ps if isinstance(ps, str) and ps.strip() else f.get('familyName', '')

def _font_identity_hits(candidate, declared):
    c = _font_identity_norm(candidate)
    if len(c) < 4:
        return set()
    return {w for w, n in declared.items() if c == n or n.startswith(c)}

def _pdf_font_identity_failures(entries, expected):
    """Both declared font identities must be actually embedded. Subset
    prefixes are stripped per producer practice; a bare family prefix that
    overlaps both declarations is disambiguated from the parsed internal name
    of the embedded bytes (PyMuPDF) and never counts for both."""
    evidenced = set()
    fails = []
    for f in entries:
        if not f.get('bytes'):
            continue
        norm_base = _font_identity_norm(re.sub('^[A-Z]{6}\\+', '', str(f.get('name', ''))))
        matches = {w for w, n in expected.items() if norm_base == _font_identity_norm(n) or (len(norm_base) >= 4 and _font_identity_norm(n).startswith(norm_base))}
        if len(matches) != 1 and f.get('buf'):
            try:
                parsed = _font_identity_norm(pymupdf.Font(fontbuffer=f['buf']).name)
            except Exception:
                parsed = ''
            if len(parsed) >= 4:
                matches = {w for w, n in expected.items() if parsed == _font_identity_norm(n) or _font_identity_norm(n).startswith(parsed)}
        if not matches:
            fails.append('PDF embedded font identity mismatch: ' + str(f.get('name')))
        elif len(matches) == 1:
            evidenced |= matches
    for w, n in sorted(expected.items()):
        if w not in evidenced:
            fails.append('PDF embedded font identity missing: %s (weight %s)' % (n, w))
    return fails

def _font_identity_norm(name):
    return re.sub('[\\s\\-_]+', '', str(name)).lower()

def platform_font_failures(dom, expected):
    """Actual rendered font evidence from CDP representative nodes.

    expected maps required weights (400/500) to declared internal names.
    Every sampled font that rendered glyphs must be the declared identity
    for exactly one weight and come from a document-loaded custom font, so
    undeclared fallback, same-name system fonts and ambiguous bare family
    names fail even when some other node is clean. Each weight additionally
    needs one representative node whose positive-glyph fonts all map to
    exactly that identity. Nodes without rendered glyphs are not evidence
    and never negate real hits. CSS aliases, file names and FontFace loaded
    status alone never prove identity."""
    if not isinstance(expected, dict) or not expected:
        return ['font identity evidence incomplete']
    rows = dom.get('platformFonts') if isinstance(dom, dict) else None
    if not isinstance(rows, list) or not rows:
        return ['platform font evidence incomplete']
    declared = {w: _font_identity_norm(n) for w, n in expected.items()}
    failures = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for f in row.get('fonts') or []:
            if not isinstance(f, dict) or f.get('glyphCount', 0) <= 0:
                continue
            hits = _font_identity_hits(_font_identity_candidate(f), declared)
            label = '%s (node %s)' % (f.get('familyName') or f.get('postScriptName') or '?', row.get('nodeId'))
            if len(hits) != 1:
                failures.append('undeclared or ambiguous platform font: ' + label)
                continue
            if f.get('isCustomFont') is not True:
                failures.append('platform font is not a custom embedded font: ' + label)
    for weight in sorted(declared):
        nodes = [row for row in rows if isinstance(row, dict) and str(row.get('fontWeight', '')) == str(weight)]
        if not nodes:
            failures.append('no representative node at font-weight %s: %s' % (weight, expected[weight]))
            continue
        clean = any((isinstance(row, dict) and [f for f in row.get('fonts') or [] if isinstance(f, dict) and f.get('glyphCount', 0) > 0] and all((_font_identity_hits(_font_identity_candidate(f), declared) == {weight} and f.get('isCustomFont') is True for f in row.get('fonts') or [] if isinstance(f, dict) and f.get('glyphCount', 0) > 0)) for row in nodes))
        if not clean:
            failures.append('declared font not cleanly used at font-weight %s representative nodes: %s' % (weight, expected[weight]))
    return failures

def validate_education(education, locale='zh-CN', *, selected=False):
    if not selected:
        raise ValueError('explicit source-bound education required')
    return education
