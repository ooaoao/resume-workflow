// Explicit local HTML -> print-media measurement / PDF. Own browser only.
import fs from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {pageBox} from './render_stage.mjs';

const [html, out, chrome, stage] = process.argv.slice(2);
if (!html || !out || !chrome || !['html', 'pdf'].includes(stage)) throw Error('HTML OUTPUT CHROME html|pdf required');
const box = pageBox(fs.readFileSync(html, 'utf8'));
const width = box.px.width - box.marginPx.left - box.marginPx.right;
const height = box.px.height - box.marginPx.top - box.marginPx.bottom;
const profile = path.join(out, '.browser-' + process.pid);
const child = spawn(chrome, ['--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--disable-background-networking', '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], {windowsHide:true, stdio:['ignore','ignore','pipe']});
let socket, stderr = '', sequence = 0, session, closing = false;
const pending = new Map();
const deadline = setTimeout(() => {
  for (const {reject} of pending.values()) reject(Error('renderer deadline exceeded'));
  // Only this child, never an image-name/process-tree kill.
  child.kill();
}, 90000);

function call(method, params = {}, target = session) {
  return new Promise((resolve,reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(Error('CDP timeout: ' + method));
    }, 15000);
    pending.set(id, {resolve,reject,timer});
    socket.send(JSON.stringify({id,method,params,...(target ? {sessionId:target} : {})}));
  });
}
async function evaluate(expression) {
  const result = await call('Runtime.evaluate', {expression,awaitPromise:true,returnByValue:true});
  if (result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
try {
  const endpoint = await new Promise((resolve,reject) => {
    const timer = setTimeout(() => reject(Error('Chrome startup timeout: ' + stderr.slice(-1000))), 15000);
    child.once('error', error => {
      clearTimeout(timer);
      reject(error);
    });
    child.once('exit', code => {
      if (!socket) {
        clearTimeout(timer);
        reject(Error('Chrome exited ' + code));
      }
    });
    child.stderr.on('data', b => {
      stderr += b.toString();
      const m = /DevTools listening on (ws:\/\/[^\s]+)/.exec(stderr);
      if (m) {
        clearTimeout(timer);
        resolve(m[1]);
      }
    });
  });
  socket = new WebSocket(endpoint);
  await new Promise((resolve,reject) => {
    socket.addEventListener('open',resolve,{once:true});
    socket.addEventListener('error',reject,{once:true});
  });
  socket.addEventListener('message', e => {
    const msg = JSON.parse(e.data), item = pending.get(msg.id);
    if (!item) return;
    pending.delete(msg.id);
    clearTimeout(item.timer);
    if (msg.error) {
      item.reject(Error(JSON.stringify(msg.error)));
    } else {
      item.resolve(msg.result);
    }
  });
  socket.addEventListener('close', () => {
    if (!closing) {
      for (const item of pending.values()) {
        clearTimeout(item.timer);
        item.reject(Error('browser connection lost'));
      }
    }
    pending.clear();
  });
  const {targetId} = await call('Target.createTarget', {url:'about:blank'}, null);
  session = (await call('Target.attachToTarget', {targetId,flatten:true}, null)).sessionId;
  await call('Page.enable');
  await call('DOM.enable');
  await call('CSS.enable');
  await call('Emulation.setDeviceMetricsOverride', {width,height,deviceScaleFactor:1,mobile:false});
  await call('Emulation.setEmulatedMedia', {media:'print'});
  // No remote resource is permitted by the Python passive-HTML boundary.
  await call('Network.enable');
  await call('Network.setBlockedURLs', {urls:['http://*','https://*','ftp://*']});
  await call('Page.navigate', {url:pathToFileURL(html).href});
  await evaluate(`new Promise((resolve,reject) => {
    const finish = async () => {
      await document.fonts.ready;
      await Promise.all([...document.images].map(i => i.decode()));
      requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)));
    };
    if (document.readyState === 'complete') finish().catch(reject);
    else window.addEventListener('load', () => finish().catch(reject), {once:true});
  })`);
  const dom = await evaluate(`(() => {
    const leaves = [...document.body.querySelectorAll('*')].filter(e => !e.children.length && e.tagName !== 'SCRIPT' && e.tagName !== 'STYLE' && e.innerText?.trim());
    const nodes = leaves.map(e => {
      const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
      return {text:e.innerText, x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom,fontWeight:cs.fontWeight};
    });
    const clipping = [...document.body.querySelectorAll('*')].filter(e => e.clientWidth && e.scrollWidth > e.clientWidth + 1).map(e=>({tag:e.tagName,cls:e.className,client:e.clientWidth,scroll:e.scrollWidth}));
    return {text:document.body.innerText, nodes, clipping, bodyHeight:document.body.getBoundingClientRect().height, images:[...document.images].map(i=>({complete:i.complete,naturalWidth:i.naturalWidth})), weights:nodes.map(n=>n.fontWeight)};
  })()`);
  // innerText omits CSS-generated text such as education-note markers. Use
  // Chromium's actual visible layout text, including pseudo-element glyphs,
  // as the complete PDF-text evidence; retain innerText above for diagnosis.
  const snapshot = await call('DOMSnapshot.captureSnapshot', {computedStyles:['visibility']});
  const documentSnapshot = snapshot.documents[0];
  const {layout,nodes:snapshotNodes} = documentSnapshot;
  const pseudoTypes = new Map();
  if (snapshotNodes.pseudoType) {
    snapshotNodes.pseudoType.index.forEach((nodeIndex,i) => {
      pseudoTypes.set(nodeIndex,snapshot.strings[snapshotNodes.pseudoType.value[i]]);
    });
  }
  function pseudoAncestor(nodeIndex) {
    for (let index = nodeIndex; index >= 0; index = snapshotNodes.parentIndex[index]) {
      if (pseudoTypes.has(index)) return pseudoTypes.get(index);
    }
    return null;
  }
  dom.visibleTextFragments = [];
  layout.text.forEach((textIndex,i) => {
    const text = snapshot.strings[textIndex];
    const visibility = snapshot.strings[layout.styles[i][0]];
    if (text && visibility === 'visible') {
      dom.visibleTextFragments.push({
        text,
        nodeIndex:layout.nodeIndex[i],
        pseudoType:pseudoAncestor(layout.nodeIndex[i]),
        bounds:layout.bounds[i],
      });
    }
  });
  dom.visibleText = dom.visibleTextFragments.map(fragment=>fragment.text).join('\n');
  const root = (await call('DOM.getDocument', {depth:-1})).root.nodeId;
  const ids = (await call('DOM.querySelectorAll', {nodeId:root,selector:'.name,.role,.metric-value,.metric-label,.summary,.school,.major,.date,.edu-note,.proj-name,.proj-kind,.proj-role,.proj-label,.proj-text,.skill-label,.skill-text,.hl,.section-title'})).nodeIds;
  dom.platformFonts = [];
  for (const nodeId of ids) {
    const {computedStyle} = await call('CSS.getComputedStyleForNode', {nodeId});
    const {fonts} = await call('CSS.getPlatformFontsForNode', {nodeId});
    dom.platformFonts.push({nodeId,fontWeight:computedStyle.find(x=>x.name==='font-weight')?.value,fonts});
  }
  dom.page = {...box,printArea:{width,height}};
  const paper = await evaluate(`(() => {
    const rules = [...document.styleSheets].flatMap(sheet => [...sheet.cssRules]);
    const page = rules.find(rule => rule.type === CSSRule.PAGE_RULE);
    if (!page) throw Error('print page rule missing');
    const image = page.style.backgroundImage;
    return {
      color:page.style.backgroundColor || 'transparent',
      image:!image || ['initial','unset'].includes(image) ? 'none' : image,
    };
  })()`);
  if (paper.image !== 'none') throw Error('solid print paper background required: ' + JSON.stringify(paper));
  dom.preview = {
    width:box.px.width,
    height:box.px.height,
    contentX:box.marginPx.left,
    contentY:box.marginPx.top,
    contentWidth:width,
    contentHeight:height,
    paperBackground:paper.color,
    substrate:'#ffffff',
  };
  dom.overflow = dom.nodes.filter(n=>n.x < -1 || n.y < -1 || n.right > width+1 || n.bottom > height+1);
  fs.writeFileSync(path.join(out, stage==='html' ? 'html_dom.json' : 'pdf_dom.json'), JSON.stringify(dom,null,2));
  if (stage==='html') {
    const {data} = await call('Page.captureScreenshot', {format:'png',captureBeyondViewport:true,clip:{x:0,y:0,width,height,scale:1}});
    // Composite the existing print-area pixels without resizing or changing DOM,
    // viewport, CSS, text wrapping or the font measurements above.
    const fullPage = await evaluate(`new Promise((resolve,reject) => {
      const geometry = ${JSON.stringify(dom.preview)};
      const canvas = document.createElement('canvas');
      canvas.width = geometry.width;
      canvas.height = geometry.height;
      const context = canvas.getContext('2d');
      context.fillStyle = geometry.substrate;
      context.fillRect(0,0,canvas.width,canvas.height);
      context.fillStyle = geometry.paperBackground;
      context.fillRect(0,0,canvas.width,canvas.height);
      const image = new Image();
      image.onload = () => {
        if (image.naturalWidth !== geometry.contentWidth || image.naturalHeight !== geometry.contentHeight) {
          reject(Error('print-area capture dimensions changed'));
          return;
        }
        context.drawImage(image,geometry.contentX,geometry.contentY);
        resolve(canvas.toDataURL('image/png').split(',')[1]);
      };
      image.onerror = () => reject(Error('print-area PNG could not be decoded'));
      image.src = 'data:image/png;base64,' + ${JSON.stringify(data)};
    })`);
    fs.writeFileSync(path.join(out,'html_preview.png'), Buffer.from(fullPage,'base64'));
  }
  // Continuous print-media DOM does not expose fragmentation. Measure using
  // the same print path in both stages; HTML's temporary PDF is consumed and
  // removed by Python, never published as the formal PDF artifact.
  const {data} = await call('Page.printToPDF', {printBackground:true,preferCSSPageSize:true,displayHeaderFooter:false});
  fs.writeFileSync(path.join(out,stage==='html' ? '.html-pagination.pdf' : 'resume.pdf'),Buffer.from(data,'base64'));
} catch (error) {
  fs.writeFileSync(path.join(out,'renderer-error.txt'), String(error.stack) + '\n' + stderr.slice(-3000));
  throw error;
} finally {
  closing = true;
  if (socket?.readyState === WebSocket.OPEN) {
    try {
      await call('Browser.close',{},null);
    } catch (error) {
      fs.writeFileSync(path.join(out,'browser-close.txt'),String(error));
    }
    socket.close();
  }
  if (child.exitCode === null && child.signalCode === null) {
    await Promise.race([new Promise(resolve=>child.once('exit',resolve)),new Promise(resolve=>setTimeout(resolve,3000))]);
    if (child.exitCode === null && child.signalCode === null) child.kill();
  }
  clearTimeout(deadline);
  for (const item of pending.values()) clearTimeout(item.timer);
  try {
    fs.rmSync(profile,{recursive:true,force:true});
  } catch (error) {
    fs.writeFileSync(path.join(out,'profile-cleanup.txt'),String(error));
  }
}
