import { projectVisibleNodes } from './visibility.js';

const $ = id => document.getElementById(id);
const coordinateIds = ['box-x', 'box-y', 'box-width', 'box-height'];
const state = {
  index: null, summaries: [], current: null, observation: null, snapshot: null,
  capture: null, review: null, stale: false, busy: false, dirty: false,
  mode: 'existing', selectedNode: null, draft: null, generation: 0, drag: null,
  hierarchy: null, hierarchyById: new Map(),
  selectedLayers: null,
  projectedNodes: [],
};

const layerColors = ['#2563eb', '#7c3aed', '#059669', '#d97706', '#db2777', '#0891b2'];

/** Fetch failures remain visible; unsaved issues never pretend to have reached disk. */
async function request(url, options = {}) {
  const response = await fetch(url, { cache: 'no-store', ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

/** Keep all user/site text as textContent, never HTML. */
function status(message, kind = '') {
  $('status').textContent = message;
  $('status').className = kind;
}

function endpoint() {
  return '/api/review?' + new URLSearchParams({ page: state.current.id, viewport: state.observation.viewport });
}

/** Guard form edits when switching page, resolution or selected issue. */
function canDiscard() {
  return !state.dirty || confirm('当前问题尚未保存，是否放弃输入？');
}

function summaryFor(page, viewport) {
  return state.summaries.find(item => item.pageId === page && item.viewportId === viewport);
}

/** Page badges distinguish no issues from never having reviewed the screenshot. */
function renderJobs() {
  $('jobs').replaceChildren();
  const search = $('search').value.toLowerCase();
  const filter = $('review-filter').value;
  for (const page of state.index.pages) {
    const summaries = state.summaries.filter(item => item.pageId === page.id);
    const issues = summaries.reduce((sum, item) => sum + item.issues, 0);
    const reviewed = summaries.length > 0 && summaries.every(item => item.reviewed);
    if (!(page.id + ' ' + page.source).toLowerCase().includes(search) ||
        filter === 'issues' && !issues || filter === 'reviewed' && !reviewed ||
        filter === 'unreviewed' && reviewed) continue;
    const button = document.createElement('button');
    button.className = 'job' + (page === state.current ? ' active' : '');
    button.textContent = `${page.id} · ${page.status}${issues ? ` · 问题 ${issues}` : ''}${reviewed ? ' · 已检查' : ''}`;
    button.onclick = () => selectPage(page);
    $('jobs').append(button);
  }
  $('review-count').textContent = '已检查 ' + state.summaries.filter(item => item.reviewed).length +
    '/' + state.summaries.length + ' · 问题 ' + state.summaries.reduce((sum, item) => sum + item.issues, 0);
}

function renderSizes() {
  $('sizes').replaceChildren();
  for (const observation of state.current.observations) {
    const item = summaryFor(state.current.id, observation.viewport);
    const button = document.createElement('button');
    button.className = observation === state.observation ? 'active' : '';
    button.textContent = `${observation.viewport} · ${observation.status}${item?.issues ? ` · ${item.issues} 问题` : ''}${item?.reviewed ? ' · 已检查' : ''}`;
    button.onclick = () => { if (!state.busy && canDiscard()) void loadObservation(observation); };
    $('sizes').append(button);
  }
}

/** Filtering affects inspection only; it never removes recorded automatic boxes. */
function visibleNodes() {
  return state.projectedNodes.filter(node =>
    ($('box-source').value === 'raw' || matchesLayer(state.hierarchyById.get(node.nodeId))) &&
    ($('box-source').value === 'raw' || !$('struct-filter').value || state.hierarchyById.get(node.nodeId)?.struct === $('struct-filter').value) &&
    ($('box-source').value === 'raw' || !$('disposition-filter').value || state.hierarchyById.get(node.nodeId)?.disposition === $('disposition-filter').value) &&
    (!$('controls').checked || node.interactiveHint) && (!$('texts').checked || node.ownText) &&
    (!$('tag').value || node.tag === $('tag').value)).map(displayNode) || [];
}

/** Visual mode uses normalized geometry for drawing, hit testing and new issue coordinates. */
function displayNode(node) {
  const visual = $('box-source').value === 'visual' && state.hierarchyById.get(node.nodeId);
  if (!visual) return node;
  const [x, y, width, height] = visual.bbox;
  return { ...node, captureBbox: { x, y, width, height } };
}

/** Each checked layer is independent; an empty selection intentionally displays no automatic boxes. */
function matchesLayer(node) {
  return Boolean(node && state.selectedLayers?.has(node.layer));
}

function configureLayers() {
  const layers = Object.keys(state.hierarchy?.stats.by_layer || {}).map(Number).sort((a, b) => a - b);
  state.selectedLayers = state.selectedLayers === null
    ? new Set(layers.includes(1) ? [1] : layers.slice(0, 1))
    : new Set(layers.filter(layer => state.selectedLayers.has(layer)));
  $('layer-options').replaceChildren();
  for (const layer of layers) {
    const label = document.createElement('label');
    label.className = 'layer-option';
    label.style.borderColor = layerColors[layer % layerColors.length];
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.dataset.layer = String(layer);
    checkbox.checked = state.selectedLayers.has(layer);
    checkbox.onchange = () => {
      if (checkbox.checked) state.selectedLayers.add(layer); else state.selectedLayers.delete(layer);
      draw(); renderNodes();
    };
    label.append(checkbox, `Layer ${layer} · ${state.hierarchy.stats.by_layer[layer]} 框`);
    $('layer-options').append(label);
  }
  $('box-source').querySelector('option[value="visual"]').disabled = !state.hierarchy;
  const selectedStruct = $('struct-filter').value;
  $('struct-filter').replaceChildren(new Option('全部 struct', ''), ...[...new Set(
    (state.hierarchy?.nodes || []).map(node => node.struct).filter(Boolean))].sort().map(value => new Option(value, value)));
  $('struct-filter').value = selectedStruct;
  if (!state.hierarchy) $('box-source').value = 'raw';
  updateLayerControls();
}

function updateLayerControls() {
  const visual = $('box-source').value === 'visual';
  $('struct-filter').disabled = !visual;
  $('disposition-filter').disabled = !visual;
  $('layer-controls').disabled = !visual;
  $('hierarchy-info').textContent = state.hierarchy ?
    `规则候选（未经人工确认）：${state.hierarchy.stats.raw_visible} 个原始可见框 → ${state.hierarchy.stats.retained} 个保留节点 · Layer 0–${state.hierarchy.stats.max_layer} · ${state.hierarchy.rule_version}` :
    '尚未生成视觉层级；可运行 npm run layers -- --dir <结果目录>。当前显示原始框。';
  draw(); renderNodes(); renderEditor();
}

/** Resolve the selected source's parent without assuming geometric containment. */
function selectedParent() {
  if (!state.selectedNode || !state.snapshot) return null;
  let id = $('box-source').value === 'visual'
    ? state.hierarchyById.get(state.selectedNode.nodeId)?.parent_id : state.selectedNode.parentId;
  const seen = new Set();
  while (id && !seen.has(id)) {
    seen.add(id);
    const node = state.snapshot.nodes.find(node => node.nodeId === id);
    if (!node) return null;
    const projected = state.projectedNodes.find(item => item.nodeId === id);
    if (projected) return displayNode(projected);
    id = node.parentId;
  }
  return null;
}

/** Draw image-space rectangles; SVG viewBox supplies scaling without changing coordinates. */
function rectangle(bbox, color, width = 1, dashed = false) {
  if (!bbox?.every(Number.isFinite)) return;
  const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  // Inset only the painted stroke at image edges. Stored and hit-tested boxes stay unchanged.
  const painted = [...bbox];
  const inset = Math.min(width / 2, bbox[2] / 2, bbox[3] / 2);
  if (bbox[0] <= 0) { painted[0] += inset; painted[2] -= inset; }
  if (bbox[1] <= 0) { painted[1] += inset; painted[3] -= inset; }
  if (state.capture && bbox[0] + bbox[2] >= state.capture.width) painted[2] -= inset;
  if (state.capture && bbox[1] + bbox[3] >= state.capture.height) painted[3] -= inset;
  ['x', 'y', 'width', 'height'].forEach((name, index) => rect.setAttribute(name, painted[index]));
  rect.setAttribute('fill', 'none');
  rect.setAttribute('stroke', color);
  rect.setAttribute('stroke-width', width);
  rect.setAttribute('vector-effect', 'non-scaling-stroke');
  if (dashed) rect.setAttribute('stroke-dasharray', '5 3');
  $('svg').append(rect);
}

function nodeBox(node) {
  const box = node.captureBbox;
  return [box.x, box.y, box.width, box.height];
}

function draw() {
  $('svg').replaceChildren();
  if (!state.snapshot) return;
  if ($('boxes').checked) for (const node of visibleNodes()) {
    const visual = state.hierarchyById.get(node.nodeId);
    const color = $('box-source').value === 'visual' && visual
      ? layerColors[visual.layer % layerColors.length] : '#00c853';
    rectangle(nodeBox(node), color);
  }
  const parent = selectedParent();
  if ($('parent-box').checked && parent) rectangle(nodeBox(parent), '#9333ea', 2, true);
  if ($('annotations').checked && !state.stale) for (const issue of state.review?.issues || [])
    rectangle(issue.bbox, issue.kind === 'missing_box' ? '#16a34a' : '#dc2626', 2);
  if (state.draft) rectangle(state.draft.bbox,
    state.draft.kind === 'missing_box' ? '#16a34a' : '#dc2626', 3, true);
  if (state.drag) rectangle(dragBox(state.drag.start, state.drag.end), '#16a34a', 2, true);
}

function renderNodes() {
  $('nodes').replaceChildren();
  for (const node of visibleNodes()) {
    const button = document.createElement('button');
    const visual = state.hierarchyById.get(node.nodeId);
    button.textContent = `${node.nodeId} ${visual ? `L${visual.layer} ${visual.struct || ''} ` : ''}<${node.tag}> ${node.ownText.slice(0, 45)}`;
    button.onclick = () => chooseNode(node);
    $('nodes').append(button);
  }
}

/** Preview zoom never changes the captured bbox geometry. */
function resizeStage() {
  if (!state.capture) return;
  const zoom = $('zoom').value === 'fit'
    ? Math.min(1, Math.max(100, $('workspace').clientWidth - 28) / state.capture.width)
    : Number($('zoom').value);
  $('stage').style.width = `${state.capture.width * zoom}px`;
}

function renderEditor() {
  const draft = state.draft;
  $('editor-title').textContent = draft ?
    (draft.kind === 'missing_box' ? '缺失区域标记' : `已有框问题 · ${state.selectedNode?.tag || draft.tag || ''}`) : '请选择已有框，或拖拽框选缺失区域';
  $('type-label').textContent = draft?.kind === 'missing_box' ? '缺失类型（必填）' : '问题类型（必填）';
  $('issue-type').value = draft?.issueType || '';
  $('reason').value = draft?.reason || '';
  $('content').value = draft?.content || '';
  $('expected-tag').value = draft?.expectedTag || '';
  coordinateIds.forEach((id, index) => {
    $(id).value = draft ? draft.bbox[index] : '';
    $(id).readOnly = draft?.kind !== 'missing_box';
  });
  for (const element of $('issue-form').elements) element.disabled = !draft || state.busy || state.stale;
  const visual = state.selectedNode && state.hierarchyById.get(state.selectedNode.nodeId);
  $('selected-hierarchy').textContent = visual
    ? `Layer ${visual.layer} · ${visual.struct || visual.kind} / ${visual.subtype || visual.semantic_hint} · ${visual.disposition || ''} · parent: ${visual.parent_id || 'ROOT'}`
    : state.selectedNode ? '该原始节点未保留为独立视觉层级节点' : '';
  $('select-parent').hidden = !selectedParent();
  $('select-parent').disabled = state.busy;
  $('detail').textContent = JSON.stringify(state.selectedNode
    ? { ...state.selectedNode, visual_hierarchy: visual || null } : draft || '未选择', null, 2);
}

function clearDraft() {
  state.draft = null; state.selectedNode = null; state.dirty = false;
  $('hit-choices').hidden = true;
  renderEditor(); draw(); renderIssues();
}

/** Node list and overlap selector expose every nested wrapper, even identical-size boxes. */
function chooseNode(node) {
  if (state.busy || state.stale || !canDiscard()) return;
  state.selectedNode = node;
  state.draft = { id: crypto.randomUUID(), kind: 'existing_box', nodeId: node.nodeId,
    tag: node.tag, bbox: nodeBox(node), issueType: '', reason: '', content: node.ownText || '', expectedTag: '' };
  state.dirty = false;
  renderEditor(); draw(); renderIssues();
  $('inspect').scrollTop = 0;
}

function editIssue(issue) {
  if (state.busy || state.stale || !canDiscard()) return;
  state.draft = structuredClone(issue);
  state.selectedNode = state.snapshot.nodes.find(node => node.nodeId === issue.nodeId) || null;
  state.dirty = false;
  renderEditor(); draw(); renderIssues();
  $('inspect').scrollTop = 0;
}

function renderIssues() {
  $('issues').replaceChildren();
  const issues = state.stale ? [] : state.review?.issues || [];
  $('issue-count').textContent = issues.length;
  for (const issue of issues) {
    const card = document.createElement('div');
    card.className = 'issue-card' + (issue.kind === 'missing_box' ? ' missing' : '') +
      (issue.id === state.draft?.id ? ' selected' : '');
    const title = document.createElement('b');
    title.textContent = `${issue.kind === 'missing_box' ? '缺失' : issue.tag} · ${issue.issueType}`;
    const content = document.createElement('p');
    content.textContent = issue.reason || issue.content;
    const geometry = document.createElement('p');
    geometry.className = 'muted'; geometry.textContent = issue.bbox.map(n => Math.round(n * 10) / 10).join(', ');
    if (issue.kind === 'existing_box' && !state.projectedNodes.some(node => node.nodeId === issue.nodeId))
      geometry.textContent += ' · 当前自动规则已排除，保留原问题位置';
    const remove = document.createElement('button');
    remove.type = 'button'; remove.textContent = '删除'; remove.disabled = state.busy;
    remove.onclick = event => {
      event.stopPropagation();
      if (!canDiscard() || !confirm('删除这条问题记录？')) return;
      void persist(issues.filter(item => item.id !== issue.id), state.review.reviewed)
        .then(() => clearDraft()).catch(error => status(error.message, 'error'));
    };
    card.append(title, content, geometry, remove); card.onclick = () => editIssue(issue);
    $('issues').append(card);
  }
  $('reviewed').textContent = state.review?.reviewed && !state.stale ? '已检查（点击取消）' : '标记已检查';
  $('reviewed').disabled = state.busy || state.stale || !state.review;
}

/** Save explicit changes to disk, retaining server revision checks across browser tabs. */
async function persist(issues, reviewed) {
  if (state.stale) throw new Error('请先为当前截图重新开始核验');
  state.busy = true; renderEditor(); renderIssues(); status('正在保存…');
  try {
    const review = await request(endpoint(), { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ revision: state.review.revision, capture: state.capture, reviewed, issues }) });
    state.review = review;
    const item = summaryFor(state.current.id, state.observation.viewport);
    if (item) Object.assign(item, { reviewed, issues: issues.length, stale: false });
    state.dirty = false;
    status('已保存到 ' + state.observation.directory + '/human-review.json');
    renderJobs(); renderSizes();
    return review;
  } finally { state.busy = false; renderEditor(); renderIssues(); draw(); }
}

/** Failed or outdated observations cannot receive annotations under the wrong image. */
async function loadObservation(observation) {
  const generation = ++state.generation;
  state.busy = true; state.dirty = false; state.observation = observation;
  state.snapshot = null; state.review = null; state.capture = null; state.draft = null;
  state.projectedNodes = [];
  state.hierarchy = null; state.hierarchyById = new Map();
  state.selectedNode = null; state.stale = false; state.drag = null;
  $('stage').hidden = true; $('hit-choices').hidden = true; $('restart-review').hidden = true;
  renderSizes(); renderEditor(); renderIssues(); status('正在读取…');
  try {
    if (observation.status !== 'captured') throw new Error(observation.error || '该分辨率尚未采集成功');
    const [snapshot, saved, hierarchy] = await Promise.all([
      request(observation.directory + '/nodes.json'), request(endpoint()),
      fetch(observation.directory + '/hierarchy.json', { cache: 'no-store' }).then(response => response.ok ? response.json() : null),
    ]);
    if (generation !== state.generation) return;
    if (snapshot.documentId !== saved.currentCapture.documentId || snapshot.capturedAt !== saved.currentCapture.capturedAt)
      throw new Error('观测已在读取期间更新，请重新加载');
    state.snapshot = snapshot; state.capture = saved.currentCapture; state.review = saved.review;
    state.projectedNodes = projectVisibleNodes(snapshot).nodes;
    if (hierarchy?.capture.documentId === snapshot.documentId && hierarchy.capture.capturedAt === snapshot.capturedAt) {
      state.hierarchy = hierarchy;
      state.hierarchyById = new Map(hierarchy.nodes.map(node => [node.node_id, node]));
    }
    state.stale = saved.stale;
    $('image').src = observation.directory + '/' + state.capture.image;
    await $('image').decode();
    if (generation !== state.generation) return;
    $('svg').setAttribute('viewBox', `0 0 ${state.capture.width} ${state.capture.height}`);
    $('stage').hidden = false; resizeStage();
    $('message').textContent = snapshot.title + ' · ' + snapshot.nodes.length + ' 个节点 · ' +
      state.capture.width + '×' + state.capture.height + (snapshot.capture?.truncated ? '（达到限高）' : '') +
      (observation.warnings?.length ? ' · ' + observation.warnings.join('；') : '');
    const previousTag = $('tag').value;
    $('tag').replaceChildren(new Option('全部 tag', ''), ...[...new Set(snapshot.nodes.map(n => n.tag))].sort().map(tag => new Option(tag, tag)));
    $('tag').value = previousTag;
    configureLayers();
    if (state.stale) {
      $('restart-review').hidden = false;
      status('截图已重新采集，旧问题记录已过期。重新开始时会保留历史记录。', 'warning');
    } else status(saved.review.updatedAt ? '已恢复保存记录 · ' + saved.review.updatedAt : '尚无问题记录');
    const url = new URL(location.href);
    url.searchParams.set('page', state.current.id); url.searchParams.set('viewport', observation.viewport);
    history.replaceState(null, '', url);
    renderNodes(); draw();
  } catch (error) { if (generation === state.generation) status(error.message, 'error'); }
  finally {
    if (generation === state.generation) { state.busy = false; renderEditor(); renderIssues(); }
  }
}

function selectPage(page) {
  if (state.busy || !canDiscard()) return;
  state.current = page; renderJobs();
  const previous = state.observation?.viewport;
  const observation = page.observations.find(item => item.viewport === previous && item.status === 'captured') ||
    page.observations.find(item => item.status === 'captured') || page.observations[0];
  if (observation) void loadObservation(observation);
  else {
    state.snapshot = null; state.observation = null; state.review = null; state.capture = null;
    state.draft = null; state.selectedNode = null; state.dirty = false;
    $('stage').hidden = true; renderSizes(); renderEditor(); renderIssues();
    status(page.error || '没有可核验的观测', 'error');
  }
}

/** Convert pointer positions using SVG's current displayed size, including zoom and scrolling. */
function imagePoint(event) {
  const bounds = $('svg').getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(state.capture.width, (event.clientX - bounds.left) / bounds.width * state.capture.width)),
    y: Math.max(0, Math.min(state.capture.height, (event.clientY - bounds.top) / bounds.height * state.capture.height)),
  };
}

function dragBox(a, b) {
  const round = value => Math.round(value * 10) / 10;
  const left = round(Math.min(a.x, b.x)), top = round(Math.min(a.y, b.y));
  return [left, top, round(round(Math.max(a.x, b.x)) - left), round(round(Math.max(a.y, b.y)) - top)];
}

function setMode(mode) {
  state.mode = mode;
  $('select-mode').classList.toggle('active', mode === 'existing');
  $('missing-mode').classList.toggle('active', mode === 'missing');
  $('mode-help').textContent = mode === 'missing' ?
    '在图片上拖拽框选缺失区域，然后填写缺失类型和内容。可关闭自动框方便观察。' :
    '点击图片中的框，重叠的框可在下拉列表中切换；也可从右侧节点列表选择。';
}

$('svg').addEventListener('pointerdown', event => {
  if (event.button !== 0 || !state.snapshot || state.busy || state.stale) return;
  const point = imagePoint(event);
  if (state.mode === 'missing') {
    if (!canDiscard()) return;
    state.drag = { start: point, end: point };
    $('svg').setPointerCapture(event.pointerId); draw(); event.preventDefault();
  } else {
    const hits = visibleNodes().filter(node => {
      const r = node.captureBbox;
      return point.x >= r.x && point.x <= r.x + r.width && point.y >= r.y && point.y <= r.y + r.height;
    }).sort((a, b) => a.captureBbox.width * a.captureBbox.height - b.captureBbox.width * b.captureBbox.height);
    if (!hits.length) { status('此处没有自动框，可切换“框选缺失”'); return; }
    chooseNode(hits[0]);
    $('hit-choices').replaceChildren(...hits.map(node => new Option(`${node.nodeId} <${node.tag}> ${node.ownText.slice(0, 45)}`, node.nodeId)));
    $('hit-choices').hidden = hits.length < 2;
  }
});
$('svg').addEventListener('pointermove', event => {
  if (!state.drag) return;
  state.drag.end = imagePoint(event); draw();
});
$('svg').addEventListener('pointerup', event => {
  if (!state.drag) return;
  const bbox = dragBox(state.drag.start, imagePoint(event)); state.drag = null;
  if ($('svg').hasPointerCapture(event.pointerId)) $('svg').releasePointerCapture(event.pointerId);
  if (bbox[2] < 2 || bbox[3] < 2) { status('框选区域太小，请重新拖拽', 'warning'); draw(); return; }
  state.draft = { id: crypto.randomUUID(), kind: 'missing_box', bbox, issueType: '', reason: '', content: '', expectedTag: '' };
  state.selectedNode = null; state.dirty = true;
  $('hit-choices').hidden = true; renderEditor(); renderIssues(); draw(); $('issue-type').focus();
});
$('svg').addEventListener('pointercancel', () => { state.drag = null; draw(); });
$('hit-choices').onchange = () => {
  const node = state.snapshot.nodes.find(node => node.nodeId === $('hit-choices').value);
  if (node) chooseNode(node);
};
$('issue-form').oninput = () => {
  if (!state.draft) return;
  state.dirty = true;
  if (state.draft.kind === 'missing_box') { state.draft.bbox = coordinateIds.map(id => Number($(id).value)); draw(); }
};
$('issue-form').onsubmit = async event => {
  event.preventDefault();
  if (!state.draft || state.busy) return;
  const issue = { ...state.draft, issueType: $('issue-type').value.trim(), reason: $('reason').value.trim(),
    content: $('content').value.trim(), expectedTag: $('expected-tag').value.trim() };
  if (!issue.issueType || (issue.kind === 'existing_box' && !issue.reason) || (issue.kind === 'missing_box' && !issue.content)) {
    status('请填写类型，以及错误原因或缺失内容', 'error'); return;
  }
  const issues = [...state.review.issues]; const index = issues.findIndex(item => item.id === issue.id);
  if (index < 0) issues.push(issue); else issues[index] = issue;
  state.draft = issue;
  try { const saved = await persist(issues, state.review.reviewed); state.draft = structuredClone(saved.issues.find(item => item.id === issue.id)); renderEditor(); }
  catch (error) { status(error.message, 'error'); }
};
$('clear-draft').onclick = () => { if (canDiscard()) clearDraft(); };
$('reviewed').onclick = () => {
  if (state.dirty) { status('请先保存或清空当前问题', 'warning'); return; }
  void persist(state.review.issues, !state.review.reviewed).catch(error => status(error.message, 'error'));
};
$('reload').onclick = () => { if (!state.busy && state.observation && canDiscard()) void loadObservation(state.observation); };
$('restart-review').onclick = () => {
  if (!confirm('为这次截图重新核验？旧记录会在首次保存时归档。')) return;
  state.stale = false; state.review.issues = []; state.review.reviewed = false;
  $('restart-review').hidden = true; clearDraft(); status('已切换到当前截图，首次保存时归档旧记录');
};
$('select-mode').onclick = () => setMode('existing');
$('missing-mode').onclick = () => setMode('missing');
for (const id of ['boxes', 'annotations', 'controls', 'texts', 'tag', 'parent-box', 'struct-filter', 'disposition-filter'])
  $(id).onchange = () => { draw(); renderNodes(); };
$('box-source').onchange = updateLayerControls;
$('layers-all').onclick = () => {
  state.selectedLayers = new Set(Object.keys(state.hierarchy?.stats.by_layer || {}).map(Number));
  for (const checkbox of $('layer-options').querySelectorAll('input')) checkbox.checked = true;
  draw(); renderNodes();
};
$('layers-none').onclick = () => {
  state.selectedLayers = new Set();
  for (const checkbox of $('layer-options').querySelectorAll('input')) checkbox.checked = false;
  draw(); renderNodes();
};
$('select-parent').onclick = () => {
  const parent = selectedParent();
  if (!parent || state.busy || !canDiscard()) return;
  state.dirty = false;
  const visual = state.hierarchyById.get(parent.nodeId);
  if ($('box-source').value === 'visual' && visual) {
    state.selectedLayers.add(visual.layer);
    const checkbox = $('layer-options').querySelector(`input[data-layer="${visual.layer}"]`);
    if (checkbox) checkbox.checked = true;
  }
  chooseNode(parent); renderNodes();
};
$('zoom').onchange = resizeStage;
$('search').oninput = renderJobs;
$('review-filter').onchange = renderJobs;
window.addEventListener('resize', resizeStage);
window.addEventListener('beforeunload', event => { if (state.dirty || state.busy) { event.preventDefault(); event.returnValue = ''; } });

try {
  [state.index, { observations: state.summaries }] = await Promise.all([
    request('batch-index.json'), request('/api/review-summary'),
  ]);
  $('summary').textContent = `${state.index.pages.length} 页 · ${state.index.config.viewports.length} 个分辨率 · ${state.index.status}`;
  renderJobs(); renderEditor();
  const query = new URL(location.href).searchParams;
  state.observation = { viewport: query.get('viewport') };
  const page = state.index.pages.find(page => page.id === query.get('page')) || state.index.pages[0];
  if (page) selectPage(page);
} catch (error) { status('加载失败：' + error.message + '。请通过 npm run review 启动。', 'error'); }
