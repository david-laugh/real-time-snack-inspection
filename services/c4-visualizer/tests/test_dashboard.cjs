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
    .replace(/renderScoreChart\(\);\s*loadInitial\(\);\s*connect\(\);\s*$/, '');
  vm.runInContext(script, sandbox);
  return {run: code => vm.runInContext(code, sandbox), images};
}
const frame = (n) => JSON.stringify({trace_id: `t${n}`, session_id: 's', frame_id: n, width: 3, height: 2, frame: 'base64'});
const result = (n) => JSON.stringify({trace_id: `t${n}`, session_id: 's', frame_id: n, detections: []});
function loaded(d) { const img = d.images.at(-1); img.naturalWidth = 3; img.naturalHeight = 2; img.onload(); }

test('RLE row-major pixels, leading zero, and invalid-mask fallback', () => {
  const d = dashboard();
  const pixels = d.run(`decodeMask({encoding:'rle',width:3,height:2,data:{order:'row-major',counts:[0,2,2,1,1]}},3,2,'normal').getContext('2d').pixels`);
  assert.deepEqual(Array.from(pixels).filter((_,i) => i % 4 === 3), [96,96,0,0,96,0]);
  for (const counts of ['[7]', '[1,-1,6]', '[2]', '[1,1.5,3.5]']) {
    assert.equal(d.run(`decodeMask({encoding:'rle',width:3,height:2,data:{order:'row-major',counts:${counts}}},3,2,'normal')`), null);
  }
  assert.equal(d.run(`decodeMask(null,3,2,'pass')`), null);
  assert.equal(d.run(`decodeMask({encoding:'rle',width:2,height:3},3,2,'normal')`), null);
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
    detections:[{item_id:'cookie',bbox:[0,0,2,1],status:'defect',defect_score:0.7,
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

test('normal, defect and unknown remain distinct in overlay and feed', () => {
  const d = dashboard();
  assert.equal(d.run("statusView('normal').label"), '정상');
  assert.equal(d.run("statusView('unknown').label"), '판정 보류');
  assert.equal(d.run("statusView('defect').label"), '불량');
  d.run("addResult({status:'unknown',item_id:'c0',defect_score:0,defect_type:null})");
  assert.match(d.run("id('feedList').innerHTML"), /판정 보류/);
});

test('pipeline view shows each stage from C1/C2/C3 data without re-judging', () => {
  const d = dashboard();
  d.run(`updateStats({total:6,pass_count:4,fail_count:1,unknown_count:1,defect_rate:16.7,fps:30,received_frames:900,
    c1_metrics:{received:910,dropped:0,input_fps:30,sessions:1},
    c2_metrics:{dropped_stale:3,dropped_old:0,forwarded:900,error:1,timeout:0,inflight:1,max_inflight:3,c4_error:0,c4_skipped:0},
    drop_window:{seconds:6,c2_drop_pct:0.3,c2_error_pct:0.1,result_gap_estimate:0},
    c3_replicas:{'c3-inference-1':{frames:450,c3_total_p50_ms:43},'c3-inference-2':{frames:450,c3_total_p50_ms:44}},
    latency:{upload:{p50_ms:50,p95_ms:60,samples:9},c1_c2:{p50_ms:3,p95_ms:4,samples:9},preprocess:{p50_ms:6,p95_ms:7,samples:9},
      c2_c3:{p50_ms:2,p95_ms:3,samples:9},c3_queue:{p50_ms:0,p95_ms:0,samples:9},c3_service:{p50_ms:43,p95_ms:70,samples:9},
      c3_c4:{p50_ms:2.5,p95_ms:3.5,samples:9},e2e:{p50_ms:110,p95_ms:140,samples:9}}});`);
  const flow = d.run("id('pipeFlow').innerHTML");
  for (const text of ['C1 Gateway', 'C2 Preprocess', 'C3 Inference ×2', 'C4 Visualizer', 'c3-inference-2', '밀려서 버림 3 · 0.3%', '실패(오류+timeout) 1 · 0.1%']) {
    assert.ok(flow.includes(text), text);
  }
  assert.equal(d.run("id('sUnknown').textContent"), '1');
  assert.match(d.run("id('pipeLegend').innerHTML"), /C3 추론 <b>43<\/b>/);
  // no prompts/missing counters -> placeholders, not zeros
  d.run(`updateStats({total:0,pass_count:0,fail_count:0,defect_rate:0,fps:0});`);
  assert.match(d.run("id('pipeFlow').innerHTML"), /실패\(오류\+timeout\) —/);
});
