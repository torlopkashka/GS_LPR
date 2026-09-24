// Обновление панели: статусы, превью камер, последние события.
(function () {
  const agentEl = document.querySelector('#agent span');
  if (!agentEl) return;

  async function status() {
    try {
      const r = await fetch('/api/status');
      if (r.status === 401) { location.href = '/login'; return; }
      const s = await r.json();
      agentEl.textContent = s.agent.online
        ? 'на связи' + (s.agent.info.driver ? ' (' + s.agent.info.driver + ')' : '')
        : 'НЕ на связи';
      agentEl.className = s.agent.online ? 'ok' : 'bad';
      const net = document.getElementById('net');
      const bx = s.bitrix24;
      if (bx.enabled) {
        net.textContent = !bx.internet ? 'Интернет: НЕТ связи с Битрикс24'
          : bx.ready ? 'Битрикс24: бот на связи' : 'Битрикс24: бот настраивается…';
        net.className = 'small ' + (bx.internet && bx.ready ? 'muted' : 'bad');
      } else {
        net.textContent = 'Бот Битрикс24 не настроен';
      }
      for (const c of s.cameras) {
        const el = document.querySelector(`[data-cam="${c.id}"] .cam-status`);
        if (!el) continue;
        el.textContent = c.connected
          ? `● онлайн · ${c.analyze_ms} мс` + (c.last_plate ? ` · ${c.last_plate}` : '')
          : `● нет потока${c.error ? ': ' + c.error : ''}`;
        el.className = 'cam-status ' + (c.connected ? 'ok' : 'bad');
      }
    } catch (e) { agentEl.textContent = 'сервер недоступен'; agentEl.className = 'bad'; }
  }

  function previews() {
    if (document.hidden) return;
    document.querySelectorAll('img.preview').forEach(img => {
      if (img.dataset.loading) return;
      img.dataset.loading = '1';
      const next = new Image();
      next.onload = () => { img.src = next.src; delete img.dataset.loading; };
      next.onerror = () => { delete img.dataset.loading; };
      next.src = img.dataset.src + '?t=' + Date.now();
    });
  }

  async function events() {
    if (document.hidden) return;
    const r = await fetch('/partials/events');
    if (r.ok) document.getElementById('events-body').innerHTML = await r.text();
  }

  document.getElementById('open-btn').addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    if (!confirm('Открыть ворота?')) return;
    btn.disabled = true;
    try {
      const r = await fetch('/api/open', { method: 'POST' });
      const d = await r.json();
      alert(d.ok ? 'Команда отправлена' : 'Ошибка: ' + d.detail);
    } finally { btn.disabled = false; events(); }
  });

  status(); previews();
  setInterval(status, 3000);
  setInterval(previews, 1500);
  setInterval(events, 5000);
})();
