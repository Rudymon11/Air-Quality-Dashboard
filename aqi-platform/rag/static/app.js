// State
let currentTable = 'daily';
let currentOffset = 0;
let currentTotal = 0;
let currentLimit = 100;

// PM2.5 thresholds for colour coding (µg/m³, Indian NAAQS)
const THRESHOLDS = {
  avg_pm25:  [60, 90],
  avg_pm10:  [100, 150],
  avg_so2:   [80, 180],
  avg_no2:   [80, 180],
  avg_co:    [4000, 10000],
  avg_o3:    [100, 168],
  avg_nh3:   [100, 200],
  avg_no:    [40, 80],
  pollutant_value: [60, 120],
};

const COL_LABELS = {
  city: 'City', reading_date: 'Date', source: 'Source',
  avg_pm25: 'PM2.5 (µg/m³)', avg_pm10: 'PM10 (µg/m³)', avg_so2: 'SO2 (µg/m³)',
  avg_no2: 'NO2 (µg/m³)', avg_no: 'NO (µg/m³)', avg_co: 'CO (µg/m³)',
  avg_nh3: 'NH3 (µg/m³)', avg_o3: 'O3 (µg/m³)',
  active_stations: 'Stations',
  station: 'Station', pollutant: 'Pollutant',
  pollutant_value: 'Value', unit: 'Unit',
  reading_time_utc: 'Timestamp', ingested_at: 'Ingested At',
};

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  loadCities();
  loadTable(0);
});

function loadCities() {
  fetch('/api/cities')
    .then(r => r.json())
    .then(cities => {
      const sel = document.getElementById('filter-city');
      cities.forEach(c => {
        const o = document.createElement('option');
        o.value = c; o.textContent = c;
        sel.appendChild(o);
      });
    });
}

// ---------------------------------------------------------------------------
// Page switching
// ---------------------------------------------------------------------------
function showPage(page) {
  document.getElementById('page-explorer').style.display = page === 'explorer' ? '' : 'none';
  document.getElementById('page-qa').style.display       = page === 'qa'       ? '' : 'none';
  document.querySelectorAll('.nav-btn').forEach((b, i) => {
    b.classList.toggle('active', (i === 0 && page === 'explorer') || (i === 1 && page === 'qa'));
  });
}

// ---------------------------------------------------------------------------
// Table switching
// ---------------------------------------------------------------------------
function switchTable(name) {
  currentTable = name;
  currentOffset = 0;

  document.querySelectorAll('#explorerTabs .nav-link').forEach((b, i) => {
    b.classList.toggle('active', (i === 0 && name === 'daily') || (i === 1 && name === 'readings'));
  });

  // show/hide filters
  document.getElementById('filter-date-from-wrap').style.display  = name === 'daily' ? '' : 'none';
  document.getElementById('filter-date-to-wrap').style.display    = name === 'daily' ? '' : 'none';
  document.getElementById('filter-station-wrap').style.display    = name === 'readings' ? '' : 'none';

  // pollutant options differ per table
  const sel = document.getElementById('filter-pollutant');
  sel.innerHTML = '<option value="">All</option>';
  const opts = name === 'daily'
    ? ['PM2.5','PM10','SO2','NO2','NO','CO','NH3','O3']
    : ['PM2.5','PM10','SO2','NO2','NO','NOX','CO','NH3','O3','TEMP','HUMIDITY','WIND_SPEED','WIND_DIR'];
  opts.forEach(p => { const o = document.createElement('option'); o.value = p; o.textContent = p; sel.appendChild(o); });

  loadTable(0);
}

// ---------------------------------------------------------------------------
// Load table data
// ---------------------------------------------------------------------------
function loadTable(offset) {
  currentOffset = offset;
  currentLimit  = parseInt(document.getElementById('filter-limit').value);

  const city      = document.getElementById('filter-city').value;
  const pollutant = document.getElementById('filter-pollutant').value;
  const source    = document.getElementById('filter-source').value;
  const limit     = currentLimit;

  let url;
  if (currentTable === 'daily') {
    const from = document.getElementById('filter-date-from').value;
    const to   = document.getElementById('filter-date-to').value;
    url = `/api/daily?city=${encodeURIComponent(city)}&pollutant=${encodeURIComponent(pollutant)}&source=${encodeURIComponent(source)}&date_from=${from}&date_to=${to}&limit=${limit}&offset=${offset}`;
  } else {
    const station = document.getElementById('filter-station').value;
    url = `/api/readings?city=${encodeURIComponent(city)}&pollutant=${encodeURIComponent(pollutant)}&source=${encodeURIComponent(source)}&station=${encodeURIComponent(station)}&limit=${limit}&offset=${offset}`;
  }

  document.getElementById('table-loading').style.display    = '';
  document.getElementById('table-container').style.display  = 'none';

  fetch(url)
    .then(r => r.json())
    .then(data => {
      currentTotal = data.total;
      renderTable(data.columns, data.rows);
      updatePagination(data.total, data.offset, data.limit);
      document.getElementById('table-loading').style.display   = 'none';
      document.getElementById('table-container').style.display = '';
    });
}

function renderTable(columns, rows) {
  const head = document.getElementById('table-head');
  const body = document.getElementById('table-body');

  head.innerHTML = '<tr>' + columns.map(c =>
    `<th>${COL_LABELS[c] || c}</th>`
  ).join('') + '</tr>';

  body.innerHTML = rows.map(row =>
    '<tr>' + columns.map(col => {
      const val = row[col];
      if (val === null || val === undefined) return `<td class="val-null">—</td>`;
      if (THRESHOLDS[col]) {
        const [warn, bad] = THRESHOLDS[col];
        const n = parseFloat(val);
        const cls = n >= bad ? 'val-bad' : n >= warn ? 'val-warn' : 'val-good';
        return `<td class="${cls}">${formatVal(col, val)}</td>`;
      }
      return `<td>${formatVal(col, val)}</td>`;
    }).join('') + '</tr>'
  ).join('');
}

function formatVal(col, val) {
  if (col === 'reading_date') return val;
  if (col === 'reading_time_utc' || col === 'ingested_at')
    return val.toString().replace('T', ' ').substring(0, 16);
  if (typeof val === 'number' || (typeof val === 'string' && !isNaN(val) && val !== ''))
    return parseFloat(val) % 1 === 0 ? parseInt(val) : parseFloat(val).toFixed(2);
  return val;
}

function updatePagination(total, offset, limit) {
  const page    = Math.floor(offset / limit) + 1;
  const pages   = Math.ceil(total / limit);
  const showing = Math.min(offset + limit, total);

  document.getElementById('total-count').textContent   = total.toLocaleString();
  document.getElementById('showing-count').textContent = `${offset + 1}–${showing}`;
  document.getElementById('page-info').textContent     = `Page ${page} of ${pages}`;
  document.getElementById('btn-prev').disabled = offset === 0;
  document.getElementById('btn-next').disabled = offset + limit >= total;
}

function changePage(dir) {
  const newOffset = currentOffset + dir * currentLimit;
  if (newOffset < 0 || newOffset >= currentTotal) return;
  loadTable(newOffset);
}

// ---------------------------------------------------------------------------
// Q&A
// ---------------------------------------------------------------------------
let conversationHistory = [];
let priorFilters = {};
const TOKEN_LIMIT = 131072;
const TOKEN_WARN  = 100000;
let totalTokensUsed = 0;

function setQuery(el) {
  document.getElementById('qa-input').value = el.textContent;
}

function clearConversation() {
  conversationHistory = [];
  priorFilters = {};
  totalTokensUsed = 0;
  document.getElementById('qa-thread').innerHTML = '';
  document.getElementById('qa-result').style.display = 'none';
  document.getElementById('qa-input').value = '';
  document.getElementById('token-warning').style.display = 'none';
}

function askQuestion() {
  const query = document.getElementById('qa-input').value.trim();
  if (!query) return;

  // Append user bubble to thread
  const thread = document.getElementById('qa-thread');
  thread.insertAdjacentHTML('beforeend', `
    <div class="chat-bubble user-bubble mb-3">${query}</div>
  `);
  document.getElementById('qa-input').value = '';
  document.getElementById('qa-loading').style.display = '';

  fetch('/ask', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ query, history: conversationHistory, prior_filters: priorFilters }),
  })
    .then(r => {
      if (!r.ok) return r.text().then(t => { throw new Error(r.status + ' ' + t); });
      return r.json();
    })
    .then(data => {
      // Update history
      conversationHistory.push({ role: 'user',      content: query });
      conversationHistory.push({ role: 'assistant', content: data.answer });
      priorFilters = data.filters_applied || {};

      // Track tokens and warn if approaching limit
      if (data.tokens_used) {
        totalTokensUsed = data.tokens_used;  // Groq returns cumulative context size
        if (totalTokensUsed >= TOKEN_WARN) {
          const pct = Math.round((totalTokensUsed / TOKEN_LIMIT) * 100);
          const warn = document.getElementById('token-warning');
          warn.textContent = `Context window ${pct}% full (${totalTokensUsed.toLocaleString()} / ${TOKEN_LIMIT.toLocaleString()} tokens). Start a new chat soon to avoid cutoff.`;
          warn.style.display = '';
        }
      }

      // Append assistant bubble
      const routeTag = data.route === 'needs_exact_lookup' ? 'exact lookup'
                     : data.route === 'both'               ? 'exact + trend'
                     : 'trend context';
      thread.insertAdjacentHTML('beforeend', `
        <div class="chat-bubble assistant-bubble mb-3">
          <span class="route-tag">${routeTag}</span>${data.answer}
        </div>
      `);
      thread.scrollTop = thread.scrollHeight;

      renderSources(data.sources);
      renderAudit(data.rows_used, data.filters_applied);
      renderRetrievedTable(data.sources, data.sql_rows, data.route);
      document.getElementById('qa-loading').style.display = 'none';
      document.getElementById('qa-result').style.display  = '';
    })
    .catch(err => {
      document.getElementById('qa-loading').style.display = 'none';
      alert('Error: ' + err);
    });
}

function renderSources(sources) {
  const container = document.getElementById('sources-container');
  container.innerHTML = sources.map(s => `
    <div class="source-badge">
      <span class="city-name">${s.city}</span>
      <span class="week-range ms-2">${s.week_start} → ${s.week_end}</span>
    </div>
  `).join('');
}

function renderAudit(rowsUsed, filters) {
  const el = document.getElementById('qa-audit');
  if (!el) return;
  const filterText = filters && Object.entries(filters)
    .filter(([, value]) => value && (!Array.isArray(value) || value.length > 0))
    .map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(', ') : value}`)
    .join(' · ');
  el.textContent = rowsUsed
    ? `Underlying sensor rows used: ${Number(rowsUsed).toLocaleString()}${filterText ? ` · ${filterText}` : ''}`
    : (filterText ? `Filters: ${filterText}` : 'No exact sensor rows used.');
}

function renderRetrievedTable(sources, sqlRows, route) {
  const thead = document.getElementById('retrieved-thead');
  const tbody = document.getElementById('retrieved-tbody');

  // for exact lookup or both: show sql rows if available
  if (sqlRows && sqlRows.length > 0) {
    thead.innerHTML = `<tr><th>Exact Readings Used</th></tr>`;
    tbody.innerHTML = sqlRows.map(r =>
      `<tr><td class="text-muted" style="white-space:normal;font-size:0.78rem">${r}</td></tr>`
    ).join('');
    return;
  }

  // fallback: show vector summaries
  if (!sources || sources.length === 0) return;
  thead.innerHTML = `<tr><th>City</th><th>Week Start</th><th>Week End</th><th>Summary</th></tr>`;
  tbody.innerHTML = sources.map(s => `
    <tr>
      <td><strong>${s.city}</strong></td>
      <td>${s.week_start}</td>
      <td>${s.week_end}</td>
      <td class="text-muted" style="white-space:normal;max-width:500px;font-size:0.78rem">${s.summary}</td>
    </tr>
  `).join('');
}
