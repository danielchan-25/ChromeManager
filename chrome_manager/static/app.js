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

function openNewProfilePanel() {
  if (!newProfilePanel) return;
  newProfilePanel.hidden = false;
  window.requestAnimationFrame(() => {
    newProfilePanel.scrollIntoView({ behavior: "smooth", block: "start" });
    document.getElementById("profile-name")?.focus();
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

const themeMode = document.getElementById("theme-mode");
const savedTheme = localStorage.getItem("chrome-manager:theme") || "system";

function applyTheme(mode) {
  const hour = new Date().getHours();
  const resolved = mode === "system" ? (hour >= 7 && hour < 19 ? "light" : "dark") : mode;
  document.documentElement.dataset.theme = resolved;
  if (themeMode) themeMode.value = mode;
}

applyTheme(savedTheme);
themeMode?.addEventListener("change", () => {
  localStorage.setItem("chrome-manager:theme", themeMode.value);
  applyTheme(themeMode.value);
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
  const dark = document.documentElement.dataset.theme === "dark";
  const grid = dark ? "#365046" : "#dbe8e2";
  const text = dark ? "#b9ccc3" : "#718079";
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
