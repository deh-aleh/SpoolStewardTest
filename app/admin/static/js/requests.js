// "…" in the Reason column: the whole reason in a window (empty column = the user wrote none)
document.addEventListener('click', function (e) {
  var b = e.target.closest && e.target.closest('button.showreason'); if (!b) return;
  document.getElementById('reason-who').textContent = b.dataset.user + ' · ' + b.dataset.what;
  document.getElementById('reason-text').textContent = b.dataset.reason;
  document.getElementById('reason-dlg').showModal();
});
// the date field appears (in CSS) only for "until the end of…"; it is required only then
document.addEventListener('change', function (e) {
  var f = e.target.closest && e.target.closest('form.decide'); if (!f || !f.elements.until) return;
  f.elements.until.required = f.elements.duration.value === 'until';
});

// Without JavaScript every field is shown and the server reads only those of the chosen kind (and refuses a permission
// the user already has). With it, only what the chosen user lacks is offered, asked to the server for that user alone;
// if the answer does not come, everything is offered (the server still refuses what the user already has).
(function () {
  var f = document.forms.grantform, known = {}, everything = {
    perms: Array.prototype.map.call(f.perm.options, function (o) { return o.value; }),
    metrics: Array.prototype.map.call(f.metric.options, function (o) { return o.value; }),
    printers: Array.prototype.map.call(f.printer.options, function (o) { return o.value; })};
  function fill(select, values) {
    Array.prototype.forEach.call(select.options, function (o) { o.hidden = o.disabled = values.indexOf(o.value) < 0; });
    if (select.selectedOptions[0] && select.selectedOptions[0].disabled) {
      var first = Array.prototype.find.call(select.options, function (o) { return !o.disabled; });
      if (first) select.value = first.value;
    }
  }
  function update() {
    var kind = f.kind.value, l = known[f.username.value] || everything, none = !l.perms.length, noquota = !l.metrics.length;
    document.querySelectorAll('#grantform [data-kind]').forEach(function (el) { el.hidden = el.dataset.kind !== kind; });
    fill(f.perm, l.perms); fill(f.printer, l.printers); fill(f.metric, l.metrics);
    document.getElementById('grant-has-all').hidden = !none;
    document.getElementById('grant-perm').hidden = none;
    f.printer.hidden = f.perm.value !== 'printer';
    f.until.hidden = f.duration.value !== 'until';
    document.getElementById('grant-no-quota').hidden = !noquota;
    document.getElementById('grant-quota').hidden = noquota;
    f.amount.required = kind === 'quota' && !noquota;
    document.getElementById('grant-go').disabled = (kind === 'permission' && none) || (kind === 'quota' && noquota);
  }
  function ask() {
    var name = f.username.value;
    update();
    if (known[name]) return;
    fetch('/admin/requests/lacks?username=' + encodeURIComponent(name), {credentials: 'same-origin'})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (l) { if (l) { known[name] = l; if (f.username.value === name) update(); } })
      .catch(function () {});
  }
  f.username.addEventListener('change', ask);
  ['kind', 'perm', 'duration'].forEach(function (n) { f[n].addEventListener('change', update); });
  ask();
})();
