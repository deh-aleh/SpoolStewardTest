function bk(){var m=document.querySelector('input[name=mode]:checked').value;
  ['passphrase','key','none'].forEach(function(k){document.getElementById('m-'+k).hidden=(k!==m)})}
document.querySelectorAll('input[name=mode]').forEach(function (r) { r.addEventListener('change', bk); });
bk();
