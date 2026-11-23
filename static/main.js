const METRIC_DEFS = [
  { key: "PER", title: "PER", subtitle: "주가수익비율 (배)", unit: "x" },
  { key: "PBR", title: "PBR", subtitle: "주가순자산비율 (배)", unit: "x" },
  { key: "PSR", title: "PSR", subtitle: "주가매출비율 (배)", unit: "x" },
  { key: "PEG", title: "PEG Ratio", subtitle: "PER / 이익성장률", unit: "x" },
  { key: "EV_EBITDA", title: "EV/EBITDA", subtitle: "기업가치 / EBITDA (배)", unit: "x" },
  { key: "DividendYield", title: "Dividend Yield", subtitle: "연간배당금 / 주가", unit: "%" },
  { key: "FCFYield", title: "FCF Yield", subtitle: "잉여현금흐름 / 시가총액", unit: "%" },
  { key: "ROE", title: "ROE", subtitle: "자기자본이익률 (순이익/자기자본)", unit: "%" },
  { key: "ROIC", title: "ROIC", subtitle: "투하자본이익률 (세후영업이익/투하자본)", unit: "%" },
  { key: "OperatingMargin", title: "영업이익률", subtitle: "영업이익/매출액", unit: "%" },
  { key: "DebtEquity", title: "부채비율 (D/E)", subtitle: "부채/자기자본", unit: "%" },
  { key: "MagicFormula", title: "그린블랏 마법공식", subtitle: "ROIC + 이익수익률(EBIT/EV)", unit: "%" },
  { key: "GrahamNumber", title: "그레이엄 넘버", subtitle: "sqrt(22.5 × EPS × BPS)", unit: "" },
];

const form = document.getElementById("ticker-form");
const input = document.getElementById("ticker-input");
const companyNameEl = document.getElementById("company-name");
const statusEl = document.getElementById("status");
const chartsEl = document.getElementById("charts");
const tableSection = document.getElementById("table-section");
const tableEl = document.getElementById("data-table");

let tooltipEl = document.querySelector(".tooltip");
if (!tooltipEl) {
  tooltipEl = document.createElement("div");
  tooltipEl.className = "tooltip";
  document.body.appendChild(tooltipEl);
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const ticker = input.value.trim();
  if (ticker) {
    loadTicker(ticker);
    loadFinancials(ticker);
  }
});

async function loadTicker(ticker) {
  setStatus(`"${ticker}" 데이터를 불러오는 중...`, false);
  companyNameEl.hidden = true;
  chartsEl.hidden = true;
  tableSection.hidden = true;

  try {
    const res = await fetch(`/api/ratios/${encodeURIComponent(ticker)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "데이터를 불러오지 못했습니다.");

    companyNameEl.textContent = data.display || data.ticker;
    companyNameEl.hidden = false;

    setStatus(describeCacheInfo(data.cache_info), false);
    renderCharts(data);
    renderTable(data);
    chartsEl.hidden = false;
    tableSection.hidden = false;
  } catch (err) {
    setStatus(err.message, true);
  }
}

// ---------- 재무제표 표 (연도별 / 최근 5개 분기) ----------

const finSection = document.getElementById("fin-section");
const finStatusEl = document.getElementById("fin-status");
let finRequestId = 0;

async function loadFinancials(ticker) {
  const reqId = ++finRequestId; // 빠르게 연속 조회할 때 늦게 온 이전 응답이 덮어쓰지 않도록
  finSection.hidden = true;
  setFinStatus("재무제표를 불러오는 중...", false);
  try {
    const res = await fetch(`/api/financials/${encodeURIComponent(ticker)}`);
    const data = await res.json();
    if (reqId !== finRequestId) return;
    if (!res.ok) throw new Error(data.error || "재무제표를 불러오지 못했습니다.");

    const unit = amountUnit(data.currency);
    document.getElementById("fin-unit-annual").textContent = `(단위: ${unit.label})`;
    document.getElementById("fin-unit-quarterly").textContent = `(단위: ${unit.label})`;
    renderFinTable(document.getElementById("fin-annual"), data.metrics, data.annual, unit);
    renderFinTable(document.getElementById("fin-quarterly"), data.metrics, data.quarterly, unit);
    setFinStatus("", false);
    finSection.hidden = false;
  } catch (err) {
    if (reqId !== finRequestId) return;
    setFinStatus(err.message, true);
  }
}

function setFinStatus(message, isError) {
  finStatusEl.hidden = !message;
  finStatusEl.textContent = message;
  finStatusEl.className = "status" + (isError ? " error" : "");
}

// 원화는 억원, 나머지 통화는 백만 단위로 표시
function amountUnit(currency) {
  if (currency === "KRW") return { div: 1e8, label: "억원", digits: 0 };
  if (currency === "JPY") return { div: 1e8, label: "억엔", digits: 0 };
  return { div: 1e6, label: `${currency || ""} 백만`.trim(), digits: 0 };
}

const numFmt = (digits) =>
  new Intl.NumberFormat("ko-KR", { minimumFractionDigits: digits, maximumFractionDigits: digits });

function formatFinCell(v, kind, unit) {
  if (v === null || v === undefined) return "-";
  if (kind === "amount") return numFmt(unit.digits).format(v / unit.div);
  if (kind === "pct") return numFmt(1).format(v) + "%";
  return numFmt(1).format(v) + "배";
}

function renderFinTable(table, metrics, tbl, unit) {
  if (!tbl.periods.length) {
    table.innerHTML = `<tbody><tr><td>표시할 데이터가 없습니다</td></tr></tbody>`;
    return;
  }
  const thead = `<thead><tr><th>항목</th>${tbl.periods.map((p) => `<th>${p}</th>`).join("")}</tr></thead>`;
  const body = metrics
    .map((m) => {
      const cells = tbl.rows[m.key].map((v) => {
        const neg = v !== null && v !== undefined && v < 0 ? ' class="neg"' : "";
        return `<td${neg}>${formatFinCell(v, m.kind, unit)}</td>`;
      });
      const groupStart = m.key === "op_margin" ? ' class="group-start"' : "";
      return `<tr${groupStart}><th scope="row">${m.label}</th>${cells.join("")}</tr>`;
    })
    .join("");
  table.innerHTML = thead + `<tbody>${body}</tbody>`;
}

const CACHE_LABEL = {
  cache: "DB 캐시",
  fetched: "Yahoo Finance에서 새로 조회",
  "cache(stale)": "DB 캐시(오래됨, Yahoo Finance 조회 실패로 폴백)",
};

function describeCacheInfo(cacheInfo) {
  if (!cacheInfo) return "";
  return `연간 재무제표: ${CACHE_LABEL[cacheInfo.annual] || cacheInfo.annual} · 최근 분기(TTM): ${CACHE_LABEL[cacheInfo.ttm] || cacheInfo.ttm}`;
}

function setStatus(message, isError) {
  statusEl.hidden = !message;
  statusEl.textContent = message;
  statusEl.className = "status" + (isError ? " error" : "");
}

function toQuarterLabel(dateStr) {
  const [y, m] = dateStr.split("-").map(Number);
  const q = Math.ceil(m / 3);
  return `'${String(y).slice(2)} Q${q}`;
}

function renderCharts(data) {
  chartsEl.innerHTML = "";
  const labels = data.dates.map(toQuarterLabel);

  const label = data.display || data.ticker;

  METRIC_DEFS.forEach((def) => {
    const card = document.createElement("div");
    card.className = "chart-card";

    const h3 = document.createElement("h3");
    h3.textContent = `${label} · ${def.title}`;
    const subtitle = document.createElement("p");
    subtitle.className = "subtitle";
    subtitle.textContent = def.subtitle;
    card.appendChild(h3);
    card.appendChild(subtitle);

    const values = data.metrics[def.key];
    const hasData = values.some((v) => v !== null && v !== undefined);

    if (!hasData) {
      const empty = document.createElement("div");
      empty.className = "chart-empty";
      empty.textContent = "표시할 데이터가 없습니다";
      card.appendChild(empty);
    } else {
      card.appendChild(buildLineChart(labels, values, def.unit));
    }

    chartsEl.appendChild(card);
  });
}

function buildLineChart(labels, values, unit) {
  const width = 320;
  const height = 180;
  const padL = 36;
  const padR = 12;
  const padT = 12;
  const padB = 24;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;

  const valid = values.filter((v) => v !== null && v !== undefined);
  let min = Math.min(...valid);
  let max = Math.max(...valid);
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const pad = (max - min) * 0.15;
  min -= pad;
  max += pad;

  const xFor = (i) =>
    labels.length === 1 ? padL + innerW / 2 : padL + (i / (labels.length - 1)) * innerW;
  const yFor = (v) => padT + innerH - ((v - min) / (max - min)) * innerH;

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "chart-svg");
  svg.setAttribute("preserveAspectRatio", "none");

  // gridlines + y labels (min/mid/max)
  [min + (max - min), min + (max - min) / 2, min].forEach((v) => {
    const y = yFor(v);
    const line = document.createElementNS(svg.namespaceURI, "line");
    line.setAttribute("x1", padL);
    line.setAttribute("x2", width - padR);
    line.setAttribute("y1", y);
    line.setAttribute("y2", y);
    line.setAttribute("class", "grid-line");
    svg.appendChild(line);

    const label = document.createElementNS(svg.namespaceURI, "text");
    label.setAttribute("x", padL - 6);
    label.setAttribute("y", y + 3);
    label.setAttribute("text-anchor", "end");
    label.setAttribute("class", "axis-label");
    label.textContent = formatValue(v, unit);
    svg.appendChild(label);
  });

  // x labels
  labels.forEach((label, i) => {
    const text = document.createElementNS(svg.namespaceURI, "text");
    text.setAttribute("x", xFor(i));
    text.setAttribute("y", height - 6);
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("class", "axis-label");
    text.textContent = label;
    svg.appendChild(text);
  });

  // line path (skip gaps where value is null)
  let d = "";
  values.forEach((v, i) => {
    if (v === null || v === undefined) return;
    d += (d ? "L" : "M") + xFor(i) + " " + yFor(v) + " ";
  });
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d", d.trim());
  path.setAttribute("class", "value-line");
  svg.appendChild(path);

  // dots + hover targets
  values.forEach((v, i) => {
    if (v === null || v === undefined) return;
    const cx = xFor(i);
    const cy = yFor(v);

    const dot = document.createElementNS(svg.namespaceURI, "circle");
    dot.setAttribute("cx", cx);
    dot.setAttribute("cy", cy);
    dot.setAttribute("r", 4);
    dot.setAttribute("class", "value-dot");
    svg.appendChild(dot);

    const hit = document.createElementNS(svg.namespaceURI, "circle");
    hit.setAttribute("cx", cx);
    hit.setAttribute("cy", cy);
    hit.setAttribute("r", 10);
    hit.setAttribute("class", "hit-area");
    hit.addEventListener("mouseenter", (evt) => showTooltip(evt, labels[i], v, unit));
    hit.addEventListener("mousemove", (evt) => positionTooltip(evt));
    hit.addEventListener("mouseleave", hideTooltip);
    svg.appendChild(hit);
  });

  return svg;
}

function formatValue(v, unit) {
  if (v === null || v === undefined) return "-";
  const num = unit === "%" ? v : v;
  return num.toFixed(1) + unit;
}

function showTooltip(evt, label, value, unit) {
  tooltipEl.textContent = `${label}: ${formatValue(value, unit)}`;
  tooltipEl.style.visibility = "visible";
  positionTooltip(evt);
}

function positionTooltip(evt) {
  tooltipEl.style.left = evt.clientX + "px";
  tooltipEl.style.top = evt.clientY + "px";
}

function hideTooltip() {
  tooltipEl.style.visibility = "hidden";
}

function renderTable(data) {
  const headRow = ["날짜", ...METRIC_DEFS.map((d) => d.title)];
  const thead = `<thead><tr>${headRow.map((h) => `<th>${h}</th>`).join("")}</tr></thead>`;

  const bodyRows = data.dates
    .map((date, i) => {
      const cells = METRIC_DEFS.map((def) => {
        const v = data.metrics[def.key][i];
        return `<td>${v === null || v === undefined ? "-" : formatValue(v, def.unit)}</td>`;
      });
      return `<tr><td>${date}</td>${cells.join("")}</tr>`;
    })
    .join("");

  tableEl.innerHTML = thead + `<tbody>${bodyRows}</tbody>`;
}

// ---------- 탭 전환 ----------

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => {
      const active = b === btn;
      b.classList.toggle("is-active", active);
      b.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".panel").forEach((panel) => {
      const active = panel.id === `panel-${btn.dataset.panel}`;
      panel.classList.toggle("is-active", active);
      panel.hidden = !active;
    });
  });
});
