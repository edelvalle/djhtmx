const DEFAULT_MORPHDOM_OPTIONS = {
  onElUpdated: (el) => {
    // Executes JS hooks
    code = typeof el.getAttribute === "function" ? el.getAttribute('hx-updated') : void 0;
    if (code) (() => { eval(code) }).bind(el)();
  },

  onNodeAdded: (el) => {
    // Executes JS hooks
    code = typeof el.getAttribute === "function" ? el.getAttribute('hx-added') : void 0;
    if (code) (() => { eval(code) }).bind(el)();
  },
};

const INTERACTIVE_MORHDOM_OPTIONS = Object.assign({}, DEFAULT_MORPHDOM_OPTIONS, {
   onBeforeElUpdated: (fromEl, toEl) => {
    // Keeps the focus on an input if it was focused before replacement
    if (fromEl.hasAttribute && fromEl.hasAttribute('hx-disabled')) {
      return false;
    }

    const tagName= fromEl.tagName;
    const shouldPatch = (
      fromEl === document.activeElement &&
      (tagName === 'INPUT' || tagName === 'SELECT' || tagName === 'TEXTAREA') &&
      !fromEl.hasAttribute('hx-override')
    )
    if (shouldPatch) {
      toEl.getAttributeNames().forEach((name) =>
        fromEl.setAttribute(name, toEl.getAttribute(name))
      );
      fromEl.readOnly = toEl.readOnly;
      return false;
    }

    return true;
  },
});


htmx.defineExtension('morphdom-swap', {
  isInlineSwap: function(swapStyle) {
    return swapStyle === 'morphdom';
  },
  handleSwap: function (swapStyle, target, fragment) {
    if (swapStyle === 'morphdom') {
      if (target.nodeName === 'BODY') {
        morphdom(target, fragment, DEFAULT_MORPHDOM_OPTIONS);
      } else {
        morphdom(target, fragment.outerHTML, INTERACTIVE_MORHDOM_OPTIONS);
      }
      return [target];
    }
  },
});


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
