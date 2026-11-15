// Theater player for clip tiles outside match pages (stats page "Bu ayın klipleri" etc.):
// a clip plays large over the page; ‹ › / arrow keys for the others, plays on to the next one,
// F fullscreen, Esc / back / × to close (you stay on the page), "Maça git" opens the clip's match.
// Tiles: <a class="tclip" href="match url" data-src data-poster data-title data-sub>.
(() => {
  const T = { i: 0, el: null, items: [] };
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  function build() {
    T.el = document.createElement('div');
    T.el.className = 'theater';
    T.el.innerHTML = `<div class="bar"><div class="who"></div><span class="count"></span>
        <a class="tbtn go" href="#">Maça git &rarr;</a>
        <button class="tbtn fs" title="Tam ekran (F)">&#x26F6; <span>Tam ekran</span></button>
        <button class="tbtn x" title="Kapat (Esc)">&times;</button></div>
      <div class="stage"><video controls playsinline></video>
        <button class="nav prev" title="Önceki (&larr;)">&#8249;</button><button class="nav next" title="Sonraki (&rarr;)">&#8250;</button></div>
      <div class="hint">&larr; &rarr; klipler &middot; F tam ekran &middot; Esc kapat</div>`;
    document.body.appendChild(T.el);
    const v = T.el.querySelector('video');
    T.el.querySelector('.x').onclick = () => (history.state && history.state.theater ? history.back() : close());
    T.el.querySelector('.fs').onclick = () => (v.requestFullscreen || v.webkitEnterFullscreen || (() => {})).call(v);
    T.el.querySelector('.prev').onclick = () => show(T.i - 1);
    T.el.querySelector('.next').onclick = () => show(T.i + 1);
    T.el.addEventListener('click', e => { if (e.target === T.el) T.el.querySelector('.x').click(); });
    v.addEventListener('ended', () => { if (T.i < T.items.length - 1) show(T.i + 1); });
    document.addEventListener('keydown', e => {
      if (!T.el.classList.contains('on') || e.target.closest('input, textarea, select')) return;
      if (e.key === 'Escape' && !document.fullscreenElement) T.el.querySelector('.x').click();
      else if (e.key === 'ArrowRight') { e.preventDefault(); show(T.i + 1); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); show(T.i - 1); }
      else if (e.key === 'f' || e.key === 'F') T.el.querySelector('.fs').click();
    });
    window.addEventListener('popstate', () => { if (T.el.classList.contains('on')) close(); });
  }

  function show(i) {
    if (i < 0 || i >= T.items.length) return;
    T.i = i;
    const it = T.items[i], v = T.el.querySelector('video');
    v.poster = it.poster || '';
    v.src = it.src;
    v.play().catch(() => {});
    T.el.querySelector('.who').innerHTML = (it.avatar || '') +
      `<div style="min-width:0"><b>${esc(it.title)}</b><span>${esc(it.sub)}</span></div>`;
    T.el.querySelector('.count').textContent = (i + 1) + ' / ' + T.items.length;
    T.el.querySelector('.go').href = it.match;
    T.el.querySelector('.prev').disabled = i === 0;
    T.el.querySelector('.next').disabled = i === T.items.length - 1;
  }

  function open(items, i) {
    if (!T.el) build();
    T.items = items;
    if (!T.el.classList.contains('on')) history.pushState({theater: true}, '');
    T.el.classList.add('on');
    document.body.classList.add('theater-open');
    show(i);
  }

  function close() {
    const v = T.el.querySelector('video');
    v.pause();
    v.removeAttribute('src'); v.load();
    T.el.classList.remove('on');
    document.body.classList.remove('theater-open');
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.tclips').forEach(group => {
      const tiles = [...group.querySelectorAll('.tclip[data-src]')];
      const items = tiles.map(a => ({src: a.dataset.src, poster: a.dataset.poster, title: a.dataset.title,
                                     sub: a.dataset.sub, match: a.getAttribute('href'),
                                     avatar: (a.querySelector('.tx .av, .tx .ini') || {}).outerHTML}));
      tiles.forEach((a, i) => a.addEventListener('click', e => {
        if (e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1) return;  // new tab: the match page as before
        e.preventDefault();
        open(items, i);
      }));
    });
  });
})();
