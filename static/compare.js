// 탭 2: N개 종목의 기준일 대비 상대 수익률 비교
// - 주가의 절대 크기는 쓰지 않는다. from(기준일) 시점을 0%로 맞춘 상대 수익률만 그린다.
// - 색상은 style.css의 --series-1..8 (검증된 카테고리 팔레트) 슬롯을 순서대로 고정 배정한다.

const MAX_SERIES = 8;

const compareForm = document.getElementById("compare-form");
const tickersInput = document.getElementById("compare-tickers");
const fromInput = document.getElementById("compare-from");
const toInput = document.getElementById("compare-to");
const quickRanges = document.getElementById("quick-ranges");
const compareStatus = document.getElementById("compare-status");
const compareResult = document.getElementById("compare-result");
const compareChart = document.getElementById("compare-chart");
const compareLegend = document.getElementById("compare-legend");
const compareBasis = document.getElementById("compare-basis");
const compareSummary = document.getElementById("compare-summary");

const SVG_NS = "http://www.w3.org/2000/svg";

let compareTooltip = document.createElement("div");
compareTooltip.className = "tooltip tooltip-multi";
document.body.appendChild(compareTooltip);

// ---------- 기간 입력 ----------

function toISO(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function setRange(months) {
  const end = new Date();
  const start = new Date();
  start.setMonth(start.getMonth() - months);
  fromInput.value = toISO(start);
  toInput.value = toISO(end);
}

setRange(12); // 기본 1년

quickRanges.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-months]");
  if (!btn) return;
  setRange(Number(btn.dataset.months));
  if (tickersInput.value.trim()) compareForm.requestSubmit();
});

compareForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const list = tickersInput.value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  if (!list.length) return setCompareStatus("비교할 종목을 하나 이상 입력하세요.", true);
  if (list.length > MAX_SERIES) return setCompareStatus(`종목은 최대 ${MAX_SERIES}개까지 비교할 수 있습니다.`, true);
  if (!fromInput.value || !toInput.value) return setCompareStatus("시작일과 종료일을 모두 지정하세요.", true);
  if (fromInput.value >= toInput.value) return setCompareStatus("시작일은 종료일보다 앞서야 합니다.", true);

  loadCompare(list, fromInput.value, toInput.value);
});

function setCompareStatus(message, isError) {
  compareStatus.hidden = !message;
  compareStatus.textContent = message;
  compareStatus.className = "status" + (isError ? " error" : "");
}

async function loadCompare(list, from, to) {
  setCompareStatus(`${list.length}개 종목의 주가를 불러오는 중...`, false);
  compareResult.hidden = true;

  const qs = new URLSearchParams({ tickers: list.join(","), from, to });
  try {
    const res = await fetch(`/api/compare?${qs}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "주가를 불러오지 못했습니다.");

    renderCompare(data);
    compareResult.hidden = false;

    const notes = [`${data.dates.length}개 거래일 · ${data.series.length}개 종목`];
    if (data.warnings && data.warnings.length) notes.push(data.warnings.join(" "));
    setCompareStatus(notes.join(" · "), false);
  } catch (err) {
    setCompareStatus(err.message, true);
  }
}

// ---------- 렌더링 ----------

function seriesColor(i) {
  return `var(--series-${(i % MAX_SERIES) + 1})`;
}

function fmtPct(v, digits = 1) {
  if (v === null || v === undefined) return "-";
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

// 통화가 제각각이라(원/달러) 자릿수만 보기 좋게 맞춘다. 비교 자체는 %로만 한다.
function fmtPrice(v) {
  if (v === null || v === undefined) return "-";
  const digits = Math.abs(v) >= 1000 ? 0 : 2;
  return v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function renderCompare(data) {
  compareBasis.textContent =
    `${data.start} ~ ${data.end} · 기준일을 0%로 맞춘 상대 비교 · ${data.basis}`;

  compareChart.innerHTML = "";
  compareChart.appendChild(buildCompareChart(data));
  renderLegend(data);
  renderSummary(data);
}

function renderLegend(data) {
  compareLegend.innerHTML = "";
  data.series.forEach((s, i) => {
    const item = document.createElement("span");
    item.className = "legend-item";

    const swatch = document.createElement("span");
    swatch.className = "legend-swatch";
    swatch.style.background = seriesColor(i);

    const name = document.createElement("span");
    name.className = "legend-label";
    name.textContent = s.display;

    const value = document.createElement("span");
    value.className = "legend-value";
    value.textContent = fmtPct(s.total_return);

    item.append(swatch, name, value);
    compareLegend.appendChild(item);
  });
}

function renderSummary(data) {
  const head = ["종목", "기준일", "기준가", "총수익률", "연환산(CAGR)", "최대낙폭(MDD)"];
  const thead = `<thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead>`;

  const rows = data.series
    .map((s, i) => {
      const swatch = `<span class="legend-swatch" style="background:${seriesColor(i)}"></span>`;
      return `<tr>
        <td>${swatch}${escapeHtml(s.display)}</td>
        <td>${s.base_date}</td>
        <td>${fmtPrice(s.base_price)}</td>
        <td class="${s.total_return >= 0 ? "pos" : "neg"}">${fmtPct(s.total_return, 2)}</td>
        <td>${s.cagr === null ? "-" : fmtPct(s.cagr, 2)}</td>
        <td class="neg">${s.mdd === null ? "-" : fmtPct(s.mdd, 2)}</td>
      </tr>`;
    })
    .join("");

  compareSummary.innerHTML = thead + `<tbody>${rows}</tbody>`;
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

// y축 눈금: 범위를 1/2/5 × 10^k 단위로 끊는다.
function niceTicks(min, max, count) {
  const raw = (max - min) / Math.max(count, 1);
  const mag = Math.pow(10, Math.floor(Math.log10(Math.abs(raw) || 1)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const ticks = [];
  for (let t = Math.ceil(min / step) * step; t <= max + step * 1e-9; t += step) {
    ticks.push(Number(t.toFixed(10)));
  }
  return ticks;
}

function formatDateLabel(iso, spanDays) {
  const [y, m, d] = iso.split("-");
  if (spanDays > 730) return `${y.slice(2)}.${m}`;
  if (spanDays > 120) return `${y.slice(2)}.${m}`;
  return `${m}.${d}`;
}

function buildCompareChart(data) {
  const W = 960;
  const H = 440;
  const padL = 58;
  const padR = 16;
  const padT = 16;
  const padB = 34;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;

  const dates = data.dates;
  const spanDays = (new Date(dates[dates.length - 1]) - new Date(dates[0])) / 86400000;

  const all = data.series.flatMap((s) => s.returns).filter((v) => v !== null);
  let min = Math.min(0, ...all);
  let max = Math.max(0, ...all);
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const pad = (max - min) * 0.08;
  min -= pad;
  max += pad;

  const xFor = (i) => (dates.length === 1 ? padL + innerW / 2 : padL + (i / (dates.length - 1)) * innerW);
  const yFor = (v) => padT + innerH - ((v - min) / (max - min)) * innerH;

  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "compare-svg");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "종목별 기준일 대비 상대 수익률 추이");

  const el = (tag, attrs) => {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
  };

  // y 그리드 + 라벨 (0% 선은 강조)
  niceTicks(min, max, 6).forEach((t) => {
    const y = yFor(t);
    svg.appendChild(
      el("line", {
        x1: padL, x2: W - padR, y1: y, y2: y,
        class: t === 0 ? "zero-line" : "grid-line",
      })
    );
    svg.appendChild(
      Object.assign(el("text", { x: padL - 8, y: y + 4, "text-anchor": "end", class: "axis-label" }), {
        textContent: `${t > 0 ? "+" : ""}${t}%`,
      })
    );
  });

  // x 라벨 (최대 7개)
  const xTickCount = Math.min(7, dates.length);
  for (let k = 0; k < xTickCount; k++) {
    const i = xTickCount === 1 ? 0 : Math.round((k / (xTickCount - 1)) * (dates.length - 1));
    svg.appendChild(
      Object.assign(
        el("text", { x: xFor(i), y: H - 10, "text-anchor": "middle", class: "axis-label" }),
        { textContent: formatDateLabel(dates[i], spanDays) }
      )
    );
  }

  // 종목별 라인
  data.series.forEach((s, si) => {
    let d = "";
    let gap = true; // 데이터가 없는 구간에서는 선을 잇지 않고 끊는다
    s.returns.forEach((v, i) => {
      if (v === null) {
        gap = true;
        return;
      }
      d += (gap ? "M" : "L") + xFor(i).toFixed(2) + " " + yFor(v).toFixed(2) + " ";
      gap = false;
    });
    if (!d) return;
    svg.appendChild(el("path", { d: d.trim(), class: "series-line", stroke: seriesColor(si) }));
  });

  // 종목이 4개 이하면 라인 끝에 직접 라벨(색 점 + 텍스트)을 붙인다.
  if (data.series.length <= 4) {
    const ends = data.series
      .map((s, si) => {
        const idx = s.returns.reduce((acc, v, i) => (v === null ? acc : i), -1);
        return idx < 0 ? null : { si, idx, value: s.returns[idx] };
      })
      .filter(Boolean)
      // 값이 큰 순서(= 화면 위쪽 순서)로 위에서부터 배치해야 라벨 순서가 선 순서와 어긋나지 않는다.
      .sort((a, b) => b.value - a.value);

    let prevY = -Infinity;
    ends.forEach((e) => {
      const cx = xFor(e.idx);
      const cy = yFor(e.value);
      const y = Math.max(cy, prevY + 14);
      prevY = y;

      svg.appendChild(el("circle", { cx, cy, r: 4, fill: seriesColor(e.si), class: "series-end-dot" }));
      svg.appendChild(
        Object.assign(
          el("text", { x: cx - 8, y: y + 4, "text-anchor": "end", class: "series-end-label" }),
          { textContent: fmtPct(e.value) }
        )
      );
    });
  }

  // hover: 세로 크로스헤어 + 해당 날짜의 전 종목 값 툴팁
  const crosshair = el("line", { x1: 0, x2: 0, y1: padT, y2: padT + innerH, class: "crosshair", visibility: "hidden" });
  svg.appendChild(crosshair);

  const dots = data.series.map((_, si) =>
    el("circle", { r: 5, class: "crosshair-dot", fill: seriesColor(si), visibility: "hidden" })
  );
  dots.forEach((dot) => svg.appendChild(dot));

  const overlay = el("rect", { x: padL, y: padT, width: innerW, height: innerH, class: "hover-overlay" });
  svg.appendChild(overlay);

  overlay.addEventListener("mousemove", (evt) => {
    const rect = svg.getBoundingClientRect();
    const vx = ((evt.clientX - rect.left) / rect.width) * W;
    const ratio = (vx - padL) / innerW;
    const i = Math.max(0, Math.min(dates.length - 1, Math.round(ratio * (dates.length - 1))));

    crosshair.setAttribute("x1", xFor(i));
    crosshair.setAttribute("x2", xFor(i));
    crosshair.setAttribute("visibility", "visible");

    const rows = [];
    data.series.forEach((s, si) => {
      const v = s.returns[i];
      const dot = dots[si];
      if (v === null) {
        dot.setAttribute("visibility", "hidden");
        return;
      }
      dot.setAttribute("cx", xFor(i));
      dot.setAttribute("cy", yFor(v));
      dot.setAttribute("visibility", "visible");
      rows.push({ name: s.display, value: v, price: s.prices[i], color: seriesColor(si) });
    });

    rows.sort((a, b) => b.value - a.value);
    compareTooltip.innerHTML =
      `<div class="tt-date">${dates[i]}</div>` +
      rows
        .map(
          (r) =>
            `<div class="tt-row"><span class="legend-swatch" style="background:${r.color}"></span>` +
            `<span class="tt-name">${escapeHtml(r.name)}</span>` +
            `<span class="tt-value">${fmtPct(r.value, 2)}</span>` +
            `<span class="tt-price">${r.price === null ? "" : fmtPrice(r.price)}</span></div>`
        )
        .join("");
    compareTooltip.style.visibility = "visible";
    compareTooltip.style.left = evt.clientX + "px";
    compareTooltip.style.top = evt.clientY + "px";
  });

  overlay.addEventListener("mouseleave", () => {
    crosshair.setAttribute("visibility", "hidden");
    dots.forEach((d) => d.setAttribute("visibility", "hidden"));
    compareTooltip.style.visibility = "hidden";
  });

  return svg;
}
