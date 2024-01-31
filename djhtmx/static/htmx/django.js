(function () {
    document.body.addEventListener("htmx:afterSwap", (event) => {
        let hxState = event.detail.xhr.getResponseHeader("HX-State");
        if (hxState) {
            let { component_id, state } = JSON.parse(hxState);
            let element = document.getElementById(component_id);
            if (element) element.dataset.hxState = state;
        }
    });

    document.body.addEventListener("htmx:beforeRequest", (event) => {
        let subscriptions = {};
        let states = [];
        document.querySelectorAll("[data-hx-state]").forEach((element) => {
            let hxSubscriptions = element.dataset.hxSubscriptions;
            if (hxSubscriptions !== undefined) {
                subscriptions[element.id] = element.dataset.hxSubscriptions;
            }
            states.push(element.dataset.hxState);
        });
        event.detail.requestConfig.unfilteredParameters["__hx-states__"] =
            states;
        event.detail.requestConfig.unfilteredParameters[
            "__hx-subscriptions__"
        ] = subscriptions;
    });

    document.body.addEventListener("htmx:configRequest", (event) => {
        const csrf_header = document
            .querySelector("meta[name=djang-csrf-header-name]")
            .getAttribute("content");
        const csrf_token = document
            .querySelector("meta[name=djang-csrf-token]")
            .getAttribute("content");
        event.detail.headers[csrf_header] = csrf_token;
    });

    document.addEventListener("hxDispatchEvent", (event) => {
        event.detail.value.map(({ event, target }) => {
            document.querySelector(target).dispatchEvent(new Event(event));
        });
    });

    document.addEventListener("hxFocus", (event) => {
        event.detail.value.map((selector) => {
            document.querySelector(selector).focus();
        });
    });
})();
