const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function dashboard() {
  const images = [];
  const elements = new Map();
  const context2d = () => ({
    createImageData: (w, h) => ({data: new Uint8ClampedArray(w * h * 4)}),
    putImageData(data) { this.pixels = data.data; },
    clearRect() {}, drawImage() {}, strokeRect() {}, fillRect() {}, fillText() {},
    measureText: () => ({width: 10}),
  });
  const element = () => {
    const ctx = context2d();
    return {style: {}, setAttribute() {}, getContext: () => ctx, querySelector: () => null,
      appendChild() {}, getBoundingClientRect: () => ({width: 600, height: 400})};
  };
  const sandbox = {document: {
    createElement: element,
    getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
  }, ResizeObserver: class {observe() {}},
  Image: class {constructor() {images.push(this);} },
  };
  vm.createContext(sandbox);
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
    .replace(/renderCircChart\(\);\s*loadInitial\(\);\s*connect\(\);\s*$/, '');
  vm.runInContext(script, sandbox);
  return {run: code => vm.runInContext(code, sandbox), images};
}
const frame = (n) => JSON.stringify({trace_id: `t${n}`, session_id: 's', frame_id: n, width: 3, height: 2, frame: 'base64'});
const result = (n) => JSON.stringify({trace_id: `t${n}`, session_id: 's', frame_id: n, detections: []});
function loaded(d) { const img = d.images.at(-1); img.naturalWidth = 3; img.naturalHeight = 2; img.onload(); }

test('RLE row-major pixels, leading zero, and invalid-mask fallback', () => {
  const d = dashboard();
  const pixels = d.run(`decodeMask({encoding:'rle',width:3,height:2,data:{order:'row-major',counts:[0,2,2,1,1]}},3,2,'pass').getContext('2d').pixels`);
  assert.deepEqual(Array.from(pixels).filter((_,i) => i % 4 === 3), [96,96,0,0,96,0]);
  for (const counts of ['[7]', '[1,-1,6]', '[2]', '[1,1.5,3.5]']) {
    assert.equal(d.run(`decodeMask({encoding:'rle',width:3,height:2,data:{order:'row-major',counts:${counts}}},3,2,'pass')`), null);
  }
  assert.equal(d.run(`decodeMask(null,3,2,'pass')`), null);
  assert.equal(d.run(`decodeMask({encoding:'rle',width:2,height:3},3,2,'pass')`), null);
});

test('matches either arrival order, clears empty frames, and ignores late results', () => {
  const d = dashboard();
  d.run(`updateFrameResult(${result(1)}); updateCamera(${frame(1)});`);
  loaded(d);
  assert.equal(d.run('currentFrame.frame_id'), 1);
  d.run(`updateCamera(${frame(2)}); updateCamera(${frame(3)}); updateFrameResult(${result(3)});`);
  loaded(d);
  assert.equal(d.run('currentFrame.frame_id'), 3);
  const before = d.images.length;
  d.run(`updateFrameResult(${result(2)});`);
  assert.equal(d.images.length, before);
  assert.equal(d.run('bboxOverlays.length'), 0);
  for (let i=4; i<50; i++) d.run(`updateCamera(${frame(i)});`);
  assert.equal(d.run('pendingFrames.size'), 30);
});

test('a preview does not prevent delayed results from being displayed', () => {
  const d = dashboard();
  d.run(`updateCamera(${frame(1)});`); loaded(d);
  d.run(`updateCamera(${frame(2)}); updateFrameResult(${result(1)});`); loaded(d);
  assert.equal(d.run('currentFrame.frame_id'), 1);
  d.run(`updateFrameResult(${result(2)});`); loaded(d);
  assert.equal(d.run('currentFrame.frame_id'), 2);
});

test('renders a matched mask and removes it on an empty next frame', () => {
  const d = dashboard();
  d.run(`updateCamera(${frame(1)}); updateFrameResult({trace_id:'t1',session_id:'s',frame_id:1,
    detections:[{item_id:'cookie',bbox:[0,0,2,1],status:'fail',circularity:0.7,
      mask:{encoding:'rle',width:3,height:2,data:{order:'row-major',counts:[0,2,4]}}}]});`);
  loaded(d);
  assert.equal(d.run('bboxOverlays.length'), 1);
  assert.equal(d.run('bboxOverlays[0].maskCanvas.width'), 3);
  d.run(`updateCamera(${frame(2)}); updateFrameResult(${result(2)});`); loaded(d);
  assert.equal(d.run('bboxOverlays.length'), 0);
});

test('metrics distinguish unavailable counters from zero', () => {
  const d = dashboard();
  d.run(`updateStats({total:0,pass_count:0,fail_count:0,defect_rate:0,fps:0});`);
  assert.equal(d.run("id('pDrop').textContent"), '—');
  assert.equal(d.run("id('pE2e').textContent"), '—');
  d.run(`updateStats({total:0,pass_count:0,fail_count:0,defect_rate:0,fps:0,
    c2_metrics:{dropped_stale:0,dropped_old:0,c4_skipped:null,c4_error:null}});`);
  assert.equal(d.run("id('pDrop').textContent"), '0');
  assert.equal(d.run("id('pLatencyTail').textContent"), '수신 대기');
});
