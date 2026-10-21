// printers.html: values from the server arrive in the JSON block #pr-data
var PR = JSON.parse(document.getElementById('pr-data').textContent);
// names for the rename hints: the queues there are now, and the ones removed (with the day)
var QUEUE_NAMES = PR.queues;
var FORMER = PR.former;  // most recent first
var T_FORMER = PR.t_former;
var T_TAKEN = PR.t_taken;
var T_SAME = PR.t_same;
var T_BAD = PR.t_bad;
// ink bars: the level and the colour come as data attributes (no inline style)
document.querySelectorAll('.bar > i[data-w]').forEach(function (i) {
  i.style.width = i.dataset.w + '%'; i.style.background = i.dataset.bg;
});
document.addEventListener('click', function (e) {  // buttons that open a dialog of their card
  var o = e.target.closest && e.target.closest('[data-open-dialog]');
  if (o) document.getElementById(o.dataset.openDialog).showModal();
});
(function () {
  var els = document.querySelectorAll('.countdown');
  if (!els.length) return;
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function tick() {
    els.forEach(function (el) {
      var s = Math.max(0, Math.floor((new Date(el.dataset.end) - Date.now()) / 1000));
      el.textContent = Math.floor(s / 3600) + ':' + pad(Math.floor(s % 3600 / 60)) + ':' + pad(s % 60);
      if (s === 0) setTimeout(function () { location.reload(); }, 40000);
    });
  }
  tick(); setInterval(tick, 1000);
})();
document.addEventListener('confirmed', function (e) {  // "x" next to a name used before: forgotten without reloading the page
  var f = e.target; if (!f.classList || !f.classList.contains('forgetform')) return;
  var name = f.elements.name.value;
  fetch(f.action, {method: 'POST', headers: {'X-Fetch': '1', 'Content-Type': 'application/x-www-form-urlencoded'}, body: new URLSearchParams(new FormData(f))})
    .then(function (r) { if (!r.ok) throw 0; return r.json(); })
    .then(function () {
      FORMER = FORMER.filter(function (x) { return x[0] !== name; });
      document.querySelectorAll('form.forgetform').forEach(function (g) {  // every queue has its own rename window with the same list
        if (g.elements.name.value !== name) return;
        var item = g.closest('li'), details = item.closest('details'); item.remove();
        var left = details.querySelectorAll('li').length;
        if (left) details.querySelector('.former-count').textContent = left; else details.remove();
      });
      document.querySelectorAll('form.rnform input[name=new_name]').forEach(renameHint);
    })
    .catch(function () { f.submit(); });  // anything wrong: the ordinary way, with its message
}, true);
document.addEventListener('click', function (e) {
  var qa = e.target.closest && e.target.closest('button.qaddr');  // "..." next to a queue: its address, ready to copy
  if (qa) { var dlg = document.getElementById('qaddr-dlg'); dlg.querySelector('[data-copy]').textContent = qa.dataset.url; dlg.querySelector('#qaddr-name').textContent = qa.dataset.queue; dlg.showModal(); return; }
  var n = e.target.closest && e.target.closest('button.former');  // a name used before: written in the field last used in that dialog
  if (n) {
    var dlg = n.closest('dialog'), r = dlg.rnField || dlg.querySelector('input[name=new_name]');
    r.value = n.dataset.name; renameHint(r); r.focus(); return;
  }
  var b = e.target.closest && e.target.closest('button.chip'); if (!b) return;
  var f = b.closest('form'); f.elements.amount.value = b.dataset.amount; f.elements.unit.value = b.dataset.unit;
});
function renameHint(input) {
  // While a new name is typed: the rest of a name used before that starts the same way appears in grey inside the field
  // (only a reminder: the whole name must still be typed), and the line under it says if the name is one of the past (and
  // since when) or the name of a queue that exists. A name that exists, or the current one, can not be sent.
  var form = input.form, hint = form.querySelector('.rnhint'), ghost = form.querySelector('.ghost'), button = form.querySelector('button');
  var typed = input.value.trim(), v = typed.toLowerCase(), own = input.placeholder.toLowerCase();
  var past = FORMER.filter(function (f) { return f[0].toLowerCase() === v; })[0];
  var bad = typed && !/^[A-Za-z0-9._@+\-]{1,127}$/.test(typed), taken = v !== own && QUEUE_NAMES.indexOf(v) >= 0;
  hint.className = 'rnhint' + (bad || taken ? ' err' : ' muted');
  hint.textContent = bad ? T_BAD : v === own ? T_SAME : taken ? T_TAKEN : past ? T_FORMER.replace('DATE', past[1]) : '';
  button.disabled = !typed || bad || taken || v === own;
  var guess = v && !bad && input.value === typed && FORMER.filter(function (f) {
    var l = f[0].toLowerCase(); return l.length > v.length && l.indexOf(v) === 0; })[0];
  ghost.textContent = '';
  if (guess && input.scrollWidth <= input.clientWidth) {
    var hidden = document.createElement('span'); hidden.className = 'typed'; hidden.textContent = input.value;
    ghost.appendChild(hidden); ghost.appendChild(document.createTextNode(guess[0].slice(typed.length)));
  }
}
document.querySelectorAll('form.rnform input[name=new_name]').forEach(renameHint);
document.addEventListener('input', function (e) {
  if (e.target.matches && e.target.matches('form.rnform input[name=new_name]')) renameHint(e.target);
});
document.addEventListener('focusin', function (e) {
  if (e.target.matches && e.target.matches('form.rnform input[name=new_name]')) e.target.closest('dialog').rnField = e.target;
});
