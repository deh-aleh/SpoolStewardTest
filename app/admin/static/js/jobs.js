// the "ⓘ" and "…" buttons open the window they belong to
document.querySelectorAll('[data-open-id]').forEach(function (b) {
  b.addEventListener('click', function () { document.getElementById(b.dataset.openId).showModal(); });
});
document.querySelectorAll('[data-open-next]').forEach(function (b) {
  b.addEventListener('click', function () { b.nextElementSibling.showModal(); });
});
document.querySelectorAll('[data-open-td]').forEach(function (b) {
  b.addEventListener('click', function () { b.closest('td').querySelector('dialog').showModal(); });
});
