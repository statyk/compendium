// Warn before leaving a page with unsaved edits in any form marked
// data-dirty-guard. Submitting a guarded form clears the flag so normal
// saves never warn.
(function () {
  var dirty = false;
  var guarded = document.querySelectorAll('form[data-dirty-guard]');
  if (!guarded.length) return;
  guarded.forEach(function (f) {
    f.addEventListener('input', function () { dirty = true; });
    f.addEventListener('change', function () { dirty = true; });
    f.addEventListener('submit', function () { dirty = false; });
  });
  window.addEventListener('beforeunload', function (e) {
    if (!dirty) return;
    e.preventDefault();
    e.returnValue = '';
  });
})();
