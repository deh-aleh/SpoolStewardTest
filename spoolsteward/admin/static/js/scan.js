// scan.html
(function () {
  var f = document.getElementById('sf');
  if (!f) return;
  var D = JSON.parse(document.getElementById('sc-data').textContent);  // values from the server
  var all = D.caps;  // scanner id -> what that device reported about itself
  var sc = document.getElementById('scanner');
  var src = document.getElementById('source'), dpi = document.getElementById('dpi'), note = document.getElementById('dpinote');
  var name = document.getElementById('filename'), info = document.getElementById('scaninfo');
  var go = document.getElementById('go'), wait = document.getElementById('wait'), where = document.getElementById('where');
  var L = D.L;  // texts for the script, in the language of the page
  function fmtL(s, v) { return s.replace(/%\((\w+)\)s/g, function (m, k) { return v[k]; }); }
  var canPick = !!window.showSaveFilePicker;
  if (canPick) where.textContent = L.pick;
  function cur() { return all[sc.value] || {}; }
  function val(n) { var c = f.querySelector('input[name=' + n + ']:checked'); return c ? c.value : ''; }
  function segFill(box, name, opts, keep) {  // the choice is a row of buttons, built from what this scanner offers
    box.innerHTML = '';
    opts.forEach(function (o) {
      var l = document.createElement('label'), i = document.createElement('input'), t = document.createElement('span');
      i.type = 'radio'; i.name = name; i.value = o[0]; i.checked = String(o[0]) === String(keep); t.textContent = o[1];
      l.appendChild(i); l.appendChild(t); box.appendChild(l);
    });
    if (box.firstChild && !box.querySelector('input:checked')) box.querySelector('input').checked = true;
  }
  function dpis(which) {
    var c = cur();
    if (which === 'auto') { var a = (c.platen || {}).dpi || [], b = (c.adf || {}).dpi || []; var both = a.filter(function (x) { return b.indexOf(x) >= 0; }); return both.length ? both : a; }
    return (c[which] || {}).dpi || [];
  }
  function sources() {  // only what this scanner really has
    var c = cur(), keep = val('source');
    var opts = [];
    if (c.adf) opts.push(['auto', L.src_auto]);
    if (c.platen) opts.push(['platen', L.src_platen]);
    if (c.adf) opts.push(['adf', L.src_adf]);
    segFill(src, 'source', opts, keep);
    // what this scanner says about itself (nothing is assumed: a missing value is simply left out)
    var t = [], p = c.platen || {}, a = c.adf || {};
    var size = p.size || a.size; if (size) t.push(fmtL(L.max_size, {size: size}));
    if (c.adf) t.push((a.duplex ? L.adf_duplex : L.adf_simplex) + (a.dpi && a.dpi.length ? fmtL(L.up_to, {n: Math.max.apply(null, a.dpi)}) : '') + '.');
    else t.push(L.no_adf);
    if (p.dpi && p.dpi.length) t.push(fmtL(L.glass_dpi, {n: Math.max.apply(null, p.dpi)}));
    info.textContent = t.join(' ');
  }
  function fill() {
    var keep = +val('dpi') || 300, list = dpis(val('source'));
    var pick = list.indexOf(keep) >= 0 ? keep : list.indexOf(300) >= 0 ? 300 : list[0];
    segFill(dpi, 'dpi', list.map(function (r) { return [r, r + ' dpi' + (r === 300 ? ' ' + L.recommended : '')]; }), pick);
    note.textContent = val('source') === 'auto' && list.length ? fmtL(L.auto_dpi, {n: Math.max.apply(null, list)}) : '';
  }
  sc.addEventListener('change', function () { sources(); fill(); });
  sources();
  src.addEventListener('change', fill); fill();
  function ext() { return val('fmt') === 'pdf' ? 'pdf' : 'jpg'; }
  function finish() { go.disabled = false; wait.style.display = 'none'; }
  if (running_now()) { go.disabled = true; go.title = L.busy; }
  function running_now() { return D.running > 0; }
  var running = D.running;
  if (running && !window.__pending) setTimeout(function () { location.reload(); }, 3000);  // come back later: still there
  function save(sid, handle, label) {
    return fetch('/me/scan/' + sid + '/file', {credentials: 'same-origin'}).then(function (r) { return r.blob().then(function (blob) {
      var m = /filename\*=UTF-8''([^;]+)/.exec(r.headers.get('Content-Disposition') || ''), nm = m ? decodeURIComponent(m[1]) : L.default_name;
      if (handle) return handle.createWritable().then(function (w) { return w.write(blob).then(function () { return w.close(); }); })
        .then(function () { where.textContent = fmtL(L.saved, {name: handle.name}); });
      var a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = nm; document.body.appendChild(a); a.click(); a.remove();
      where.textContent = fmtL(L.downloaded, {name: nm});
    }); });
  }
  function poll(sid, handle) {
    fetch('/me/scan/' + sid, {credentials: 'same-origin'}).then(function (r) { return r.json(); }).then(function (j) {
      if (j.state === 'running') return setTimeout(function () { poll(sid, handle); }, 2000);
      window.__pending = false;
      if (j.state === 'done') return save(sid, handle).then(function () { location.reload(); });
      location.reload();  // the error message is shown by the page below
    }).catch(function () { setTimeout(function () { poll(sid, handle); }, 4000); });
  }
  var W = D.W;
  // scan window: the button tells the server "my scan app is about to scan"; the popup shows the address it saw
  var wb = document.getElementById('winbtn'), wd = document.getElementById('windlg'), wt = null, wend = 0;
  function wtick() {
    var left = Math.max(0, Math.round((wend - Date.now()) / 1000));
    document.getElementById('wincount').textContent = left > 0 ? fmtL(W.left, {m: Math.floor(left / 60), s: left % 60}) : W.over;
    if (left <= 0) { clearInterval(wt); wt = null; var dot = document.getElementById('windot'); if (dot) dot.remove(); }
  }
  function wpost(url) {
    return fetch(url, {method: 'POST', body: new URLSearchParams({csrf_token: f.querySelector('[name=csrf_token]').value}), credentials: 'same-origin'})
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || L.error); return j; }); });
  }
  if (W && wb && wd) {
    wb.addEventListener('click', function () {
      wpost('/me/scan/window').then(function (j) {
        document.getElementById('winip').textContent = j.ip;
        var n = document.getElementById('winnote'), msg = j.proxy ? W.proxy : j.ipv6 ? W.v6 : '';
        n.textContent = msg; n.style.display = msg ? 'block' : 'none';
        wend = Date.now() + j.seconds * 1000; wtick(); if (wt) clearInterval(wt); wt = setInterval(wtick, 1000);
        if (!document.getElementById('windot')) { var d = document.createElement('span'); d.className = 'dot'; d.id = 'windot'; wb.appendChild(d); }
        if (!wd.open) wd.showModal();
      }).catch(function (x) { where.textContent = x.message; });
    });
    document.getElementById('winok').addEventListener('click', function () { wd.close(); });
    document.getElementById('winstop').addEventListener('click', function () {
      wpost('/me/scan/window/close').then(function () { wend = 0; wtick(); var dot = document.getElementById('windot'); if (dot) dot.remove(); wd.close(); }).catch(function () { wd.close(); });
    });
  }
  f.addEventListener('submit', async function (ev) {
    if (!window.fetch) return;  // old browser: plain form post, the result is listed below
    ev.preventDefault();
    go.disabled = true; wait.style.display = 'block';
    var handle = null;
    if (canPick) {  // must be asked now: the permission is tied to this click, the scan takes much longer
      try {
        var e = ext();
        handle = await window.showSaveFilePicker({suggestedName: (name.value || L.default_name) + '.' + e,
          types: [{description: e.toUpperCase(), accept: e === 'pdf' ? {'application/pdf': ['.pdf']} : {'image/jpeg': ['.jpg'], 'application/zip': ['.zip']}}]});
      } catch (x) { finish(); return; }  // user closed the dialog
    }
    try {
      var r = await fetch('/me/scan', {method: 'POST', body: new URLSearchParams(new FormData(f)), headers: {'X-Requested-With': 'fetch'}, credentials: 'same-origin'});
      var j = await r.json();
      if (!r.ok) { where.textContent = j.error || L.error; finish(); return; }
      window.__pending = true;
      poll(j.id, handle);
    } catch (x) { where.textContent = L.error + ': ' + x; finish(); }
  });
})();
