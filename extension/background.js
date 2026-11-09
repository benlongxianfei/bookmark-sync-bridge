// Bookmark Sync Bridge - background service worker
// ------------------------------------------------------------
// master  : the authoritative browser. Pushes on every change; never pulls.
// follower: mirrors the master. Pulls on browser startup and daily at 01:00.
//           Never pushes automatically - use the manual "Push now" button.
// ------------------------------------------------------------

const SERVER = 'http://127.0.0.1:8765';
const ALARM_PUSH = 'bm-push';   // 兜底推送（仅 master）
const ALARM_PULL = 'bm-pull';   // 拉取补齐（仅 follower）
const FOLDER_KEYS = ['bookmark_bar', 'other', 'synced'];

function log(...a) { console.log('[书签同步]', ...a); }

async function getCfg() {
  return await chrome.storage.local.get({
    role: 'follower',      // 'master' | 'follower'; set master on the primary browser
    lastPush: 0,
    lastPull: 0,
    lastError: ''
  });
}

// ---------------- 读取本浏览器书签 ----------------
function conv(node) {
  const o = { type: 'folder', name: node.title || '', children: [] };
  for (const c of (node.children || [])) {
    if (c.url) o.children.push({ type: 'url', name: c.title || '', url: c.url });
    else o.children.push(conv(c));
  }
  return o;
}

async function readLocalTree() {
  const [root] = await chrome.bookmarks.getTree();
  const res = { roots: {}, version: 1 };
  // Chrome 规范：children[0]=书签栏 [1]=其他书签 [2]=移动设备书签
  const kids = root.children || [];
  for (let i = 0; i < FOLDER_KEYS.length && i < kids.length; i++) {
    res.roots[FOLDER_KEYS[i]] = conv(kids[i]);
  }
  return { tree: res, rootId: root.id, kids };
}

function countUrls(node, set) {
  for (const c of (node.children || [])) {
    // BookmarkTreeNode has no "type" field; presence of url marks a bookmark.
    if (c.url) set.add(c.url); else countUrls(c, set);
  }
  return set;
}

// ---------------- 推送 ----------------
let pushTimer = null;
function schedulePush(delay = 1500) {
  if (pushTimer) clearTimeout(pushTimer);
  pushTimer = setTimeout(() => { doPush(); }, delay);
}

async function doPush() {
  try {
    const { role } = await getCfg();
    const { tree } = await readLocalTree();
    const urls = countUrls(tree.roots.bookmark_bar || { children: [] }, new Set());
    for (const k of FOLDER_KEYS) if (tree.roots[k]) countUrls(tree.roots[k], urls);
    const r = await fetch(SERVER + '/push', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // master pushes replace the master tree (including deletions); follower pushes are additive
      body: JSON.stringify({ browser: browserName(), role, tree })
    });
    const j = await r.json();
    await chrome.storage.local.set({ lastPush: Date.now(), lastError: '', lastCount: urls.size });
    log('推送完成', urls.size, j);
    return j;
  } catch (e) {
    await chrome.storage.local.set({ lastError: '推送失败: ' + e.message });
    log('推送失败', e);
    return null;
  }
}

// ---------------- 拉取 + 写回 ----------------
async function getFolderId(baseId, pathParts, cache) {
  let cur = baseId;
  for (const name of pathParts) {
    const key = cur + '\u0001' + name;
    if (cache.has(key)) { cur = cache.get(key); continue; }
    let kids = await chrome.bookmarks.getChildren(cur);
    let found = kids.find(k => !k.url && (k.title || '') === name);
    if (!found) found = await chrome.bookmarks.create({ parentId: cur, title: name });
    cache.set(key, found.id);
    cur = found.id;
  }
  return cur;
}

async function applyTree(masterTree) {
  // Guard: skip alignment when the master tree looks abnormal (empty or a
  // sudden drop), to avoid wiping the follower's bookmarks.
  const localUrls = new Set();
  try {
    const [r0] = await chrome.bookmarks.getTree();
    (function w(n) { for (const c of (n.children || [])) { if (c.url) localUrls.add(c.url); else w(c); } })(r0);
  } catch (e) { }
  try {
    const all = await chrome.bookmarks.search({});
    for (const n of all) if (n.url) localUrls.add(n.url);
  } catch (e) { }

  const masterUrls = new Set();
  const MR = masterTree.roots || {};
  for (const k of FOLDER_KEYS) {
    const r = MR[k];
    if (r) (function w(n) { for (const c of (n.children || [])) { if (c.url) masterUrls.add(c.url); else w(c); } })(r);
  }

  if (masterUrls.size === 0) return 0;              // 主树为空，不动
  if (localUrls.size === 0) {
    // Startup race: an empty local tree means the bookmark model is not ready.
    log('本地为 0（书签模型未就绪），跳过本轮');
    return 0;
  }
  if (masterUrls.size < localUrls.size * 0.7) {
    log('Guard: master tree shrank sharply, skip alignment');
    return 0;
  }

  const [root] = await chrome.bookmarks.getTree();
  const kids = root.children || [];
  const baseIds = {};
  for (let i = 0; i < FOLDER_KEYS.length && i < kids.length; i++) baseIds[FOLDER_KEYS[i]] = kids[i].id;

  // Compare ordered structure to detect add / delete / rename / move / reorder
  const mFp = FOLDER_KEYS.map((k, i) => fp((MR[k] || { children: [] }), 'name')).join('||');
  const lFp = FOLDER_KEYS.map((k, i) => fp((kids[i] || { children: [] }), 'title')).join('||');
  if (mFp === lFp) {
    log('本浏览器与主树完全一致，无需对齐');
    return 0;
  }
  log('Difference detected, start full alignment');

  // Archive the current local tree before rebuilding, so mistakes are recoverable
  try {
    const localTree = conv(root);
    await fetch(SERVER + '/push', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ browser: browserName() + '-对齐前', role: 'follower', tree: localTree })
    });
    log('Local tree archived before alignment');
  } catch (e) { log('备份失败（继续对齐）', e); }

  // Rebuild from the master tree; an incrementing index preserves ordering
  let created = 0;
  for (let i = 0; i < FOLDER_KEYS.length; i++) {
    const base = baseIds[FOLDER_KEYS[i]];
    const src = MR[FOLDER_KEYS[i]];
    if (!base || !src) continue;
    // 清空该根容器
    try {
      const chs = await chrome.bookmarks.getChildren(base);
      for (const c of chs) {
        try { await chrome.bookmarks.removeTree(c.id); } catch (e) { }
      }
    } catch (e) { log('清空失败', e); }
    // 按主树重建
    async function build(node, parentId) {
      let idx = 0;
      for (const c of (node.children || [])) {
        if (c.children) {                    // 文件夹（空文件夹也保留）
          const nf = await chrome.bookmarks.create({ parentId, index: idx++, title: c.name || '' });
          await build(c, nf.id);
        } else if (c.url) {
          await chrome.bookmarks.create({ parentId, index: idx++, title: c.name || c.url, url: c.url });
          created++;
        }
      }
    }
    await build(src, base);
  }
  log('Alignment complete, rebuilt ' + created + ' bookmarks');
  return created;
}

// Serialize an ordered bookmark tree for structural comparison.
// Folders are detected by the presence of "children" (getTree() has no type field).
function fp(node, nameKey) {
  let s = '';
  for (const c of (node.children || [])) {
    if (c.children) {
      s += '[' + (c[nameKey] || '') + fp(c, nameKey) + ']';
    } else if (c.url) {
      s += '(' + c.url + '|' + (c[nameKey] || '') + ')';
    }
  }
  return s;
}

// Serialize alignment runs: startup events can fire more than one pull.
let applying = false;

async function doPull(autoApply) {
  if (applying) { log('已有拉取在进行，跳过本次'); return null; }
  applying = true;
  try {
    const r = await fetch(SERVER + '/pull');
    const j = await r.json();
    if (!j.ok) throw new Error('服务返回异常');
    const masterUrls = countUrls(j.tree.roots.bookmark_bar || { children: [] }, new Set());
    for (const k of FOLDER_KEYS) if (j.tree.roots[k]) countUrls(j.tree.roots[k], masterUrls);
    let added = 0;
    if (autoApply) added = await applyTree(j.tree);
    await chrome.storage.local.set({
      lastPull: Date.now(), lastError: '',
      lastMasterCount: masterUrls.size, lastAdded: added
    });
    log('拉取完成 主树', masterUrls.size, '补齐', added);
    return { masterUrls: masterUrls.size, added };
  } catch (e) {
    await chrome.storage.local.set({ lastError: '拉取失败: ' + e.message });
    log('拉取失败', e);
    return null;
  } finally {
    applying = false;      // always release, including on error
  }
}

// ---------------- 浏览器标识 ----------------
function browserName() {
  const ua = navigator.userAgent;
  if (/Waxiang/i.test(ua)) return 'waxiang';
  if (/Edg\//.test(ua)) return 'edge';
  if (/Quark|quark/i.test(ua)) return 'quark';
  if (/UCBrowser|UBrowser/i.test(ua)) return 'uc';
  if (/Chrome\//.test(ua)) return 'chrome';
  return 'unknown';
}

// ---------------- Events and timers ----------------
// master   -> push on change + a periodic fallback push
// follower -> pull on startup + once daily
async function setupAlarms() {
  const { role } = await getCfg();
  await chrome.alarms.clearAll();
  if (role === 'master') {
    chrome.alarms.create(ALARM_PUSH, { periodInMinutes: 15 });   // 兜底推送
    log('定时已设置：master 只推（15min 兜底）');
  } else {
    // Next 01:00, then every 24h
    const next = new Date();
    next.setHours(1, 0, 0, 0);
    if (next.getTime() <= Date.now()) next.setDate(next.getDate() + 1);
    chrome.alarms.create(ALARM_PULL, { when: next.getTime(), periodInMinutes: 1440 });
    log('定时已设置：follower 每天凌晨 1:00 自动对齐（下次 ' + next.toLocaleString() + '）');
  }
}

// Only the master pushes automatically; followers require a manual push
for (const ev of ['onCreated', 'onRemoved', 'onChanged', 'onMoved', 'onChildrenReordered']) {
  if (chrome.bookmarks[ev] && chrome.bookmarks[ev].addListener) {
    chrome.bookmarks[ev].addListener(async () => {
      const { role } = await getCfg();
      if (role !== 'master') return;
      // 忽略刚由回写自己创建的书签造成的事件
      if (applying) return;
      schedulePush(2000);
    });
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  const { role } = await getCfg();
  log('扩展已安装，角色=' + role);
  await setupAlarms();
  if (role === 'master') {
    schedulePush(3000);                                           // 主：推一次
  } else {
    setTimeout(() => doPull(true), 3000);                         // 从：拉一次
  }
});

chrome.runtime.onStartup.addListener(async () => {
  const { role } = await getCfg();
  log('浏览器启动，角色=' + role);
  await setupAlarms();
  if (role === 'master') {
    // 主：启动后把当前书签推给服务端
    setTimeout(() => doPush(), 3000);
  } else {
    // 从：打开浏览器时自动从主拉取一次（主 -> 从，单向）
    setTimeout(() => doPull(true), 3000);
  }
});

chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name === ALARM_PUSH) { await doPush(); }
  if (a.name === ALARM_PULL) { await doPull(true); }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg.cmd === 'push') sendResponse(await doPush());
    else if (msg.cmd === 'pull') sendResponse(await doPull(msg.apply !== false));
    else if (msg.cmd === 'status') {
      const c = await getCfg();
      let healthy = false, info = {};
      try {
        const r = await fetch(SERVER + '/health'); info = await r.json(); healthy = !!info.ok;
      } catch (e) { info = { error: e.message }; }
      sendResponse({ cfg: c, server: info, healthy, browser: browserName() });
    } else if (msg.cmd === 'save') {
      await chrome.storage.local.set(msg.data || {});
      await setupAlarms();
      // 切到 master 就推一次；切到 follower 就拉一次，立即生效
      const { role } = await getCfg();
      if (role === 'master') { doPush(); } else { doPull(true); }
      sendResponse({ ok: true, role });
    }
  })();
  return true;
});

setupAlarms();
