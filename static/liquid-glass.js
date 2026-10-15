/* Liquid Glass motoru: arka plan sahnesi + yüzey başına kenar kırılması (SVG displacement) + imleç ışığı
   + 💧 ayar paneli. Sadece body[data-theme="liquid"] iken çalışır. Ayarlar bu tarayıcıda saklanır. */
(function(){
  const SEL = '.box,.nav';
  const KEY = 'lg_cfg';
  const DEF = { blur:16, refract:46, band:26, radius:22, tint:8, spec:55, sat:185, bright:106, speed:10, anim:true, refractOn:true,
                c1:'#4f7bff', c2:'#ff5fb7', c3:'#2ee6c8', c4:'#ffb547', base:'#070915', accent:'#8fd8ff' };
  const PRESETS = {
    'Aurora':  { c1:'#4f7bff', c2:'#ff5fb7', c3:'#2ee6c8', c4:'#ffb547', base:'#070915', accent:'#8fd8ff' },
    'Okyanus': { c1:'#0ea5e9', c2:'#22d3ee', c3:'#6366f1', c4:'#14b8a6', base:'#03101a', accent:'#67e8f9' },
    'Gün Batımı': { c1:'#f97316', c2:'#ec4899', c3:'#facc15', c4:'#8b5cf6', base:'#140708', accent:'#fdba74' },
    'Orman':   { c1:'#22c55e', c2:'#84cc16', c3:'#06b6d4', c4:'#eab308', base:'#03110a', accent:'#86efac' },
    'Mor':     { c1:'#8b5cf6', c2:'#d946ef', c3:'#6366f1', c4:'#f472b6', base:'#0b0616', accent:'#d8b4fe' },
    'Grafit':  { c1:'#64748b', c2:'#94a3b8', c3:'#475569', c4:'#cbd5e1', base:'#06070a', accent:'#e2e8f0' }
  };
  // Zayif cihazda (<=4 cekirdek / <=4 GB) kenar kirilmasi varsayilan kapali gelir; kullanici 💧'dan acabilir.
  // Kaydedilmis ayar varsa ona dokunulmaz.
  if ((navigator.hardwareConcurrency || 8) <= 4 || (navigator.deviceMemory || 8) <= 4) DEF.refractOn = false;
  const isChromium = !!(navigator.userAgentData && navigator.userAgentData.brands &&
      navigator.userAgentData.brands.some(b => /Chromium|Google Chrome|Edge|Opera/i.test(b.brand)));
  let cfg = load(), svg, defs, ro, io, on = false, uid = 0, panel;
  const maps = new WeakMap();

  function load(){ try { return Object.assign({}, DEF, JSON.parse(localStorage.getItem(KEY) || '{}')); } catch (e) { return Object.assign({}, DEF); } }
  function save(){ try { localStorage.setItem(KEY, JSON.stringify(cfg)); } catch (e) {} }
  function applyVars(){
    const b = document.body.style, set = (k, v) => b.setProperty(k, v);
    set('--lg-blur', cfg.blur + 'px'); set('--lg-radius', cfg.radius + 'px'); set('--lg-tint', (cfg.tint / 100).toFixed(3));
    set('--lg-spec', (cfg.spec / 100).toFixed(2)); set('--lg-sat', cfg.sat + '%'); set('--lg-bright', (cfg.bright / 100).toFixed(2));
    set('--lg-speed', Math.max(.1, cfg.speed / 10).toFixed(2)); set('--lg-play', cfg.anim ? 'running' : 'paused');
    ['c1','c2','c3','c4','base','accent'].forEach(k => set('--lg-' + k, cfg[k]));
  }
  function clearVars(){ ['blur','radius','tint','spec','sat','bright','speed','play','c1','c2','c3','c4','base','accent'].forEach(k => document.body.style.removeProperty('--lg-' + k)); }

  function scene(){
    if (document.querySelector('.lg-scene')) return;
    const s = document.createElement('div'); s.className = 'lg-scene'; s.setAttribute('aria-hidden', 'true');
    s.innerHTML = '<i></i><i></i><i></i><i></i><b></b>';
    document.body.prepend(s);
  }
  function ensureSvg(){
    if (svg) return;
    svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '0'); svg.setAttribute('height', '0');
    svg.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden';
    defs = document.createElementNS(svg.namespaceURI, 'defs'); svg.appendChild(defs);
    document.body.appendChild(svg);
  }
  /* Yuvarlatılmış dikdörtgen SDF'inden displacement map: R/G = içeri doğru normal * (kenara yakınlık)^2.
     radii: [sol-üst, sağ-üst, sağ-alt, sol-alt] — nav gibi sadece alt köşesi yuvarlak yüzeyler için. */
  function buildMap(w, h, radii, band){
    const k = Math.min(1, 700 / Math.max(w, h));
    const cw = Math.max(8, Math.round(w * k)), ch = Math.max(8, Math.round(h * k));
    const c = document.createElement('canvas'); c.width = cw; c.height = ch;
    const ctx = c.getContext('2d'); const img = ctx.createImageData(cw, ch); const d = img.data;
    const B = band * k, hx = cw / 2, hy = ch / 2;
    const R = radii.map(r => Math.min(r * k, hx, hy));
    for (let y = 0; y < ch; y++){
      for (let x = 0; x < cw; x++){
        const px = x + .5 - hx, py = y + .5 - hy;
        const rr = px < 0 ? (py < 0 ? R[0] : R[3]) : (py < 0 ? R[1] : R[2]);
        const qx = Math.abs(px) - (hx - rr), qy = Math.abs(py) - (hy - rr);
        const sd = Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) + Math.min(Math.max(qx, qy), 0) - rr;
        const dist = -sd; let Rv = 128, Gv = 128;
        if (dist < B){
          let nx, ny;
          if (qx > 0 && qy > 0){ const l = Math.hypot(qx, qy) || 1; nx = qx / l * Math.sign(px); ny = qy / l * Math.sign(py); }
          else if (qx > qy){ nx = Math.sign(px); ny = 0; } else { nx = 0; ny = Math.sign(py); }
          const t = Math.max(0, 1 - Math.max(dist, 0) / B), m = t * t;
          Rv = 128 - nx * m * 127; Gv = 128 - ny * m * 127;
        }
        const i = (y * cw + x) * 4; d[i] = Rv; d[i + 1] = Gv; d[i + 2] = 128; d[i + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
    return c.toDataURL('image/png');
  }
  // Ayni boyut/kose icin harita bir kez uretilir (PNG kodlama pahali); boyut 8px adimlara yuvarlanir.
  const mapCache = new Map();
  function getMap(w, h, radii, band){
    const qw = Math.max(8, Math.round(w / 8) * 8), qh = Math.max(8, Math.round(h / 8) * 8);
    const k = [qw, qh, radii.join(','), band].join('|');
    let u = mapCache.get(k);
    if (!u){ u = buildMap(qw, qh, radii, band); if (mapCache.size > 150) mapCache.clear(); mapCache.set(k, u); }
    return u;
  }
  const visible = new WeakMap();   // ekran disindaki yuzeyler icin harita uretilmez (gorununce uretilir)
  function refract(el){
    if (visible.get(el) === false) return;
    const b = el.getBoundingClientRect(); const w = Math.round(b.width), h = Math.round(b.height);
    if (w < 20 || h < 20) return;
    const cs = getComputedStyle(el);
    const radii = ['borderTopLeftRadius','borderTopRightRadius','borderBottomRightRadius','borderBottomLeftRadius'].map(p => parseFloat(cs[p]) || 0);
    const key = [w, h, radii.join(','), cfg.band, cfg.refract].join('|'); let st = maps.get(el);
    if (st && st.key === key) return;
    if (!st){ st = { id: 'lg-f' + (++uid) }; maps.set(el, st); }
    st.key = key;
    let f = document.getElementById(st.id);
    if (!f){ f = document.createElementNS(svg.namespaceURI, 'filter'); f.id = st.id; defs.appendChild(f); }
    f.setAttribute('x', '0'); f.setAttribute('y', '0'); f.setAttribute('width', w); f.setAttribute('height', h);
    f.setAttribute('filterUnits', 'userSpaceOnUse'); f.setAttribute('color-interpolation-filters', 'sRGB');
    f.innerHTML = `<feImage href="${getMap(w, h, radii, cfg.band)}" x="0" y="0" width="${w}" height="${h}" preserveAspectRatio="none" result="m"/>` +
      `<feDisplacementMap in="SourceGraphic" in2="m" scale="${cfg.refract}" xChannelSelector="R" yChannelSelector="G"/>`;
    el.style.setProperty('--lg-f', `url(#${st.id})`);
  }
  let pending = new Set(), raf = 0;
  function queue(el){ pending.add(el); if (!raf) raf = requestAnimationFrame(() => { raf = 0; const p = pending; pending = new Set(); if (on && ro) p.forEach(refract); }); }
  function refreshAll(){ document.querySelectorAll(SEL).forEach(el => { const st = maps.get(el); if (st) st.key = ''; queue(el); }); }
  let mvEv = null, mvRaf = 0;
  function onMove(e){
    mvEv = e; if (mvRaf) return;
    mvRaf = requestAnimationFrame(() => {
      mvRaf = 0; const ev = mvEv;
      const el = ev.target.closest && ev.target.closest('.box'); if (!el) return;
      const b = el.getBoundingClientRect();
      el.style.setProperty('--mx', (ev.clientX - b.left) + 'px'); el.style.setProperty('--my', (ev.clientY - b.top) + 'px');
    });
  }
  // Kaydirirken ve sekme arka plandayken hareketli sahne durur (gorunum ayni, islemci rahatlar)
  let scT = 0;
  function onScroll(){ document.body.classList.add('lg-pause'); clearTimeout(scT); scT = setTimeout(() => document.body.classList.remove('lg-pause'), 200); }
  function onVis(){ document.body.classList.toggle('lg-hidden', document.hidden); }
  function startRefract(){
    if (!isChromium || !cfg.refractOn || ro) return;
    ensureSvg(); document.body.classList.add('lg-refract');
    const rt = new WeakMap();
    // Boyut degisirken mevcut harita aninda yeni boyuta esnetilir (bosluk olmaz); asil harita 150 ms sakinlesince uretilir
    const stretch = el => { const st = maps.get(el), f = st && document.getElementById(st.id); if (!f) return;
      const r = el.getBoundingClientRect(), w = Math.round(r.width), h = Math.round(r.height);
      f.setAttribute('width', w); f.setAttribute('height', h);
      const im = f.querySelector('feImage'); if (im){ im.setAttribute('width', w); im.setAttribute('height', h); } };
    ro = new ResizeObserver(es => es.forEach(e => { const el = e.target; stretch(el); clearTimeout(rt.get(el)); rt.set(el, setTimeout(() => queue(el), 150)); }));
    io = new IntersectionObserver(es => es.forEach(e => { visible.set(e.target, e.isIntersecting); if (e.isIntersecting) queue(e.target); }), { rootMargin: '300px' });
    document.querySelectorAll(SEL).forEach(el => { ro.observe(el); io.observe(el); queue(el); });
  }
  function stopRefract(){
    document.body.classList.remove('lg-refract');
    if (ro){ ro.disconnect(); ro = null; }
    if (io){ io.disconnect(); io = null; }
  }

  /* ---------- 💧 Ayar paneli ---------- */
  // Panel metinleri sayfa diliyle (localStorage 'lang') gelir: tr / en / ru / tm
  const LGT = {
    tr: { blur:'Bulanıklık', refract:'Kırılma', band:'Kenar kalınlığı', radius:'Köşe yuvarlaklığı', tint:'Opaklık', spec:'Parlama', sat:'Renk doygunluğu', bright:'Parlaklık', speed:'Hareket hızı',
          gear:'Liquid Glass ayarları', glass:'Cam', refr:'Kenar kırılması', nosup:' (bu tarayıcı desteklemiyor)', anim:'Arka plan hareketli', pal:'Renk paleti', own:'Özel renkler', light:'Işık', base:'Zemin', accent:'Vurgu', reset:'↺ Varsayılana dön',
          Aurora:'Aurora', Okyanus:'Okyanus', 'Gün Batımı':'Gün Batımı', Orman:'Orman', Mor:'Mor', Grafit:'Grafit' },
    en: { blur:'Blur', refract:'Refraction', band:'Edge width', radius:'Corner radius', tint:'Opacity', spec:'Highlight', sat:'Saturation', bright:'Brightness', speed:'Motion speed',
          gear:'Liquid Glass settings', glass:'Glass', refr:'Edge refraction', nosup:' (not supported in this browser)', anim:'Animated background', pal:'Color palette', own:'Custom colors', light:'Light', base:'Base', accent:'Accent', reset:'↺ Reset to default',
          Aurora:'Aurora', Okyanus:'Ocean', 'Gün Batımı':'Sunset', Orman:'Forest', Mor:'Purple', Grafit:'Graphite' },
    ru: { blur:'Размытие', refract:'Преломление', band:'Ширина края', radius:'Скругление углов', tint:'Непрозрачность', spec:'Блик', sat:'Насыщенность', bright:'Яркость', speed:'Скорость движения',
          gear:'Настройки Liquid Glass', glass:'Стекло', refr:'Преломление краёв', nosup:' (не поддерживается этим браузером)', anim:'Анимированный фон', pal:'Палитра', own:'Свои цвета', light:'Свет', base:'Фон', accent:'Акцент', reset:'↺ Сбросить',
          Aurora:'Аврора', Okyanus:'Океан', 'Gün Batımı':'Закат', Orman:'Лес', Mor:'Фиолетовый', Grafit:'Графит' },
    tm: { blur:'Bulançaklyk', refract:'Döwülme', band:'Gyra galyňlygy', radius:'Burç tegelekligi', tint:'Aýdyňsyzlyk', spec:'Ýalpyldy', sat:'Reňk doýgunlygy', bright:'Ýagtylyk', speed:'Hereket tizligi',
          gear:'Liquid Glass sazlamalary', glass:'Aýna', refr:'Gyra döwülmesi', nosup:' (bu brauzer goldamaýar)', anim:'Hereketli fon', pal:'Reňk palitrasy', own:'Öz reňkleri', light:'Yşyk', base:'Fon', accent:'Nygtaw', reset:'↺ Başlangyja gaýt',
          Aurora:'Aurora', Okyanus:'Umman', 'Gün Batımı':'Gün ýaşyşy', Orman:'Tokaý', Mor:'Gyrmyzy', Grafit:'Grafit' }
  };
  const lang = () => { try { return localStorage.getItem('lang') || 'tr'; } catch (e) { return 'tr'; } };
  const t = k => (LGT[lang()] || LGT.tr)[k] || LGT.tr[k] || k;
  const SLIDERS = [
    ['blur', 0, 40, 'px'], ['refract', 0, 120, ''], ['band', 6, 60, 'px'],
    ['radius', 0, 40, 'px'], ['tint', 0, 40, '%'], ['spec', 0, 100, '%'],
    ['sat', 100, 300, '%'], ['bright', 70, 140, '%'], ['speed', 1, 40, '']
  ].map(([k, mn, mx, u]) => [k, k, mn, mx, u]);
  function gear(){
    document.querySelectorAll('.theme-selector').forEach(ts => {
      if (ts.querySelector('.lg-gear')) return;
      const g = document.createElement('span'); g.className = 'lg-gear'; g.title = t('gear'); g.textContent = '💧';
      g.addEventListener('click', e => { e.stopPropagation(); togglePanel(); });
      ts.appendChild(g);
    });
  }
  function buildPanel(){
    panel = document.createElement('div'); panel.className = 'lg-panel'; panel.hidden = true; panel.dataset.lang = lang();
    let h = '<h4>💧 Liquid Glass <span data-x>✕</span></h4>';
    h += `<div class="lg-sec">${t('glass')}</div>`;
    SLIDERS.forEach(([k, l, mn, mx, u]) => {
      h += `<div class="lg-row"><label>${t(l)}</label><input type="range" min="${mn}" max="${mx}" data-k="${k}" value="${cfg[k]}"><output data-o="${k}">${cfg[k]}${u}</output></div>`;
    });
    h += `<label class="lg-chk"><input type="checkbox" data-c="refractOn" ${cfg.refractOn ? 'checked' : ''}> ${t('refr')}${isChromium ? '' : t('nosup')}</label>`;
    h += `<label class="lg-chk"><input type="checkbox" data-c="anim" ${cfg.anim ? 'checked' : ''}> ${t('anim')}</label>`;
    h += `<div class="lg-sec">${t('pal')}</div><div class="lg-presets">`;
    Object.entries(PRESETS).forEach(([n, p]) => { h += `<button type="button" data-p="${n}" style="background:linear-gradient(135deg,${p.c1},${p.c2},${p.c3})">${t(n)}</button>`; });
    h += `</div><div class="lg-sec">${t('own')}</div><div class="lg-colors">`;
    ['c1','c2','c3','c4'].forEach(k => { h += `<input type="color" data-col="${k}" value="${cfg[k]}" title="${t('light')} ${k.slice(1)}">`; });
    h += `<input type="color" data-col="base" value="${cfg.base}" title="${t('base')}"><input type="color" data-col="accent" value="${cfg.accent}" title="${t('accent')}"></div>`;
    h += `<button type="button" class="lg-reset">${t('reset')}</button>`;
    panel.innerHTML = h;
    document.body.appendChild(panel);
    const units = Object.fromEntries(SLIDERS.map(s => [s[0], s[4]]));
    let regen = 0;
    panel.addEventListener('input', e => {
      const t = e.target;
      if (t.dataset.k){ cfg[t.dataset.k] = +t.value; panel.querySelector(`[data-o="${t.dataset.k}"]`).textContent = t.value + units[t.dataset.k];
        if (t.dataset.k === 'band' || t.dataset.k === 'refract' || t.dataset.k === 'radius'){ clearTimeout(regen); regen = setTimeout(refreshAll, 120); } }
      if (t.dataset.col) cfg[t.dataset.col] = t.value;
      applyVars(); save();
    });
    panel.addEventListener('change', e => {
      const t = e.target; if (!t.dataset.c) return;
      cfg[t.dataset.c] = t.checked; applyVars(); save();
      if (t.dataset.c === 'refractOn'){ cfg.refractOn ? startRefract() : stopRefract(); }
    });
    panel.addEventListener('click', e => {
      const t = e.target;
      if (t.dataset.x !== undefined) panel.hidden = true;
      if (t.dataset.p){ Object.assign(cfg, PRESETS[t.dataset.p]); syncPanel(); applyVars(); save(); }
      if (t.classList.contains('lg-reset')){ cfg = Object.assign({}, DEF); syncPanel(); applyVars(); save(); stopRefract(); startRefract(); refreshAll(); }
    });
    document.addEventListener('click', e => { if (!panel.hidden && !panel.contains(e.target)) panel.hidden = true; });
  }
  function syncPanel(){
    if (!panel) return;
    panel.querySelectorAll('[data-k]').forEach(i => { i.value = cfg[i.dataset.k]; i.dispatchEvent(new Event('input', { bubbles: true })); });
    panel.querySelectorAll('[data-col]').forEach(i => { i.value = cfg[i.dataset.col]; });
    panel.querySelectorAll('[data-c]').forEach(i => { i.checked = !!cfg[i.dataset.c]; });
  }
  function togglePanel(){
    if (panel && panel.dataset.lang !== lang()){ panel.remove(); panel = null; }  // dil degistiyse yeniden ciz
    if (!panel) buildPanel(); panel.hidden = !panel.hidden;
  }

  function enable(){
    if (on) return; on = true; scene(); applyVars(); gear();
    document.addEventListener('pointermove', onMove, { passive: true });
    window.addEventListener('scroll', onScroll, { passive: true }); document.addEventListener('visibilitychange', onVis);
    startRefract();
  }
  function disable(){
    if (!on) return; on = false;
    document.removeEventListener('pointermove', onMove);
    window.removeEventListener('scroll', onScroll); document.removeEventListener('visibilitychange', onVis);
    document.body.classList.remove('lg-pause', 'lg-hidden');
    stopRefract(); clearVars(); if (panel) panel.hidden = true;
  }
  function check(){ (document.body.getAttribute('data-theme') === 'liquid') ? enable() : disable(); }
  function boot(){
    check();
    new MutationObserver(check).observe(document.body, { attributes: true, attributeFilter: ['data-theme'] });
    new MutationObserver(ms => { if (!ro) return; ms.forEach(m => m.addedNodes.forEach(n => {
      if (n.nodeType !== 1) return; [n, ...n.querySelectorAll(SEL)].forEach(el => { if (el.matches && el.matches(SEL)) { ro.observe(el); if (io) io.observe(el); queue(el); } });
    })); }).observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
