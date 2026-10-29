const byId = (id) => document.getElementById(id);
const status = byId('desktop-status');
const error = byId('desktop-error');
const screen = byId('desktop-screen');
let rfb = null;
let generation = 0;
let connected = false;
let connectionTimer = null;

function showError(message) {
  status.textContent = '未连接';
  error.textContent = message;
  error.hidden = false;
  connected = false;
  byId('desktop-paste-send').disabled = true;
}

async function connect() {
  if (document.body.dataset.desktopReady !== 'true') {
    status.textContent = '无法打开浏览器';
    byId('desktop-reconnect').disabled = true;
    return;
  }
  const attempt = ++generation;
  let failureMessage = '';
  clearTimeout(connectionTimer);
  if (rfb) rfb.disconnect();
  rfb = null;
  connected = false;
  byId('desktop-paste-send').disabled = true;
  error.hidden = true;
  status.textContent = '正在连接浏览器…';
  connectionTimer = setTimeout(() => {
    if (attempt !== generation || connected) return;
    failureMessage = '连接超时，请检查网络后重新连接。若仍失败，请检查服务器桌面服务。';
    showError(failureMessage);
    rfb?.disconnect();
  }, 15000);
  try {
    const { default: RFB } = await import('/desktop-assets/core/rfb.js');
    if (attempt !== generation) return;
    const url = new URL(`/profiles/${encodeURIComponent(document.body.dataset.profileName)}/desktop/socket`, location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    rfb = new RFB(screen, url.href);
    rfb.scaleViewport = true;
    rfb.resizeSession = false;
    rfb.showDotCursor = true;
    rfb.addEventListener('connect', () => {
      if (attempt !== generation) return;
      clearTimeout(connectionTimer);
      connected = true;
      status.textContent = '已连接 · 可直接操作浏览器';
      error.hidden = true;
      byId('desktop-paste-send').disabled = false;
      rfb.focus();
    });
    rfb.addEventListener('disconnect', () => {
      if (attempt !== generation) return;
      clearTimeout(connectionTimer);
      showError(failureMessage || '远程连接已断开。浏览器会继续运行，点击“重新连接”即可重试。');
    });
    rfb.addEventListener('credentialsrequired', () => {
      if (attempt !== generation) return;
      failureMessage = '桌面服务的认证配置与管理页面不一致，请检查服务器桌面服务配置。';
      showError(failureMessage);
      rfb.disconnect();
    });
    rfb.addEventListener('securityfailure', () => {
      if (attempt !== generation) return;
      failureMessage = '无法连接桌面服务，请检查服务器桌面服务配置后重新连接。';
      showError(failureMessage);
    });
  } catch (failure) {
    if (attempt !== generation) return;
    clearTimeout(connectionTimer);
    showError('远程操作组件加载失败，请刷新页面后重试。');
  }
}

byId('desktop-reconnect').addEventListener('click', connect);
byId('desktop-fullscreen').addEventListener('click', async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await screen.requestFullscreen();
    rfb?.focus();
  } catch (failure) {
    error.textContent = '当前浏览器无法进入全屏，请直接使用页面中的远程桌面。';
    error.hidden = false;
  }
});
byId('desktop-paste-toggle').addEventListener('click', () => {
  const panel = byId('desktop-paste-panel');
  panel.hidden = !panel.hidden;
  byId('desktop-paste-toggle').setAttribute('aria-expanded', String(!panel.hidden));
  if (!panel.hidden) byId('desktop-paste-text').focus();
});
byId('desktop-paste-send').addEventListener('click', async () => {
  const text = byId('desktop-paste-text').value;
  if (!connected || !rfb || !text) return;
  const current = rfb;
  byId('desktop-paste-send').disabled = true;
  try {
    const response = await fetch(`/profiles/${encodeURIComponent(document.body.dataset.profileName)}/desktop/clipboard`, {
      method: 'POST', body: new URLSearchParams({text}), signal: AbortSignal.timeout(8000),
    });
    if (!response.ok) throw new Error('clipboard unavailable');
    if (!connected || rfb !== current) return;
    current.focus();
    current.sendKey(0xffe3, 'ControlLeft', true);
    current.sendKey(0x0076, 'KeyV', true);
    current.sendKey(0x0076, 'KeyV', false);
    current.sendKey(0xffe3, 'ControlLeft', false);
    error.hidden = true;
    status.textContent = '文字已发送 · 可继续操作';
    byId('desktop-paste-text').value = '';
  } catch (failure) {
    error.textContent = '文字发送失败。请确认实例仍在运行，重新点击浏览器输入位置后重试。';
    error.hidden = false;
  } finally {
    byId('desktop-paste-send').disabled = !connected;
  }
});
window.addEventListener('pagehide', () => { ++generation; clearTimeout(connectionTimer); rfb?.disconnect(); });
connect();
