// 매일 새로 만드는 피드(feed.json, markets.json, programs.json)로 "오늘의 한국" 숫자와 목록을 채운다.
(async function () {
  const L = document.documentElement.lang === 'ko' ? 'ko' : 'en';
  const T = {
    en: { days: d => d + (d === 1 ? ' day left' : ' days left'), today: 'ends today', until: 'until ', asof: 'Data updated ' },
    ko: { days: d => d + '일 남음', today: '오늘까지', until: '~ ', asof: '자료 기준 ' },
  }[L];
  // 한국 시각의 오늘
  const now = new Date(Date.now() + 9 * 3600 * 1000);
  const today = now.toISOString().slice(0, 10);
  const dom = now.getUTCDate();
  const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const get = async f => { const r = await fetch(f, { cache: 'no-cache' }); if (!r.ok) throw new Error(f); return r.json(); };
  try {
    const [feed, markets, programs] = await Promise.all([get('feed.json'), get('markets.json'), get('programs.json')]);
    const venue = Object.fromEntries(feed.venues.map(v => [v.id, v]));
    const open = feed.exhibitions.filter(e => e.start <= today && e.end >= today);
    const marketsToday = markets.markets.filter(m => m.days.length && dom !== 31 && m.days.includes(dom % 10));
    const upcoming = programs.programs.filter(p => !p.end || p.end >= today);
    set('n-exhibitions', open.length);
    set('n-markets', marketsToday.length);
    set('n-programs', upcoming.length);
    const left = e => Math.round((Date.parse(e.end) - Date.parse(today)) / 86400000);
    const soon = open.slice().sort((a, b) => a.end.localeCompare(b.end)).slice(0, 6);
    document.getElementById('closing').innerHTML = soon.map(e => {
      const v = venue[e.venueId] || {};
      const d = left(e);
      return `<li>${esc(e.title)}<small>${esc(v.name || '')} · ${T.until}${esc(e.end)} · ${d === 0 ? T.today : T.days(d)}</small></li>`;
    }).join('');
    // 지역이 고루 보이게 시도마다 하나씩
    const seen = new Set();
    const pick = marketsToday.filter(m => !seen.has(m.sido) && seen.add(m.sido)).slice(0, 6);
    document.getElementById('markets-today').innerHTML = pick.map(m =>
      `<li>${esc(m.name)}<small>${esc(m.address)}</small></li>`).join('');
    set('asof', T.asof + feed.generatedAt.slice(0, 10));
    document.getElementById('live').hidden = false;
  } catch (e) { /* 피드를 못 받으면 이 구역은 숨긴 채로 둔다 */ }
})();
