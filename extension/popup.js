const $ = (id) => document.getElementById(id);

function fmt(ts) {
  if (!ts) return '-';
  const d = new Date(ts), p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function toast(msg, ok = true) {
  const t = $('toast');
  t.textContent = msg;
  t.style.color = ok ? '#00b42a' : '#f53f3f';
  setTimeout(() => { t.textContent = ''; }, 3500);
}

let curRole = null;

async function refresh() {
  let s;
  try { s = await chrome.runtime.sendMessage({ cmd: 'status' }); }
  catch (e) { $('err').textContent = '无法连接后台: ' + e.message; return; }
  if (!s) return;
  $('browser').textContent = s.browser || '-';
  $('dot').className = 'dot ' + (s.healthy ? 'ok' : 'bad');
  $('srv').textContent = s.healthy ? '已连接' : '未连接';
  $('masterCnt').textContent = (s.healthy && s.server && s.server.urls != null) ? (s.server.urls + ' 条') : '-';
  curRole = s.cfg.role || 'follower';
  $('role').value = curRole;
  $('lp').textContent = fmt(s.cfg.lastPush);
  $('ll').textContent = fmt(s.cfg.lastPull);
  $('la').textContent = (s.cfg.lastAdded != null) ? (s.cfg.lastAdded + ' 条') : '-';
  $('err').textContent = s.cfg.lastError || '';
  if (!s.healthy) {
    $('err').textContent = '服务未启动 —— 请运行本地服务（见 README）';
  }
}

$('btnPush').onclick = async () => {
  const b = $('btnPush'); b.disabled = true; toast('推送中…');
  const r = await chrome.runtime.sendMessage({ cmd: 'push' });
  b.disabled = false;
  if (r && r.ok) toast(`推送成功，汇总新增 ${r.added} 条`);
  else toast('推送失败（服务没启动？）', false);
  refresh();
};

$('btnPull').onclick = async () => {
  const b = $('btnPull'); b.disabled = true; toast('拉取中…');
  const r = await chrome.runtime.sendMessage({ cmd: 'pull', apply: true });
  b.disabled = false;
  if (r) toast(`拉取成功：汇总 ${r.masterUrls} 条，本浏览器补齐 ${r.added} 条`);
  else toast('拉取失败（服务没启动？）', false);
  refresh();
};

$('btnSave').onclick = async () => {
  const role = $('role').value;
  if (role === curRole) { toast('角色没有变化'); return; }
  await chrome.runtime.sendMessage({ cmd: 'save', data: { role } });
  toast(role === 'master' ? '已设为主（书签权威来源）' : '已设为从（自动跟随主）');
  refresh();
};

refresh();
setInterval(refresh, 4000);
