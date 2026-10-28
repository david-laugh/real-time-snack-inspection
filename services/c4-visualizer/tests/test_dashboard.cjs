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
    save() {}, restore() {}, setLineDash() {}, beginPath() {}, moveTo(x) { this.lineX = x; }, lineTo() {}, stroke() {},
    measureText: () => ({width: 10}),
  });
  const element = () => {
    const ctx = context2d();
    return {style: {}, setAttribute() {}, getContext: () => ctx, querySelector: () => null,
      appendChild() {}, addEventListener() {},
      getBoundingClientRect: () => ({left: 0, top: 0, width: 600, height: 400})};
  };
  const sandbox = {document: {
    createElement: element,
    getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
  }, ResizeObserver: class {observe() {}},
  Image: class {constructor() {images.push(this);} },
  fetches: [],
  };
  sandbox.fetch = async (url, init) => { sandbox.fetches.push([url, init]);
    return {ok: true, json: async () => ({scan_line_ratio: JSON.parse(init.body).ratio, direction: 'left'})}; };
  vm.createContext(sandbox);
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
    .replace(/renderCircChart\(\);\s*loadInitial\(\);\s*connect\(\);\s*$/, '');
  vm.runInContext(script, sandbox);
  return {run: code => vm.runInContext(code, sandbox), images, sandbox};
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
});

test('pairs within a 2 second window and expires older entries', () => {
  const d = dashboard();
  d.run('var T = 0; clock = () => T;');
  d.run(`updateCamera(${frame(1)});`); loaded(d);
  d.run(`T = 1900; updateFrameResult(${result(1)});`); loaded(d);
  assert.equal(d.run('shownFrame.frame_id'), 1);
  d.run(`updateCamera(${frame(2)}); T = 4000; updateCamera(${frame(3)});`);
  assert.deepEqual(JSON.parse(d.run('JSON.stringify([...pendingFrames.keys()])')), [JSON.stringify(['s','t3',3])]);
  const before = d.images.length;
  d.run(`updateFrameResult(${result(2)});`);  // image 2 expired: result alone cannot be drawn
  assert.equal(d.images.length, before);
  for (let i=4; i<400; i++) d.run(`updateCamera(${frame(i)});`);
  assert.equal(d.run('pendingFrames.size'), 300);
});

test('display order follows frame_id, not arrival order', () => {
  const d = dashboard();
  d.run(`updateFrameResult(${result(5)}); updateFrameResult(${result(4)});
         updateCamera(${frame(4)}); updateCamera(${frame(5)});`);
  loaded(d);
  assert.equal(d.run('currentFrame.frame_id'), 5);
  const before = d.images.length;
  d.run(`updateCamera(${frame(3)}); updateFrameResult(${result(3)});`);
  assert.equal(d.images.length, before);
});

test('frames without prompts are shown immediately and clear overlays', () => {
  const d = dashboard();
  d.run(`updateCamera(${frame(1)}); updateFrameResult({trace_id:'t1',session_id:'s',frame_id:1,
    detections:[{item_id:'c0',bbox:[0,0,2,1],status:'normal',mask:null}]});`);
  loaded(d);
  assert.equal(d.run('bboxOverlays.length'), 1);
  d.run(`updateCamera({...${frame(2)}, prompts: []});`);
  loaded(d);
  assert.equal(d.run('currentFrame.frame_id'), 2);
  assert.equal(d.run('bboxOverlays.length'), 0);
  assert.match(d.run("id('camStatus').textContent"), /객체 없음/);
});

test('decodes real C3 RLE masks from the C3 fixture', () => {
  const d = dashboard();
  const fixture = JSON.parse(fs.readFileSync(path.join(__dirname,
    '../../c3-inference/tests/fixtures/c2_snack_masks.json'), 'utf8'));
  const masks = fixture.frames.flatMap(f => f.objects.map(o => o.mask)).slice(0, 5);
  for (const mask of masks) {
    const expected = mask.data.counts.filter((_, i) => i % 2 === 1).reduce((a, b) => a + b, 0);
    for (const [status, rgb] of [['normal', [0,184,148]], ['defect', [235,59,59]]]) {
      const pixels = d.run(`decodeMask(${JSON.stringify(mask)},${mask.width},${mask.height},'${status}').getContext('2d').pixels`);
      let painted = 0;
      for (let i = 0; i < pixels.length; i += 4) {
        if (pixels[i + 3]) {
          painted++;
          assert.deepEqual([pixels[i], pixels[i + 1], pixels[i + 2], pixels[i + 3]], [...rgb, 96]);
        }
      }
      assert.equal(painted, expected);
    }
  }
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

test('scan line is drawn on the image and moved by clicking it', async () => {
  const d = dashboard();
  d.run(`updateStats({total:0,pass_count:0,fail_count:0,defect_rate:0,
    tracking:{scan_line_ratio:0.34,direction:'left'}});`);
  assert.match(d.run("id('scanLineInfo').textContent"), /34%/);
  d.run(`updateCamera({...${frame(1)}, width: 300, height: 200});`);
  const img = d.images.at(-1); img.naturalWidth = 300; img.naturalHeight = 200; img.onload();
  // 300x200 image in a 600x400 box: scale 2, no offset, so the line is at 0.34 * 600.
  assert.ok(Math.abs(d.run("id('bboxCanvas').getContext('2d').lineX") - 204) < 1e-9);
  await d.run('moveScanLine({clientX: 450})');
  const [url, init] = d.sandbox.fetches.at(-1);
  assert.equal(url, '/api/scan-line');
  assert.equal(JSON.parse(init.body).ratio, 0.75);
  assert.equal(d.run('tracking.scan_line_ratio'), 0.75);
});

test('per-frame latency and drop rates are shown', () => {
  const d = dashboard();
  d.run(`updateCamera({...${frame(1)}, gateway_received_at_ms: 1000, preprocess_started_at_ms: 1030,
    preprocess_finished_at_ms: 1045});
    updateFrameResult({...${result(1)}, captured_at_ms: 0, inference_latency_ms: 80.4,
    inference_timings_ms: {lock_wait: 50}});`);
  loaded(d);
  const html = d.run("id('frameLatency').innerHTML");
  assert.match(html, /C1 <b>30 ms/);
  assert.match(html, /C2 <b>15 ms/);
  assert.match(html, /C3 <b>80 ms<\/b> \(대기 50 포함\)/);
  d.run(`updateCamera({...${frame(2)}, prompts: [], gateway_received_at_ms: 1, preprocess_started_at_ms: 2,
    preprocess_finished_at_ms: 3});`);
  loaded(d);
  assert.match(d.run("id('frameLatency').innerHTML"), /C3 <b>—<\/b> \(C3 미전송\)/);
  d.run(`updateStats({total:0,pass_count:0,fail_count:0,defect_rate:0,
    drop_window:{seconds:5,c1_drop_pct:1.5,c2_drop_pct:20,overall_loss_pct_estimate:0,c2_processed_per_s:9.6}});`);
  assert.equal(d.run("id('pDropRates').textContent"), 'C1 1.5% · C2 20% · 전체 유실 0% (추정)');
  assert.equal(d.run("id('pC2Rate').textContent"), 'C2 처리 9.6 fps');
});
