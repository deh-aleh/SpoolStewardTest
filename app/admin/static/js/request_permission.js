// Without JavaScript the printer list is always shown; the server reads it only when "a printer" is chosen.
function permKind() {
  var box = document.getElementById('printerbox');
  if (box) { box.hidden = document.forms.permform.perm.value !== 'printer'; }
}
// the form (and so the select) exists only when there is something to ask for
if (document.forms.permform && document.forms.permform.perm) {
  document.forms.permform.perm.addEventListener('change', permKind);
  permKind();
}
