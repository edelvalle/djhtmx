// Scroll behaviour for the agent chat.
//
// Two states, held on the scroller as `data-scroll-mode`:
//
//   auto   the scroller is pinned to the newest message and follows it as the
//          reply streams in.
//   fixed  the scroller stays where the reader left it, and a floating button
//          offers a way back.
//
// Only a manual scroll enters `fixed`; only the button or sending a message
// leaves it.  Growing content never fires a scroll event, so any scroll away
// from the bottom that we did not cause ourselves is the reader's.
(() => {
  // Pixels of slack before the scroller counts as away from the bottom.  A
  // fractional scrollTop (zoom, sub-pixel layout) never quite reaches 0.
  const SLACK = 8;

  const atBottom = (scroller) =>
    scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop <= SLACK;

  const canScroll = (scroller) => scroller.scrollHeight - scroller.clientHeight > SLACK;

  const followButton = (scroller) =>
    scroller.closest(".agent-chat-viewport")?.querySelector(".agent-chat-follow");

  // The button is an offer to catch up, so it is worth showing only while the
  // reader has held the scroller still *and* there is something below the fold.
  function syncButton(scroller) {
    const button = followButton(scroller);
    if (button) {
      button.hidden = scroller.dataset.scrollMode !== "fixed" || !canScroll(scroller);
    }
  }

  function setMode(scroller, mode) {
    scroller.dataset.scrollMode = mode;
    syncButton(scroller);
  }

  function follow(scroller) {
    // Instant, not smooth: a smooth scroll keeps firing scroll events after the
    // guard below is lifted, and one of those would look like a manual scroll.
    scroller.dataset.scrolling = "1";
    scroller.scrollTop = scroller.scrollHeight;
    requestAnimationFrame(() => delete scroller.dataset.scrolling);
  }

  function resume(panel) {
    const scroller = panel?.querySelector(".agent-chat-scroll");
    if (scroller) {
      setMode(scroller, "auto");
      follow(scroller);
    }
  }

  function init(scroller) {
    if (scroller.dataset.scrollMode) {
      return; // already wired; htmx re-runs this for every swapped fragment
    }
    setMode(scroller, "auto");
    follow(scroller);

    scroller.addEventListener(
      "scroll",
      () => {
        if (!scroller.dataset.scrolling && !atBottom(scroller)) {
          setMode(scroller, "fixed");
        }
      },
      { passive: true },
    );

    new MutationObserver(() => {
      if (scroller.dataset.scrollMode === "auto") {
        follow(scroller);
      }
      // Re-checked on every change: a reply streaming in can push content out of
      // sight, and a cleared conversation can pull it back.
      syncButton(scroller);
    }).observe(scroller, { childList: true, subtree: true, characterData: true });
  }

  function initAll(root) {
    if (root.matches?.(".agent-chat-scroll")) {
      init(root);
    }
    for (const scroller of root.querySelectorAll?.(".agent-chat-scroll") ?? []) {
      init(scroller);
    }
  }

  document.addEventListener("DOMContentLoaded", () => initAll(document));
  // htmx events bubble to document, so this catches the panel arriving in any
  // fragment as well as a full page load.
  document.addEventListener("htmx:load", (event) => initAll(event.target));

  document.addEventListener("click", (event) => {
    if (event.target.closest?.(".agent-chat-follow")) {
      resume(event.target.closest(".agent-chat"));
    }
  });

  // Sending empties the box and resumes following.  The box is not re-rendered
  // (the panel is left alone so the scroller survives), so it has to be cleared
  // here rather than server-side.
  document.addEventListener("htmx:afterRequest", (event) => {
    const form = event.target.closest?.(".agent-chat-form");
    if (form && event.detail?.successful) {
      const input = form.querySelector("input[name=prompt]");
      if (input) {
        input.value = "";
      }
      resume(form.closest(".agent-chat"));
    }
  });
})();
