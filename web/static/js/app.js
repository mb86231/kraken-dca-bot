/**
 * Common utilities for the DCA-Bot dashboard.
 */

function csrfToken() {
  // Prefer the cookie so the token always matches the current cookie value,
  // even if the page meta tag is stale after a reload or multi-tab use.
  const match = document.cookie.match(/(?:^|; )dca_csrf=([^;]*)/);
  if (match) return decodeURIComponent(match[1]);
  return document.querySelector('meta[name="csrf-token"]')?.content || '';
}

async function apiRequest(method, url, body = {}) {
  const headers = {
    'Content-Type': 'application/json',
    'X-CSRF-Token': csrfToken(),
  };
  const options = {
    method,
    headers,
  };
  if (method !== 'GET') {
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  if (response.status === 401 || response.status === 307) {
    window.location.href = '/login';
    return null;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `Request failed: ${response.status}`);
  }
  return data;
}

async function apiPost(url, body = {}) {
  return apiRequest('POST', url, body);
}

async function apiPut(url, body = {}) {
  return apiRequest('PUT', url, body);
}

async function apiDelete(url, body = {}) {
  return apiRequest('DELETE', url, body);
}

async function apiGet(url) {
  const response = await fetch(url, {
    headers: { 'X-CSRF-Token': csrfToken() },
  });
  if (response.status === 401 || response.status === 307) {
    window.location.href = '/login';
    return null;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `Request failed: ${response.status}`);
  }
  return data;
}

function showToast(message, type = 'success') {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);
}

function formatCurrency(value, currency = '') {
  if (value === null || value === undefined) return '—';
  const suffix = currency ? ` ${currency}` : '';
  return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + suffix;
}

function formatCrypto(value) {
  if (value === null || value === undefined) return '—';
  return value.toFixed(8);
}

function formatDatetime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString();
}

function setModeBanner(data) {
  const badge = document.getElementById('mode-badge');
  if (!badge) return;
  if (data.demo_mode) {
    badge.textContent = 'Demo Mode';
    badge.className = 'mode-badge demo';
  } else if (data.live_trading_enabled) {
    badge.textContent = 'Live Trading';
    badge.className = 'mode-badge live';
  } else {
    badge.textContent = 'Simulation / Dry-run';
    badge.className = 'mode-badge simulated';
  }
}

async function loadBalance() {
  try {
    const data = await apiGet('/api/balance');
    const el = document.getElementById('available-funds');
    if (!el) return;
    if (data.error) {
      el.textContent = '—';
      el.title = `Balance unavailable: ${data.error}`;
      el.classList.add('error');
    } else {
      el.textContent = formatCurrency(data.available_fiat, data.fiat_currency);
      el.title = '';
      el.classList.remove('error');
    }
  } catch (err) {
    console.error('Failed to load balance:', err);
  }
}

async function loadStatus() {
  try {
    const data = await apiGet('/api/status');
    if (!data) return;
    setModeBanner(data);

    const statusBadge = document.getElementById('status-badge');
    if (statusBadge) {
      const active = data.status === 'running' || data.status === 'waiting';
      statusBadge.innerHTML = (active ? '<span class="pulse-dot"></span> ' : '') + (active ? 'Bot running' : data.status === 'paused' ? 'Paused' : 'Stopped');
      statusBadge.className = 'badge ' + (active ? 'running' : data.status === 'paused' ? 'paused' : 'stopped');
      statusBadge.title = active ? 'The bot is active and processing cycles' : 'The bot is not currently running';
    }

    const pauseBtn = document.getElementById('pause-toggle-btn');
    if (pauseBtn) {
      const paused = data.paused;
      pauseBtn.textContent = paused ? 'Paused' : 'Pause';
      pauseBtn.setAttribute('data-action', paused ? '/api/bot/resume' : '/api/bot/pause');
      pauseBtn.classList.toggle('paused', paused);
    }

    const liveBtn = document.getElementById('live-trading-toggle-btn');
    if (liveBtn) {
      const envOverride = data.live_trading_env_override === true;
      const live = data.live_trading_enabled === true;
      liveBtn.textContent = `Live: ${live ? 'ON' : 'OFF'}`;
      liveBtn.dataset.live = live ? 'on' : 'off';
      liveBtn.classList.toggle('live-on', live);
      if (data.demo_mode) {
        liveBtn.disabled = true;
        liveBtn.title = 'Live trading is not available in demo mode';
      } else if (envOverride) {
        liveBtn.disabled = true;
        liveBtn.title = 'Controlled by the LIVE_TRADING_ENABLED environment variable — remove it from the server env to manage this toggle here';
      } else {
        liveBtn.disabled = false;
        liveBtn.title = live
          ? 'Live trading is ON — real orders will be placed. Click to switch to dry-run.'
          : 'Dry-run mode — no real orders. Click to enable live trading.';
      }
    }

    const totalInvested = document.getElementById('total-invested');
    if (totalInvested && data.portfolio) {
      totalInvested.textContent = formatCurrency(data.portfolio.total_invested, data.portfolio.quote_currency);
    }

    const currentValue = document.getElementById('current-value');
    if (currentValue && data.portfolio) {
      currentValue.textContent = formatCurrency(data.portfolio.current_value, data.portfolio.quote_currency);
    }

    const pl = document.getElementById('unrealized-pl');
    if (pl && data.portfolio) {
      pl.textContent = formatCurrency(data.portfolio.unrealized_pl, data.portfolio.quote_currency) +
        ` (${data.portfolio.unrealized_pl_percent.toFixed(2)}%)`;
      pl.style.color = data.portfolio.unrealized_pl >= 0 ? 'var(--green)' : 'var(--red)';
    }

    const nextCycle = document.getElementById('next-cycle');
    if (nextCycle) nextCycle.textContent = data.next_cycle_at || '—';

    const plannedBuys = document.getElementById('planned-buys');
    if (plannedBuys) plannedBuys.textContent = data.estimated_buys ?? '—';

    const budgetText = document.getElementById('monthly-budget-text');
    const budgetBar = document.getElementById('monthly-budget-bar');
    const budgetFill = document.getElementById('monthly-budget-fill');
    const budgetRemaining = document.getElementById('monthly-budget-remaining');
    if (budgetText && data.monthly_budget) {
      const limit = data.monthly_budget.limit;
      const spent = data.monthly_budget.spent;
      const currency = data.monthly_budget.currency || '';
      if (limit === null || limit === undefined) {
        budgetText.textContent = 'No limit';
        if (budgetBar) budgetBar.style.display = 'none';
        if (budgetRemaining) budgetRemaining.textContent = 'Set a max monthly amount in Settings';
      } else {
        const pct = limit > 0 ? Math.min(100, Math.max(0, (spent / limit) * 100)) : 0;
        budgetText.textContent = `${formatCurrency(spent, currency)} / ${formatCurrency(limit, currency)}`;
        if (budgetBar) {
          budgetBar.style.display = 'block';
          budgetFill.style.width = pct + '%';
          budgetFill.className = 'progress-fill ' + (pct >= 100 ? 'danger' : pct >= 75 ? 'warning' : '');
        }
        if (budgetRemaining) {
          const remaining = Math.max(0, limit - spent);
          budgetRemaining.textContent = `${formatCurrency(remaining, currency)} remaining`;
        }
      }
    }

    const lastCycle = document.getElementById('last-cycle');
    if (lastCycle) lastCycle.textContent = 'Last cycle: ' + (data.last_cycle_at || '—');

    const exchangeStatus = document.getElementById('exchange-status');
    if (exchangeStatus) {
      const exchangeOk = data.exchange_connected;
      exchangeStatus.innerHTML = (exchangeOk ? '<span class="pulse-dot"></span> ' : '') + (exchangeOk ? 'Kraken API' : 'Kraken Offline');
      exchangeStatus.className = 'badge ' + (exchangeOk ? 'ok' : 'warn');
    }

    const telegramStatus = document.getElementById('telegram-status');
    if (telegramStatus) {
      const telegramOk = data.telegram_connected;
      telegramStatus.innerHTML = (telegramOk ? '<span class="pulse-dot"></span> ' : '') + (telegramOk ? 'Telegram' : 'Telegram Off');
      telegramStatus.className = 'badge ' + (telegramOk ? 'ok' : 'warn');
    }

    const warningBadge = document.getElementById('warning-badge');
    if (warningBadge) {
      const count = data.alert_count !== undefined ? data.alert_count : (data.recent_warnings ? data.recent_warnings.length : 0);
      warningBadge.classList.toggle('has-warning', count > 0);
      warningBadge.title = count > 0 ? `${count} active alert(s)` : 'No active alerts';
      const span = warningBadge.querySelector('span');
      if (span) span.textContent = count > 0 ? `${count}` : '⚠';
    }
  } catch (err) {
    console.error('Failed to load status:', err);
    const statusBadge = document.getElementById('status-badge');
    if (statusBadge) {
      statusBadge.textContent = 'Unknown';
      statusBadge.className = 'badge warn';
    }
  }
}

// Modern replacement for window.confirm(): shows the shared in-page modal
// and resolves to true (confirmed) or false (cancelled / Escape / backdrop).
let _confirmResolve = null;

function uiConfirm(message, { title = 'Please confirm', danger = true, confirmText = 'Confirm' } = {}) {
  return new Promise((resolve) => {
    const modal = document.getElementById('confirm-modal');
    if (!modal) { resolve(window.confirm(message)); return; }
    document.getElementById('confirm-modal-title').textContent = title;
    document.getElementById('confirm-modal-message').textContent = message;
    const okBtn = document.getElementById('confirm-modal-ok');
    okBtn.textContent = confirmText;
    okBtn.className = danger ? 'btn btn-danger' : 'btn btn-primary';
    _confirmResolve = resolve;
    modal.style.display = 'block';
    okBtn.focus();
  });
}

function _closeConfirm(result) {
  const modal = document.getElementById('confirm-modal');
  if (modal) modal.style.display = 'none';
  if (_confirmResolve) { _confirmResolve(result); _confirmResolve = null; }
}

// Wire up action buttons with data-action attributes
document.addEventListener('click', async (e) => {
  const button = e.target.closest('button[data-action]');
  if (!button) return;
  e.preventDefault();
  const url = button.getAttribute('data-action');
  const confirmMessage = button.getAttribute('data-confirm');
  if (confirmMessage && !(await uiConfirm(confirmMessage, { title: 'Confirm action', danger: false }))) return;
  button.disabled = true;
  try {
    const data = await apiPost(url);
    showToast(data.message || 'Action completed', 'success');
    loadStatus();
  } catch (err) {
    showToast(err.message || 'Action failed', 'error');
  } finally {
    button.disabled = false;
  }
});

// Live trading toggle (topbar): persists via the settings API
document.addEventListener('click', async (e) => {
  const button = e.target.closest('#live-trading-toggle-btn');
  if (!button || button.disabled) return;
  const turningOn = button.dataset.live !== 'on';
  const message = turningOn
    ? 'Enable LIVE trading? The bot will place real orders with real funds.'
    : 'Disable live trading? The bot will keep running in dry-run mode (no real orders).';
  if (!(await uiConfirm(message, { title: turningOn ? 'Enable live trading' : 'Disable live trading', danger: turningOn }))) return;
  button.disabled = true;
  try {
    await apiPut('/api/settings', { live_trading_enabled: turningOn });
    showToast(turningOn ? 'Live trading enabled — real orders will be placed.' : 'Live trading disabled — running in dry-run mode.', 'success');
    loadStatus();
  } catch (err) {
    showToast(err.message || 'Action failed', 'error');
    loadStatus();
  }
});

// Theme toggle + global status loader
document.addEventListener('DOMContentLoaded', () => {
  // Confirm modal wiring
  document.querySelectorAll('[data-confirm-cancel]').forEach(el => {
    el.addEventListener('click', () => _closeConfirm(false));
  });
  const confirmOk = document.getElementById('confirm-modal-ok');
  if (confirmOk) confirmOk.addEventListener('click', () => _closeConfirm(true));
  document.addEventListener('keydown', (e) => {
    if (!_confirmResolve) return;
    if (e.key === 'Escape') _closeConfirm(false);
    else if (e.key === 'Enter') _closeConfirm(true);
  });

  loadStatus();
  setInterval(loadStatus, 10000);
  loadBalance();
  setInterval(loadBalance, 60000);

  const systemToggle = document.getElementById('nav-system-toggle');
  if (systemToggle) {
    systemToggle.addEventListener('click', () => {
      const group = document.getElementById('nav-system-group');
      if (!group) return;
      group.classList.toggle('open');
      systemToggle.setAttribute('aria-expanded', group.classList.contains('open') ? 'true' : 'false');
    });
  }

  const themeToggle = document.getElementById('theme-toggle');
  if (themeToggle) {
    const root = document.documentElement;
    const saved = localStorage.getItem('theme') || 'auto';
    root.setAttribute('data-theme', saved);
    themeToggle.addEventListener('click', () => {
      const current = root.getAttribute('data-theme');
      const next = current === 'dark' ? 'light' : current === 'light' ? 'auto' : 'dark';
      root.setAttribute('data-theme', next === 'auto' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : next);
      localStorage.setItem('theme', next);
    });
  }
});
