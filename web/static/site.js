/* Relative times are text-only; data remains server-rendered and escaped. */
const language = document.body.dataset.language || 'de';
document.querySelector('.language-form select')?.addEventListener('change', event => event.currentTarget.form.requestSubmit());
const copy = language === 'en' ? {updated: 'Updated:', last: 'Last known data', players: 'Players · last known'} : {updated: 'Stand:', last: 'Letzte bekannte Daten', players: 'Spieler · zuletzt'};
const relative = new Intl.RelativeTimeFormat(language, {numeric: 'auto'});
function updateTimes() {
  document.querySelectorAll('time[data-relative]').forEach(element => {
    const seconds = Math.min(0, Math.round((Date.parse(element.dateTime) - Date.now()) / 1000));
    if (!Number.isFinite(seconds)) return;
    const unit = Math.abs(seconds) < 60 ? 'second' : Math.abs(seconds) < 3600 ? 'minute' : Math.abs(seconds) < 86400 ? 'hour' : 'day';
    const divisor = {second: 1, minute: 60, hour: 3600, day: 86400}[unit];
    element.textContent = copy.updated + ' ' + relative.format(Math.round(seconds / divisor), unit);
    element.title = new Date(element.dateTime).toLocaleString(language === 'en' ? 'en-GB' : 'de-DE');
    const row = element.closest('.server-row');
    if (row && row.dataset.fresh === 'true' && seconds < -30) {
      row.dataset.fresh = 'false';
      row.classList.add('stale');
      row.querySelector('.state').classList.remove('online');
      row.querySelector('.state').textContent = copy.last;
      row.querySelector('.server-players .field-label').textContent = copy.players;
    }
  });
  const rows = [...document.querySelectorAll('.server-row[data-fresh="true"]')];
  const total = document.querySelector('[data-live-total]');
  const count = document.querySelector('[data-live-servers]');
  if (total) total.textContent = rows.length ? new Intl.NumberFormat(language).format(rows.reduce((sum, row) => sum + Number(row.dataset.players), 0)) : '—';
  if (count) count.textContent = rows.length;
}
updateTimes();
setInterval(updateTimes, 1000);
let refreshing = false;
const refreshSeconds = Number(document.body.dataset.refresh);
if (refreshSeconds) setInterval(async () => {
  if (document.hidden || refreshing) return;
  refreshing = true;
  try {
    const response = await fetch('/', {cache: 'no-store'});
    if (!response.ok) return;
    const parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
    // Leave navigation, focus, login/logout controls and scroll position intact.
    for (const selector of ['.server-list', '.live-total']) {
      const current = document.querySelector(selector);
      const next = parsed.querySelector(selector);
      if (current && next) current.replaceWith(next);
    }
    const currentNotice = document.querySelector('.notice');
    const nextNotice = parsed.querySelector('.notice');
    if (nextNotice && currentNotice) currentNotice.replaceWith(nextNotice);
    else if (nextNotice) document.querySelector('.overview').after(nextNotice);
    else if (currentNotice) currentNotice.remove();
    updateTimes();
  } catch { /* Keep the last measured data and its aging timestamp. */ }
  finally { refreshing = false; }
}, refreshSeconds * 1000);
document.querySelectorAll('form').forEach(form => form.addEventListener('submit', () => {
  const button = form.querySelector('button[type="submit"]');
  if (button) { button.setAttribute('aria-busy', 'true'); button.disabled = true; }
}));
window.addEventListener('pageshow', () => {
  document.querySelectorAll('[aria-busy="true"]').forEach(button => {
    button.removeAttribute('aria-busy'); button.disabled = false;
  });
});
