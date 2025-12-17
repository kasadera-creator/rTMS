// floating.js
function clickById(id){
  const el = document.getElementById(id);
  if (el) el.click();
  return !!el;
}

function submitFormById(id){
  const form = document.getElementById(id);
  if (!form) return false;
  if (typeof form.requestSubmit === 'function') form.requestSubmit();
  else form.submit();
  return true;
}

function openPrintForm(formId){
  const form = document.getElementById(formId);
  if (!form) return false;
  if (typeof form.requestSubmit === 'function') form.requestSubmit();
  else form.submit();
  return true;
}

window.RTMSFloating = { clickById, submitFormById, openPrintForm };
