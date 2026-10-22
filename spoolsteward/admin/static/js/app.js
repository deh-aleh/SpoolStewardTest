// stacked tables on phones: give every cell the name of its column (the header row is hidden there)
document.querySelectorAll('table.stack').forEach(function (t) {
  var first = t.querySelector('tr'), heads = first ? Array.prototype.map.call(first.querySelectorAll('th'), function (h) { return h.textContent.trim(); }) : [];
  if (!heads.length) return;
  t.querySelectorAll('tr').forEach(function (tr) {
    var cells = tr.querySelectorAll('td');
    Array.prototype.forEach.call(cells, function (td, i) {
      if (td.colSpan === 1 && !td.hasAttribute('data-l') && heads[i]) td.setAttribute('data-l', heads[i]);
    });
  });
});

// show/hide password (_pwfield.html): the button is hidden until scripts run
function pwShow(b, show) {
  b.parentNode.querySelector('input').type = show ? 'text' : 'password';
  b.classList.toggle('shown', show);
  b.setAttribute('aria-pressed', show ? 'true' : 'false');
  var label = show ? b.dataset.hide : b.dataset.show;
  b.setAttribute('aria-label', label); b.title = label;
}
document.querySelectorAll('.pwtoggle').forEach(function (b) {
  b.hidden = false;
  b.addEventListener('click', function () { pwShow(b, !b.classList.contains('shown')); });
});
// hidden again before sending, so the browser's password manager still sees a password box
document.addEventListener('submit', function (e) {
  e.target.querySelectorAll('.pwtoggle.shown').forEach(function (b) { pwShow(b, false); });
});

// copy button (macros.html): copies the text next to it, shows a check mark for a moment
document.addEventListener('click', function (e) {
  var b = e.target.closest && e.target.closest('button.copy');
  if (!b) return;
  var el = b.parentNode.querySelector('[data-copy]'), text = (el ? el.textContent : '').trim();
  function done() { b.classList.add('done'); b.title = b.dataset.tCopied; setTimeout(function () { b.classList.remove('done'); b.title = b.dataset.tCopy; }, 1500); }
  function fallback() {
    var t = document.createElement('textarea'); t.value = text; t.style.position = 'fixed'; t.style.opacity = '0';
    (b.closest('dialog') || document.body).appendChild(t); t.select();
    try { if (document.execCommand('copy')) done(); } catch (x) {} t.remove();
  }
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
});

// text boxes with a data-suggest attribute (JSON list): the list opens on click and narrows as you type; free text is still allowed
Array.prototype.forEach.call(document.querySelectorAll('input[data-suggest]'), function (inp) {
  var items = []; try { items = JSON.parse(inp.getAttribute('data-suggest')); } catch (x) {}
  var wrap = document.createElement('span'); wrap.className = 'sg';
  inp.parentNode.insertBefore(wrap, inp); wrap.appendChild(inp);
  var box = document.createElement('ul'); box.className = 'suggest'; box.hidden = true; wrap.appendChild(box);
  var sel = -1;
  function show() {
    var q = inp.value.trim().toLowerCase();
    var hits = items.filter(function (t) { return t.toLowerCase().indexOf(q) >= 0; })
                    .sort(function (a, b) { return (b.toLowerCase().indexOf(q) === 0) - (a.toLowerCase().indexOf(q) === 0); });
    box.innerHTML = ''; sel = -1;
    hits.forEach(function (t) {
      var li = document.createElement('li'), i = t.toLowerCase().indexOf(q);
      if (q && i >= 0) { li.appendChild(document.createTextNode(t.slice(0, i))); var m = document.createElement('mark'); m.textContent = t.slice(i, i + q.length); li.appendChild(m); li.appendChild(document.createTextNode(t.slice(i + q.length))); }
      else li.textContent = t;
      li.addEventListener('mousedown', function (e) { e.preventDefault(); inp.value = t; box.hidden = true; });
      box.appendChild(li);
    });
    box.hidden = !hits.length;
  }
  function move(d) { var l = box.children; if (!l.length) return; if (sel >= 0) l[sel].classList.remove('on'); sel = (sel + d + l.length) % l.length; l[sel].classList.add('on'); l[sel].scrollIntoView({block: 'nearest'}); }
  inp.addEventListener('focus', show); inp.addEventListener('click', show); inp.addEventListener('input', show);
  inp.addEventListener('blur', function () { box.hidden = true; });
  inp.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); if (box.hidden) show(); move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter' && sel >= 0 && !box.hidden) { e.preventDefault(); inp.value = box.children[sel].textContent; box.hidden = true; }
    else if (e.key === 'Escape') box.hidden = true;
  });
});

// forms marked data-keep: when the server sends you back with an error, what you had typed is put back
// (never passwords or files). data-keep="always" also restores after a successful reload (the scan page).
(function () {
  var K = 'formkeep:' + location.pathname, TTL = 120000, forms = document.querySelectorAll('form[data-keep]');
  function field(f) { return Array.prototype.filter.call(f.elements, function (e) { return e.name && !/^(password|file|hidden|submit|button)$/.test(e.type) && !e.hasAttribute('data-nokeep'); }); }
  try {
    var saved = JSON.parse(sessionStorage.getItem(K) || 'null');
    sessionStorage.removeItem(K);
    if (saved && Date.now() - saved.t < TTL) Array.prototype.forEach.call(forms, function (f, i) {
      var data = saved.f[i]; if (!data || !(document.querySelector('.flash.err') || f.getAttribute('data-keep') === 'always')) return;
      field(f).forEach(function (e) {
        var v = data[e.name]; if (v === undefined) return;
        if (e.type === 'radio') e.checked = (e.value === v);
        else if (e.type === 'checkbox') e.checked = v.indexOf(e.value) >= 0;
        else e.value = v;
        if (e.type !== 'text' && e.type !== 'number' && e.type !== 'textarea') e.dispatchEvent(new Event('change', {bubbles: true}));
      });
    });
  } catch (x) {}
  document.addEventListener('submit', function (ev) {
    var all = Array.prototype.slice.call(forms), i = all.indexOf(ev.target); if (i < 0) return;
    try {
      var out = {};
      field(ev.target).forEach(function (e) {
        if (e.type === 'radio') { if (e.checked) out[e.name] = e.value; }
        else if (e.type === 'checkbox') { (out[e.name] = out[e.name] || []); if (e.checked) out[e.name].push(e.value); }
        else out[e.name] = e.value;
      });
      var f = {}; f[i] = out; sessionStorage.setItem(K, JSON.stringify({t: Date.now(), f: f}));
    } catch (x) {}
  });
})();

// the application's own question window: a form with data-confirm="text" asks first (data-confirm-title, data-confirm-ok,
// data-confirm-danger; data-confirm-check="label" + data-confirm-check-name="field" adds a tick that is sent as field=1;
// with data-confirm-check-required the OK button stays off until the tick is set)
(function () {
  var dlg = document.getElementById('appconfirm'); if (!dlg) return;
  var title = dlg.querySelector('#ac-title'), text = dlg.querySelector('#ac-text'), yes = dlg.querySelector('#ac-yes'),
      no = dlg.querySelector('#ac-no'), box = dlg.querySelector('#ac-check'), tick = box.querySelector('input'), asking = null;
  document.addEventListener('submit', function (ev) {
    var f = ev.target; if (!f.hasAttribute || !f.hasAttribute('data-confirm') || f.__ok) return;
    ev.preventDefault();
    if (!dlg.showModal) { if (confirm(f.getAttribute('data-confirm'))) { f.__ok = true; f.submit(); } return; }  // very old browser
    asking = f;
    var btn = ev.submitter || f.querySelector('button:not([type=button])');  // title, label and colour default to the button's own
    var danger = f.hasAttribute('data-confirm-danger') || !!(btn && btn.classList.contains('danger'));
    title.textContent = f.getAttribute('data-confirm-title') || dlg.getAttribute('data-default-title');
    text.textContent = f.getAttribute('data-confirm');
    yes.textContent = f.getAttribute('data-confirm-ok') || (btn && btn.textContent.trim()) || 'OK';
    dlg.classList.toggle('danger', danger); yes.classList.toggle('danger-fill', danger);
    var label = f.getAttribute('data-confirm-check');
    box.hidden = !label; box.querySelector('span').textContent = label || ''; tick.checked = f.hasAttribute('data-confirm-check-on');
    yes.disabled = !!label && f.hasAttribute('data-confirm-check-required') && !tick.checked;
    dlg.showModal(); no.focus();
  }, true);
  tick.addEventListener('change', function () { if (asking && asking.hasAttribute('data-confirm-check-required')) yes.disabled = !tick.checked; });
  no.addEventListener('click', function () { asking = null; dlg.close(); });
  dlg.addEventListener('cancel', function () { asking = null; });
  yes.addEventListener('click', function () {
    var f = asking; asking = null; dlg.close(); if (!f) return;
    var name = f.getAttribute('data-confirm-check-name');
    if (name && !box.hidden && tick.checked) { var h = document.createElement('input'); h.type = 'hidden'; h.name = name; h.value = '1'; f.appendChild(h); }
    f.__ok = true;
    if (f.hasAttribute('data-confirm-event')) f.dispatchEvent(new CustomEvent('confirmed')); else f.submit();  // the page handles it itself (no reload)
  });
})();

// number boxes with − and + (macros.html, stepper): an empty box starts at the smallest sensible value
document.addEventListener('click', function (e) {
  var b = e.target.closest && e.target.closest('.stepper button'); if (!b) return;
  var i = b.parentNode.querySelector('input'), d = +b.dataset.step, v = i.value === '' ? NaN : +i.value,
      lo = i.min !== '' ? +i.min : -Infinity, hi = i.max !== '' ? +i.max : Infinity;
  if (isNaN(v)) { if (d < 0) return; v = Math.min(hi, Math.max(lo, 1)); } else v = Math.min(hi, Math.max(lo, v + d));
  i.value = v; i.dispatchEvent(new Event('input', {bubbles: true}));
});

// "Advanced permissions" block (_perm_fields.html): the printers and the days are shown only when their choice needs them
// (without scripts everything stays visible)
document.querySelectorAll('details.adv [data-show]').forEach(function (block) {
  var radios = document.querySelectorAll('details.adv input[name="' + block.dataset.show + '"]');
  function update() {
    var on = Array.prototype.some.call(radios, function (r) { return r.checked && r.value === block.dataset.value; });
    block.hidden = !on;
  }
  Array.prototype.forEach.call(radios, function (r) { r.addEventListener('change', update); });
  update();
});

// automatic refresh switch (autorefresh.html): data-ar-key (which page), data-ar-secs, data-ar-on ("1" = on at first)
document.querySelectorAll('.ar[data-ar-key]').forEach(function (box) {
  var btn = box.querySelector('button'), K = 'autorefresh:' + box.dataset.arKey, secs = parseInt(box.dataset.arSecs, 10) || 0, def = box.dataset.arOn === '1', timer = null;
  function get() { try { var v = localStorage.getItem(K); return v === null ? def : v === '1'; } catch (e) { return def; } }
  function put(v) { try { localStorage.setItem(K, v ? '1' : '0'); } catch (e) {} }
  var on = get();
  function paint() { btn.classList.toggle('on', on); btn.setAttribute('aria-checked', on ? 'true' : 'false'); }
  function tick() {
    var a = document.activeElement && document.activeElement.tagName;
    if (document.querySelector('dialog[open]') || /^(INPUT|SELECT|TEXTAREA)$/.test(a || '')) { timer = setTimeout(tick, 5000); return; }
    location.reload();
  }
  function arm() { clearTimeout(timer); if (on) timer = setTimeout(tick, secs * 1000); }
  btn.addEventListener('click', function () { on = !on; put(on); paint(); arm(); });
  paint(); arm();
});
