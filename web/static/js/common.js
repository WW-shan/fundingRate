/* 共用工具函数 */
async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (res.status === 401) { window.location.href = '/login'; throw new Error('Unauthorized'); }
  return res.json();
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
  return `<span class="badge ${cls}">${label}</span>`;
}

function typeLabel(t) {
  const map = {
    funding_extreme: '资金费率异常',
    cross_spread: '跨所费率差',
    basis_extreme: '基差异常',
    stablecoin_depeg: '锚定资产偏离',
    issuer_spread: '跨发行商价差',
  };
  return map[t] || t;
}

function exchangeBadge(ex) {
  const colors = { binance: 'badge-yellow', okx: 'badge-gray', bybit: 'badge-blue', gate: 'badge-green', bitget: 'badge-blue' };
  return `<span class="badge ${colors[ex] || 'badge-gray'}">${ex}</span>`;
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
