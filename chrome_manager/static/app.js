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

const transientError = document.querySelector(".win98-error");
if (transientError) {
  window.setTimeout(() => transientError.classList.add("is-hiding"), 4500);
  window.setTimeout(() => transientError.remove(), 5000);
}

const newProfilePanel = document.getElementById("new-profile");

function openNewProfilePanel() {
  if (!newProfilePanel) return;
  newProfilePanel.hidden = false;
  window.requestAnimationFrame(() => {
    if (!document.body.classList.contains("win98-page")) {
      newProfilePanel.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    newProfilePanel.querySelector("input")?.focus();
  });
}

document.getElementById("new-profile-button")?.addEventListener("click", openNewProfilePanel);
document.querySelectorAll("[data-open-new]").forEach((button) => {
  button.addEventListener("click", openNewProfilePanel);
});

document.getElementById("close-new-profile")?.addEventListener("click", () => {
  if (newProfilePanel) newProfilePanel.hidden = true;
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
  if (event.target === newProfilePanel) newProfilePanel.hidden = true;
});

const historyData = document.getElementById("resource-history-data");
let resourceSamples = [];
try {
  resourceSamples = JSON.parse(historyData?.textContent || "[]");
} catch {
  resourceSamples = [];
}

const chartColors = ["#22a879", "#4c8fd6", "#d58435", "#a460c2", "#db5b62", "#169aa5", "#718b32"];

function drawInstanceChart(canvas, metric, legendId) {
  if (!canvas) return;
  const context = canvas.getContext("2d");
  const bounds = canvas.getBoundingClientRect();
  const pixelRatio = window.devicePixelRatio || 1;
  const width = Math.max(280, Math.floor(bounds.width));
  const height = 190;
  canvas.width = width * pixelRatio;
  canvas.height = height * pixelRatio;
  canvas.style.height = `${height}px`;
  context.scale(pixelRatio, pixelRatio);
  context.clearRect(0, 0, width, height);
  const grid = "#dbe8e2";
  const text = "#718079";
  const left = 34, right = 38, top = 18, bottom = 27;
  const chartWidth = width - left - right, chartHeight = height - top - bottom;
  context.strokeStyle = grid;
  context.fillStyle = text;
  context.font = "11px Segoe UI";
  for (let step = 0; step <= 4; step += 1) {
    const y = top + chartHeight * step / 4;
    context.beginPath(); context.moveTo(left, y); context.lineTo(width - right, y); context.stroke();
    context.fillText(`${100 - step * 25}%`, 2, y + 4);
  }
  const instanceNames = new Map();
  resourceSamples.forEach((sample) => Object.entries(sample.instances || {}).forEach(([id, value]) => {
    instanceNames.set(id, value.name || "未命名实例");
  }));
  const series = [...instanceNames].map(([id, name], index) => ({
    id, name, color: chartColors[index % chartColors.length],
    values: resourceSamples.map((sample) => sample.instances?.[id]?.[metric] || 0),
  }));
  if (!series.length) {
    context.fillText("等待资源采样…", left, top + 28);
    return;
  }
  const maximum = metric === "cpu" ? 100 : Math.max(1, ...series.flatMap((item) => item.values));
  series.forEach((item) => {
    context.strokeStyle = item.color; context.lineWidth = 2; context.beginPath();
    item.values.forEach((value, index) => {
      const x = left + (item.values.length === 1 ? chartWidth / 2 : chartWidth * index / (item.values.length - 1));
      const y = top + chartHeight * (1 - Math.min(value, maximum) / maximum);
      index ? context.lineTo(x, y) : context.moveTo(x, y);
    });
    context.stroke();
  });
  context.fillStyle = text;
  context.fillText(metric === "cpu" ? "100%" : `${maximum.toFixed(0)} MB`, width - right + 3, top + 4);
  const legend = document.getElementById(legendId);
  if (legend) legend.innerHTML = series.map((item) => `<span><i style="background:${item.color}"></i>${item.name}</span>`).join("");
}

function renderResourceCharts() {
  drawInstanceChart(document.getElementById("cpu-resource-chart"), "cpu", "cpu-chart-legend");
  drawInstanceChart(document.getElementById("memory-resource-chart"), "memory", "memory-chart-legend");
}

function updateResourceSummary() {
  const latest = resourceSamples.at(-1);
  if (!latest?.system) return;
  const system = latest.system;
  document.getElementById("system-cpu")?.replaceChildren(`${system.cpu}%`);
  document.getElementById("system-memory")?.replaceChildren(`${system.memory_percent}%`);
  document.getElementById("system-memory-detail")?.replaceChildren(`${system.memory_used} / ${system.memory_total} GB`);
  document.getElementById("system-cpu-meter")?.style.setProperty("width", `${system.cpu}%`);
  document.getElementById("system-memory-meter")?.style.setProperty("width", `${system.memory_percent}%`);
  document.querySelectorAll("[data-profile-id]").forEach((row) => {
    const id = row.dataset.profileId;
    const instance = latest.instances?.[id];
    const usage = row.querySelector(".instance-usage");
    if (instance && usage) usage.textContent = `${usage.textContent.split(" · CPU")[0]} · CPU ${instance.cpu}% · 内存 ${instance.memory} MB`;
  });
  document.getElementById("resource-updated")?.replaceChildren(`更新于 ${new Date(latest.timestamp * 1000).toLocaleTimeString()}`);
}

if (historyData) {
  renderResourceCharts();
  window.addEventListener("resize", renderResourceCharts);
  window.setInterval(async () => {
    try {
      const response = await fetch("/api/resources");
      if (!response.ok) return;
      resourceSamples = (await response.json()).samples || [];
      updateResourceSummary();
      renderResourceCharts();
    } catch {
      // The dashboard remains usable when a refresh request briefly fails.
    }
  }, 10000);
}

// Windows 98 console: select an instance without leaving the desktop view.
const desktopProfilesNode = document.getElementById("desktop-profiles-data");
const desktopResourcesNode = document.getElementById("desktop-resource-data");
let desktopProfiles = [];
let desktopSamples = [];
try { desktopProfiles = JSON.parse(desktopProfilesNode?.textContent || "[]"); } catch { desktopProfiles = []; }
try { desktopSamples = JSON.parse(desktopResourcesNode?.textContent || "[]"); } catch { desktopSamples = []; }
const selectedProfileId = document.body.dataset.selectedProfileId;
let selectedDesktopProfile = desktopProfiles.find((profile) => String(profile.id) === selectedProfileId) || desktopProfiles[0] || null;

const detail = (id) => document.getElementById(id);
const statusLabels = { running: "运行中", stopped: "已停止", starting: "启动中", stopping: "停止中", failed: "启动失败" };

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

function drawDesktopChart(canvas, metric) {
  if (!canvas || !selectedDesktopProfile) return;
  const context = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(120, Math.floor(rect.width));
  const height = Math.max(80, Math.floor(rect.height));
  canvas.width = width * ratio; canvas.height = height * ratio;
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#000"; context.fillRect(0, 0, width, height);
  const left = 29, top = 8, right = 6, bottom = 22;
  const graphWidth = width - left - right, graphHeight = height - top - bottom;
  context.strokeStyle = "#4b4b4b"; context.lineWidth = 1;
  context.font = "9px 'Courier New'"; context.fillStyle = "#c0c0c0";
  const maximum = metric === "cpu" ? 100 : 1024;
  [maximum, maximum / 2, 0].forEach((value, index) => {
    const y = top + graphHeight * index / 2;
    context.beginPath(); context.moveTo(left, y); context.lineTo(width - right, y); context.stroke();
    context.fillText(`${value}`, 1, y + 3);
  });
  const values = desktopSamples.map((sample) => Number(sample.instances?.[String(selectedDesktopProfile.id)]?.[metric] || 0));
  if (!values.length) return;
  const tickIndexes = values.length === 1 ? [0] : [...new Set([0, Math.floor((values.length - 1) / 2), values.length - 1])];
  tickIndexes.forEach((sampleIndex, tickIndex) => {
    const x = left + (values.length === 1 ? graphWidth / 2 : graphWidth * sampleIndex / (values.length - 1));
    context.beginPath(); context.moveTo(x, top); context.lineTo(x, height - bottom); context.stroke();
    const timestamp = Number(desktopSamples[sampleIndex]?.timestamp || 0) * 1000;
    const time = timestamp ? new Date(timestamp).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "--:--:--";
    context.textAlign = values.length === 1 ? "center" : tickIndex === 0 ? "left" : tickIndex === tickIndexes.length - 1 ? "right" : "center";
    context.fillText(time, x, height - 5);
  });
  context.textAlign = "left";
  context.strokeStyle = metric === "cpu" ? "#00e000" : "#00a0ff"; context.lineWidth = 2; context.beginPath();
  values.forEach((value, index) => {
    const x = left + (values.length === 1 ? graphWidth / 2 : graphWidth * index / (values.length - 1));
    const y = top + graphHeight * (1 - Math.min(value, maximum) / maximum);
    index ? context.lineTo(x, y) : context.moveTo(x, y);
  });
  context.stroke();
}

let desktopChartFrame = 0;

function scheduleDesktopCharts() {
  window.cancelAnimationFrame(desktopChartFrame);
  desktopChartFrame = window.requestAnimationFrame(() => {
    drawDesktopChart(detail("desktop-cpu-chart"), "cpu");
    drawDesktopChart(detail("desktop-memory-chart"), "memory");
  });
}

function renderDesktopDetails() {
  const profile = selectedDesktopProfile;
  if (!profile) return;
  detail("detail-title").textContent = profile.display_name;
  detail("detail-status").textContent = statusLabels[profile.status] || profile.status;
  detail("detail-lamp").className = `status-lamp ${["running", "starting"].includes(profile.status) ? "running" : profile.status === "failed" ? "failed" : "stopped"}`;
  detail("detail-cpu").textContent = `${profile.cpu || 0}%`;
  detail("detail-memory").textContent = `${profile.memory || 0} MB`;
  detail("detail-pid").textContent = profile.pid || "—";
  detail("detail-port").textContent = profile.port || "—";
  detail("detail-uptime").textContent = profile.status === "running" ? uptime(profile.started_at) : "—";
  detail("detail-proxy").textContent = profile.proxy || "直连";
  detail("detail-description").textContent = profile.description || "暂无描述";
  detail("focus-form").action = actionPath(profile, "focus");
  detail("restart-form").action = actionPath(profile, "restart");
  detail("stop-form").action = actionPath(profile, "stop");
  detail("start-form").action = actionPath(profile, "start");
  detail("delete-form").action = actionPath(profile, "delete");
  detail("edit-link").href = actionPath(profile, "edit");
  const running = profile.status === "running";
  detail("focus-form").hidden = !running; detail("restart-form").hidden = !running; detail("stop-form").hidden = !running;
  detail("start-form").hidden = running;
  detail("edit-link").hidden = running; detail("delete-form").hidden = running;
  document.querySelectorAll(".win98-list-item").forEach((item) => {
    const selected = String(profile.id) === item.dataset.profileId;
    item.classList.toggle("selected", selected); item.setAttribute("aria-selected", String(selected));
  });
  scheduleDesktopCharts();
}

document.querySelectorAll(".win98-list-item").forEach((item) => item.addEventListener("click", () => {
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
window.addEventListener("resize", scheduleDesktopCharts);

if (desktopProfilesNode) {
  renderDesktopDetails();
  window.setInterval(async () => {
    try {
      const response = await fetch("/api/resources");
      if (!response.ok) return;
      desktopSamples = (await response.json()).samples || [];
      const latest = desktopSamples.at(-1);
      if (latest?.system) {
        detail("desktop-cpu-summary").textContent = `${latest.system.cpu}%`;
        detail("desktop-memory-summary").textContent = `${latest.system.memory_percent}%`;
      }
      const instance = latest?.instances?.[String(selectedDesktopProfile?.id)];
      if (instance && selectedDesktopProfile) { selectedDesktopProfile.cpu = instance.cpu; selectedDesktopProfile.memory = instance.memory; }
      renderDesktopDetails();
    } catch { /* A transient local refresh error does not interrupt management. */ }
  }, 10000);
}

const deleteForm = detail("delete-form");
const deleteDialog = detail("delete-confirmation");
const deleteLabel = detail("delete-profile-label");
let pendingDeleteForm = null;

deleteForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  pendingDeleteForm = deleteForm;
  if (deleteLabel) deleteLabel.textContent = selectedDesktopProfile?.display_name || "该实例";
  if (deleteDialog) deleteDialog.hidden = false;
});

detail("cancel-delete")?.addEventListener("click", () => {
  if (deleteDialog) deleteDialog.hidden = true;
  pendingDeleteForm = null;
});

detail("confirm-delete")?.addEventListener("click", () => {
  if (!pendingDeleteForm) return;
  const form = pendingDeleteForm;
  pendingDeleteForm = null;
  if (deleteDialog) deleteDialog.hidden = true;
  form.submit();
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
