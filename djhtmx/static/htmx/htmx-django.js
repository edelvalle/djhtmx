document.addEventListener('hxSendEvent', function (event) {
  event.detail.value.map(({event, target}) => {
    document.querySelector(target).dispatchEvent(new Event(event));
  });
});


document.addEventListener('hxFocus', function (event) {
  event.detail.value.map((selector) => {
    document.querySelector(selector).focus();
  });
});
