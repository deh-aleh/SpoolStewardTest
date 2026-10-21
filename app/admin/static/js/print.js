// print.html
(function () {
  var D = JSON.parse(document.getElementById('pt-data').textContent);  // values from the server
  var f = document.getElementById('file'), go = document.getElementById('go'), n = document.getElementById('fname'),
      drop = document.getElementById('drop'), c = document.getElementById('copies'), max = D.max_copies;
  if (!f) return;
  var qs = D.queues, media = document.getElementById('media');
  function reset(name) { var r = document.querySelector('input[name=' + name + '][value=""]'); if (r) r.checked = true; }  // back to "printer default"
  function opts() {
    var r = document.querySelector('input[name=queue]:checked'), q = r && qs[r.value] || {media: [], color: false};
    media.innerHTML = '<option value="">' + D.t_default + '</option>';
    q.media.forEach(function (m) {
      var o = document.createElement('option'); o.value = m.id; o.textContent = m.label + (m.id === q.media_default ? ' ' + D.t_default_mark : '');
      media.appendChild(o);
    });
    var colorRow = document.getElementById('colorrow');
    if (colorRow) {
      colorRow.style.display = q.color ? '' : 'none';
      if (!q.color) reset('color');
    }
    var sidesRow = document.getElementById('sidesrow');
    if (sidesRow) {  // only a printer with a duplex unit offers the choice
      sidesRow.style.display = q.duplex ? '' : 'none';
      if (!q.duplex) reset('sides');
    }
  }
  Array.prototype.forEach.call(document.querySelectorAll('input[name=queue]'), function (r) { r.addEventListener('change', opts); });
  opts();
  function upd() { var x = f.files && f.files[0]; n.textContent = x ? x.name + ' (' + (x.size / 1048576).toFixed(1) + ' MB)' : ''; go.disabled = !x; }
  f.addEventListener('change', upd);
  ['dragenter', 'dragover'].forEach(function (e) { drop.addEventListener(e, function (ev) { ev.preventDefault(); drop.classList.add('over'); }); });
  ['dragleave', 'drop'].forEach(function (e) { drop.addEventListener(e, function (ev) { ev.preventDefault(); drop.classList.remove('over'); }); });
  drop.addEventListener('drop', function (ev) { if (ev.dataTransfer.files.length) { f.files = ev.dataTransfer.files; upd(); } });
  document.getElementById('pf').addEventListener('submit', function () { go.disabled = true; document.getElementById('golabel').textContent = D.t_sending; });
  upd();
})();
