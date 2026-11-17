// B1分阶段渲染契约：纯函数；pagePixels不做IO，stagePlan在pdf-final时
// 以fs.exists核验html-review产物（这是刻意的文件系统前置，不是纯IO无关）。
// 消费者：render.mjs（后续接线）、render_stage_v26_test.mjs；模块须列入
// rehearsal_binding.RENDERER_SOURCES与发布adapter白名单（主端整合，不自改）。
// full=旧全链；html-review=真实A4 print-media页面截图与DOM测量，当前入口
// 还用临时打印探针测实际分页；pdf-final发布同一既有HTML的正式PDF，
// 要求html-review产物已存在。探针不作为resume.pdf交付或审签。
// 阶段由Python guard绑定传递与核验，浏览器参数单独不构成定稿声明。
import fs from 'node:fs';
import path from 'node:path';

export function parseStageArguments(argv) {
  const flags = argv.filter(a => a.startsWith('--'));
  const rest = argv.filter(a => !a.startsWith('--'));
  if (rest.length !== 3) throw Error('Renderer requires HTML, output and Chrome');
  let stage;
  for (const flag of flags) {
    const m = /^--stage=(.+)$/.exec(flag);
    if (!m) throw Error('unsupported renderer argument: ' + flag);
    if (m[1] !== 'html-review' && m[1] !== 'pdf-final') throw Error('unsupported renderer stage: ' + m[1]);
    if (stage) throw Error('duplicate --stage flag');
    stage = m[1];
  }
  return {stage: stage || 'full', rest};
}

export function stagePlan(stage, out) {
  const stageNames = {
    'full': ['dom.json', 'resume.pdf', 'chrome.log'],
    'html-review': ['html_dom.json', 'html_preview.png', 'html_stage.log'],
    // dom.json保留：正式交付核验(_check)仍要求标准测量输出；html_dom.json
    // 是评审附加产物，不替代。Python guard后续选择复核策略。
    'pdf-final': ['dom.json', 'resume.pdf', 'pdf_stage.log'],
  };
  const names = stageNames[stage];
  if (!names) throw Error('unsupported renderer stage: ' + stage);
  if (stage === 'pdf-final' && !fs.existsSync(path.join(out, 'html_dom.json'))) {
    throw Error('pdf-final requires html_dom.json from html-review stage');
  }
  return names.map(name => ({name, path: path.join(out, name)}));
}

export function pagePixels({width, height}) {
  const unit = v => {
    const m = /^(\d+(?:\.\d+)?)(mm|in)$/.exec(v);
    if (!m) return null;
    const n = Number(m[1]);
    if (!Number.isFinite(n) || n <= 0) return null;
    return m[2] === 'mm' ? Math.round(n * 96 / 25.4) : Math.round(n * 96);
  };
  const w = unit(width), h = unit(height);
  if (!w || !h) throw Error('absolute positive mm/in page size required');
  return {width: w, height: h};
}

// 从实际HTML的@page读取纸张模型：A4名义尺寸或绝对长度、四边margin(px)、
// 纵向校验。这是html-review纸面/溢出报告的唯一尺寸来源，不是常量假设。
export function pageBox(html) {
  const rule = /@page\s*\{([^}]*)\}/.exec(html);
  if (!rule) throw Error('@page rule required for staged rendering');
  const body = rule[1];
  const size = /\bsize\s*:\s*([^;}]+)/.exec(body);
  if (!size) throw Error('@page size required for staged rendering');
  const spec = size[1].trim();
  let w, h;
  if (/^A4\s*$/i.test(spec)) {
    w = '210mm';
    h = '297mm';
  }
  else {
    const nums = spec.match(/^\s*(\d+(?:\.\d+)?)(mm|in|pt|px)\s+(\d+(?:\.\d+)?)(mm|in|pt|px)\s*$/);
    if (!nums) throw Error('absolute two-length page size required');
    w = nums[1] + nums[2];
    h = nums[3] + nums[4];
  }
  const px = pagePixels({width: w, height: h});
  if (px.height <= px.width) throw Error('portrait page required');
  const toPx = v => {
    const m = /^(\d+(?:\.\d+)?)(mm|in|pt|px)$/.exec(v);
    if (!m) throw Error('absolute page margin required');
    const n = Number(m[1]);
    if (!Number.isFinite(n) || n < 0) throw Error('absolute page margin required');
    return Math.round(n * (m[2] === 'mm' ? 96 / 25.4 : m[2] === 'in' ? 96 : m[2] === 'pt' ? 96 / 72 : 1));
  };
  const margin = /\bmargin\s*:\s*([^;}]+)/.exec(body);
  let t, r, b, l;
  if (!margin) { t = r = b = l = 0; }
  else {
    const parts = margin[1].trim().split(/\s+/);
    if (parts.length < 1 || parts.length > 4) throw Error('invalid page margin');
    const v = parts.map(toPx);
    [t, r, b, l] = v.length === 1 ? [v[0], v[0], v[0], v[0]]
      : v.length === 2 ? [v[0], v[1], v[0], v[1]]
      : v.length === 3 ? [v[0], v[1], v[2], v[1]] : v;
  }
  return {px, marginPx: {top: t, right: r, bottom: b, left: l},
          sizeSource: /^A4\b/i.test(spec) ? '@page size A4 (nominal 210mm×297mm)' : '@page size ' + spec};
}
