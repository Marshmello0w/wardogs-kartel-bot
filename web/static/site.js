/* Relative times are text-only; data remains server-rendered and escaped. */
const language = document.body.dataset.language || 'de';
const languageMenu = document.querySelector('.language-menu');
const languageToggle = document.querySelector('.language-toggle');
const languageOptions = document.querySelector('.language-options');
function closeLanguageMenu() { if (languageOptions) languageOptions.hidden = true; if (languageToggle) languageToggle.setAttribute('aria-expanded', 'false'); }
languageToggle?.addEventListener('click', () => { const open = languageOptions.hidden; languageOptions.hidden = !open; languageToggle.setAttribute('aria-expanded', String(open)); });
document.addEventListener('click', event => { if (languageMenu && !languageMenu.contains(event.target)) closeLanguageMenu(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape') closeLanguageMenu(); });
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

const rewardCopy = language === 'en'
  ? {ready: 'Redemption available', not_enough_points: 'Not enough quest points.', not_online: 'You must be online on a server.', same_faction: 'You are already in this faction.', balance_limit: 'Not available: team balance would differ by more than four players.', no_vip_slots: 'There are no VIP slots available on this server.', vip_exists: 'You already have a reserved or active VIP on this server.'}
  : {ready: 'Einlösung möglich', not_enough_points: 'Nicht genügend Quest-Punkte.', not_online: 'Du musst auf einem Server online sein.', same_faction: 'Du bist bereits in dieser Fraktion.', balance_limit: 'Nicht möglich: Die Team-Balance würde mehr als vier Spieler abweichen.', no_vip_slots: 'Auf diesem Server sind keine VIP-Plätze frei.', vip_exists: 'Du hast auf diesem Server bereits einen reservierten oder aktiven VIP.'};

document.querySelectorAll('[data-reward-form]').forEach(form => {
  let availability;
  try { availability = JSON.parse(form.dataset.availability || '{}'); } catch { availability = {}; }
  const bar = form.querySelector('.reward-availability');
  const button = form.querySelector('button[type="submit"]');
  const updateRewardAvailability = () => {
    const type = form.dataset.rewardForm;
    const option = type === 'faction'
      ? availability[form.elements.faction?.value]
      : availability[form.elements.server?.value]?.[form.elements.duration?.value];
    const enabled = Boolean(option?.available);
    if (bar) {
      bar.classList.toggle('available', enabled);
      bar.classList.toggle('unavailable', !enabled);
      bar.textContent = rewardCopy[option?.reason] || rewardCopy.not_online;
    }
    if (button) button.disabled = !enabled;
  };
  form.querySelectorAll('select').forEach(select => select.addEventListener('change', updateRewardAvailability));
  updateRewardAvailability();
});
