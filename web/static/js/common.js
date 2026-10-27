/* 共用工具函数 */
async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (res.status === 401) { window.location.href = '/login'; throw new Error('Unauthorized'); }
  if (!res.ok) {
    let detail = '';
    try {
      const payload = await res.json();
      detail = payload.error || payload.message || '';
    } catch (e) {}
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[ch]);
}

function fmtPct(v, digits) {
  if (v === null || v === undefined) return 'N/A';
  return (v * 100).toFixed(digits === undefined ? 2 : digits) + '%';
}

function fmtTime(ms) {
  if (!ms) return 'N/A';
  const d = new Date(Number(ms));
  return d.toLocaleString('zh-CN', { hour12: false });
}

function pnlClass(v) {
  if (v > 0) return 'text-green';
  if (v < 0) return 'text-red';
  return '';
}

function levelBadge(level) {
  const map = {
    warning: ['badge-yellow', '⚠️ 预警'],
    critical: ['badge-red', '🔴 严重'],
    emergency: ['badge-red', '🚨 紧急'],
  };
  const [cls, label] = map[level] || ['badge-gray', level];
  return `<span class="badge ${cls}">${escapeHtml(label)}</span>`;
}

function typeLabel(t) {
  const map = {
    funding_extreme: '资金费率异常',
    cross_spread: '跨所费率差',
    basis_extreme: '基差异常',
    stablecoin_depeg: '锚定资产偏离',
    issuer_spread: '跨产品 / 跨发行商价差',
    liquidation_surge: '爆仓量激增',
  };
  return escapeHtml(map[t] || t);
}

function exchangeBadge(ex) {
  const colors = { binance: 'badge-yellow', okx: 'badge-gray', bybit: 'badge-blue', gate: 'badge-green', bitget: 'badge-blue' };
  const name = String(ex ?? '');
  return `<span class="badge ${colors[name] || 'badge-gray'}">${escapeHtml(name)}</span>`;
}


function fmtUsd(v) {
  if (v === null || v === undefined) return 'N/A';
  const n = Number(v);
  if (!isFinite(n)) return 'N/A';
  if (n >= 1e9) return '$' + (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return '$' + (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return '$' + (n / 1e3).toFixed(1) + 'K';
  return '$' + n.toFixed(0);
}
