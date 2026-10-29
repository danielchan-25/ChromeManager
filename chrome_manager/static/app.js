const scrollKey = "chrome-manager:restore-scroll";

document.addEventListener("submit", (event) => {
  if (event.target instanceof HTMLFormElement && event.target.hasAttribute("data-preserve-scroll")) {
    sessionStorage.setItem(scrollKey, String(window.scrollY));
  }
});

const savedScroll = sessionStorage.getItem(scrollKey);
if (savedScroll !== null) {
  window.addEventListener("load", () => {
    window.scrollTo({ top: Number(savedScroll), behavior: "auto" });
    sessionStorage.removeItem(scrollKey);
  }, { once: true });
}

const newProfilePanel = document.getElementById("new-profile");
let modalReturnFocus = null;

function showModal(panel, focusSelector) {
  if (!panel) return;
  modalReturnFocus = document.activeElement;
  panel.hidden = false;
  window.requestAnimationFrame(() => panel.querySelector(focusSelector)?.focus());
}

function hideModal(panel) {
  if (!panel) return;
  panel.hidden = true;
  modalReturnFocus?.focus();
  modalReturnFocus = null;
}

function openNewProfilePanel() {
  showModal(newProfilePanel, 'input');
}

document.getElementById("new-profile-button")?.addEventListener("click", openNewProfilePanel);
document.querySelectorAll("[data-open-new]").forEach((button) => {
  button.addEventListener("click", openNewProfilePanel);
});

document.getElementById("close-new-profile")?.addEventListener("click", () => {
  hideModal(newProfilePanel);
});

const instanceSearch = document.getElementById("instance-search");
const instanceCards = Array.from(document.querySelectorAll(".instance-card"));

instanceSearch?.addEventListener("input", () => {
  const query = instanceSearch.value.trim().toLocaleLowerCase();
  instanceCards.forEach((card) => {
    const searchText = (card.dataset.search || "").toLocaleLowerCase();
    card.hidden = Boolean(query) && !searchText.includes(query);
  });
});

document.getElementById("profile-sort")?.addEventListener("change", (event) => {
  const url = new URL(window.location.href);
  url.searchParams.set("sort", event.target.value);
  window.location.assign(url);
});

newProfilePanel?.addEventListener("click", (event) => {
  if (event.target === newProfilePanel) hideModal(newProfilePanel);
});

// Select an instance without leaving the table or losing pending form choices.
const desktopProfilesNode = document.getElementById("desktop-profiles-data");
let desktopProfiles = [];
try { desktopProfiles = JSON.parse(desktopProfilesNode?.textContent || "[]"); } catch { desktopProfiles = []; }
const selectedProfileId = document.body.dataset.selectedProfileId;
let selectedDesktopProfile = desktopProfiles.find((profile) => String(profile.id) === selectedProfileId) || desktopProfiles[0] || null;

const detail = (id) => document.getElementById(id);
const statusLabels = { running: "运行中", stopped: "已停止", starting: "启动中", stopping: "停止中", failed: "启动失败", maintenance: "数据维护中" };

// Reserve the dock's actual height so the final table row can scroll above it.
const detailsDock = detail('instance-details');
if (detailsDock) {
  const reserveDockSpace = () => document.documentElement.style.setProperty(
    '--detail-dock-height', `${Math.ceil(detailsDock.getBoundingClientRect().height)}px`);
  reserveDockSpace();
  new ResizeObserver(reserveDockSpace).observe(detailsDock);
}

function actionPath(profile, action) {
  const path = `/profiles/${encodeURIComponent(profile.name)}/${action}`;
  return action === "edit" ? path : `${path}?selected=${encodeURIComponent(profile.name)}`;
}

function uptime(startedAt) {
  if (!startedAt) return "—";
  const started = new Date(`${startedAt.replace(" ", "T")}Z`).getTime();
  if (Number.isNaN(started)) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000));
  const hours = String(Math.floor(seconds / 3600)).padStart(2, "0");
  const minutes = String(Math.floor(seconds % 3600 / 60)).padStart(2, "0");
  const remaining = String(seconds % 60).padStart(2, "0");
  return `${hours}:${minutes}:${remaining}`;
}

let restoreProfileId = null;
let restoreBackupIds = "";

function renderDesktopDetails() {
  const profile = selectedDesktopProfile;
  if (!profile) return;
  detail("detail-title").textContent = profile.display_name;
  detail("detail-status").textContent = statusLabels[profile.status] || profile.status;
  detail("detail-lamp").className = `status-lamp ${["running", "starting"].includes(profile.status) ? "running" : profile.status === "failed" ? "failed" : "stopped"}`;
  const cdpIndicator = detail("detail-cdp");
  cdpIndicator.hidden = profile.status !== "running";
  cdpIndicator.textContent = profile.cdp_health === "failed" ? "CDP 连接失败" : profile.cdp_health === "ok" ? "CDP 正常" : "CDP 检测中";
  cdpIndicator.classList.toggle("failed", profile.cdp_health === "failed");
  const cdpReason = detail("detail-cdp-reason");
  if (cdpReason) {
    cdpReason.hidden = profile.status !== "running" || profile.cdp_health !== "failed";
    cdpReason.textContent = profile.cdp_reason || "";
  }
  detail("detail-pid").textContent = profile.pid || "—";
  detail("detail-port").textContent = profile.port || "—";
  detail("detail-uptime").textContent = profile.status === "running" ? uptime(profile.started_at) : "—";
  detail("detail-proxy").textContent = profile.proxy || "直连";
  detail("detail-description").textContent = profile.description || "暂无描述";
  detail("focus-form").action = `${actionPath(profile, "focus")}&view=true`;
  detail("restart-form").action = actionPath(profile, "restart");
  detail("stop-form").action = actionPath(profile, "stop");
  detail("start-form").action = actionPath(profile, "start");
  detail("delete-form").action = actionPath(profile, "delete");
  detail("backup-form").action = actionPath(profile, "backup");
  detail("restore-form").action = actionPath(profile, "restore");
  detail("edit-link").href = actionPath(profile, "edit");
  detail("profile-link").href = `/profiles/${encodeURIComponent(profile.name)}`;
  const running = profile.status === "running";
  detail("focus-form").hidden = !running; detail("restart-form").hidden = !running; detail("stop-form").hidden = !running;
  const stopped = profile.status === "stopped";
  detail("start-form").hidden = !stopped;
  detail("edit-link").hidden = !stopped; detail("delete-form").hidden = !stopped; detail("backup-form").hidden = !stopped;
  const restoreForm = detail("restore-form");
  restoreForm.hidden = !stopped || !profile.backups?.length;
  const backupSelect = detail("restore-backup-select");
  const backupIds = JSON.stringify((profile.backups || []).map(backup => backup.id));
  if (restoreProfileId !== profile.id || restoreBackupIds !== backupIds) {
    const previousChoice = restoreProfileId === profile.id ? backupSelect.value : null;
    backupSelect.replaceChildren(...(profile.backups || []).map((backup) => {
      const option = document.createElement("option");
      option.value = backup.id;
      option.textContent = `${backup.created_at}${backup.kind === "pre-restore" ? "（恢复前）" : ""}`;
      return option;
    }));
    if (previousChoice && [...backupSelect.options].some(option => option.value === previousChoice)) {
      backupSelect.value = previousChoice;
    }
    restoreForm.querySelector('[name="confirmed"]').checked = false;
    restoreProfileId = profile.id;
    restoreBackupIds = backupIds;
  }
  document.querySelectorAll(".instance-row").forEach((item) => {
    const selected = String(profile.id) === item.dataset.profileId;
    item.classList.toggle("selected", selected);
    item.querySelector('.instance-select').setAttribute("aria-pressed", String(selected));
    const rowProfile = desktopProfiles.find(candidate => String(candidate.id) === item.dataset.profileId);
    if (!rowProfile) return;
    item.dataset.status = rowProfile.status;
    const status = item.querySelector('.row-status');
    status.textContent = statusLabels[rowProfile.status] || rowProfile.status;
    status.className = `row-status status-${rowProfile.status}`;
    const cdp = item.querySelector('.row-cdp');
    cdp.textContent = rowProfile.status !== 'running' ? '—' : rowProfile.cdp_health === 'ok' ? '正常' : rowProfile.cdp_health === 'failed' ? 'CDP 连接失败' : '检测中';
    cdp.classList.toggle('failed', rowProfile.status === 'running' && rowProfile.cdp_health === 'failed');
    cdp.title = rowProfile.cdp_reason || '';
  });
  detail('summary-running').textContent = desktopProfiles.filter(item => item.status === 'running').length;
  detail('summary-stopped').textContent = desktopProfiles.filter(item => item.status === 'stopped').length;
}

document.querySelectorAll(".instance-row").forEach((item) => item.addEventListener("click", () => {
  const nextProfile = desktopProfiles.find((profile) => String(profile.id) === item.dataset.profileId);
  if (!nextProfile || nextProfile.id === selectedDesktopProfile?.id) return;
  selectedDesktopProfile = nextProfile;
  renderDesktopDetails();
}));

function renderDesktopClock() {
  const clock = detail("desktop-clock");
  if (clock) clock.textContent = new Date().toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-");
  if (selectedDesktopProfile?.status === "running") detail("detail-uptime").textContent = uptime(selectedDesktopProfile.started_at);
}
renderDesktopClock(); window.setInterval(renderDesktopClock, 1000);

if (desktopProfilesNode) {
  renderDesktopDetails();
  let refreshPending = false;
  window.setInterval(async () => {
    if (refreshPending) return;
    refreshPending = true;
    const health = document.getElementById("status-warning");
    try {
      const response = await fetch("/api/status", {signal: AbortSignal.timeout(8000)});
      if (!response.ok) throw new Error("status refresh failed");
      const payload = await response.json();
      desktopProfiles.forEach(profile => {
        const instance = payload.instances?.[String(profile.id)];
        if (instance) Object.assign(profile, {status: instance.status || profile.status});
        const runtime = payload.runtime?.[String(profile.id)];
        if (runtime) Object.assign(profile, runtime);
        else if (payload.runtime) Object.assign(profile, {pid: null, started_at: null});
        const cdp = payload.cdp?.[String(profile.id)];
        profile.cdp_health = profile.status !== "running" ? "stopped" : cdp?.pid === profile.pid ? cdp.state : "checking";
        profile.cdp_reason = profile.status === "running" && cdp?.pid === profile.pid ? cdp.reason || "" : "";
      });
      const failedCount = desktopProfiles.filter(profile => profile.status === "running" && profile.cdp_health === "failed").length;
      if (health) health.textContent = [payload.warning, payload.cdp_warning, failedCount ? `${failedCount} 个实例的 CDP 连接失败` : ""].filter(Boolean).join("；");
      detail('data-status').textContent = '每 10 秒自动刷新';
      renderDesktopDetails();
    } catch {
      if (health) health.textContent = "无法连接管理服务，数据未更新；正在重试";
      detail('data-status').textContent = '数据未更新 · 正在重试';
    } finally { refreshPending = false; }
  }, 10000);
}

const deleteForm = detail("delete-form");
const deleteDialog = detail("delete-confirmation");
const deleteLabel = detail("delete-profile-label");
let pendingDeleteAction = null;

deleteForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  pendingDeleteAction = deleteForm.action;
  if (deleteLabel) deleteLabel.textContent = selectedDesktopProfile?.display_name || "该实例";
  showModal(deleteDialog, '#cancel-delete');
});

detail("cancel-delete")?.addEventListener("click", () => {
  hideModal(deleteDialog);
  pendingDeleteAction = null;
});

detail("confirm-delete")?.addEventListener("click", () => {
  if (!pendingDeleteAction) return;
  const action = pendingDeleteAction;
  pendingDeleteAction = null;
  if (deleteDialog) deleteDialog.hidden = true;
  deleteForm.action = action;
  deleteForm.submit();
});

document.addEventListener('keydown', (event) => {
  const panel = [newProfilePanel, deleteDialog].find(item => item && !item.hidden);
  if (!panel) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    if (panel === deleteDialog) pendingDeleteAction = null;
    hideModal(panel);
  } else if (event.key === 'Tab') {
    const controls = [...panel.querySelectorAll('button, input, select, textarea, a[href]')]
      .filter(item => !item.disabled && item.getClientRects().length);
    const first = controls[0], last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
});

document.querySelectorAll("[data-proxy-toggle]").forEach((toggle) => {
  const input = detail(toggle.dataset.proxyToggle);
  const field = input?.closest("[data-proxy-field]");
  const syncProxyField = () => {
    if (!input) return;
    if (field) field.hidden = !toggle.checked;
    input.disabled = !toggle.checked;
    input.required = toggle.checked;
    if (!toggle.checked) input.value = "";
  };
  toggle.addEventListener("change", syncProxyField);
  syncProxyField();
});

// The browser opens this page before startup finishes; keep its status live.
const infoView = document.querySelector('.profile-info-view[data-profile-id]');
if (infoView) {
  const refreshInfo = async () => {
    const warning = document.getElementById('profile-refresh-warning');
    try {
      const response = await fetch('/api/status', {cache: 'no-store', signal: AbortSignal.timeout(8000)});
      if (!response.ok) throw new Error('Status unavailable');
      const payload = await response.json();
      const instance = payload.instances?.[infoView.dataset.profileId];
      if (!instance || Date.now() / 1000 - payload.checked_at > 30) throw new Error('Stale status');
      infoView.querySelector('.detail-status strong').textContent = statusLabels[instance.status] || instance.status;
      infoView.querySelector('.status-lamp').className = `status-lamp ${['running', 'starting'].includes(instance.status) ? 'running' : instance.status === 'failed' ? 'failed' : 'stopped'}`;
      warning.textContent = payload.warning || '';
    } catch {
      warning.textContent = '状态未更新：无法获取最新状态，正在重试';
    } finally {
      window.setTimeout(refreshInfo, 5000);
    }
  };
  refreshInfo();
}
