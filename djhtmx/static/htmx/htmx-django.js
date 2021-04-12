htmx.defineExtension('morphdom-swap', {
  isInlineSwap: function(swapStyle) {
    return swapStyle === 'morphdom';
  },
  handleSwap: function (swapStyle, target, fragment) {
    if (swapStyle === 'morphdom') {
      morphdom(
        target,
        fragment.outerHTML,
        {
          onBeforeElUpdated: (fromEl, toEl) => {
            if (fromEl.hasAttribute(':once')) {
              return false;
            }
            const tagName= fromEl.tagName;
            const shouldPacth = (
              fromEl === document.activeElement &&
              (tagName === 'INPUT' || tagName === 'SELECT' || tagName === 'TEXTAREA') &&
              !fromEl.hasAttribute(':override')
            )
            if (shouldPacth) {
              toEl.getAttributeNames().forEach((name) =>
                fromEl.setAttribute(name, toEl.getAttribute(name))
              );
              fromEl.readOnly = toEl.readOnly;
              return false;
            }
            return true;
          }
        }
      );
      return [target];
    }
  },
});


document.addEventListener('hxSendEvent', function (event) {
  event.detail.value.map(({event, target}) => {
    document.querySelector(target).dispatchEvent(new Event(event));
  });
});
