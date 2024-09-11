(function () {
    document.body.addEventListener("htmx:configRequest", (event) => {
        const csrf_header = document
            .querySelector("meta[name=djang-csrf-header-name]")
            .getAttribute("content");
        const csrf_token = document
            .querySelector("meta[name=djang-csrf-token]")
            .getAttribute("content");
        event.detail.headers[csrf_header] = csrf_token;

        let element = event.detail.elt.closest("[data-hx-state]");
        if (element) {
            event.detail.headers["X-Component-State"] = element.dataset.hxState;
        }
    });

    // WebSocket Management
    let sentComponents = new Set();

    function sendRemovedComponents(event) {
        let removedComponents = Array.from(sentComponents).filter(
            (id) => !document.getElementById(id),
        );
        removedComponents.forEach((id) => sentComponents.delete(id));
        if (removedComponents.length) {
            event.detail.socketWrapper.send(
                JSON.stringify({
                    type: "removed",
                    component_ids: removedComponents,
                }),
            );
        }
    }

    function sendAddedComponents(event) {
        let states = [];
        let subscriptions = new Map();
        let ids = new Set();

        Array.from(document.querySelectorAll("[data-hx-state]"))
            .filter((el) => !sentComponents.has(el.id))
            .forEach((element) => {
                let hxSubscriptions = element.dataset.hxSubscriptions;
                if (hxSubscriptions !== undefined) {
                    subscriptions[element.id] = element.dataset.hxSubscriptions;
                }
                states.push(element.dataset.hxState);
                ids.add(element.id);
            });
        ids.forEach((id) => sentComponents.add(id));

        if (ids.size) {
            event.detail.socketWrapper.send(
                JSON.stringify({
                    type: "added",
                    states,
                    subscriptions,
                }),
            );
        }
    }

    document.addEventListener("htmx:wsOpen", (event) => {
        console.log("OPEN", event);
        sentComponents.clear();
    });

    document.addEventListener("htmx:wsClose", (event) => {
        console.log("CLOSE", event);
        sentComponents.clear();
    });

    document.addEventListener("htmx:wsConfigSend", (event) => {
        // add indicator
        let indicatorSelector = event.detail.elt
            .closest("[hx-indicator]")
            ?.getAttribute("hx-indicator");
        if (indicatorSelector) {
            document
                .querySelectorAll(indicatorSelector)
                .forEach((el) => el.classList.add("htmx-request"));
        }

        // send current state
        sendRemovedComponents(event);
        sendAddedComponents(event);

        // enrich event message
        event.detail.headers["HX-Component-Id"] =
            event.detail.elt.closest("[data-hx-state]").id;
        event.detail.headers["HX-Component-Handler"] =
            event.detail.elt.getAttribute("ws-send");
    });

    document.addEventListener("htmx:wsBeforeMessage", (event) => {
        // remove indicator
        document
            .querySelectorAll(".htmx-request")
            .forEach((el) => el.classList.remove("htmx-request"));

        // process message
        if (event.detail.message.startsWith("{")) {
            let commandData = JSON.parse(event.detail.message);
            event.preventDefault();
            let { command } = commandData;
            switch (command) {
                case "destroy": {
                    let { component_id } = commandData;
                    document.getElementById(component_id)?.remove();
                    break;
                }
                case "focus": {
                    let { selector } = commandData;
                    document.querySelector(selector)?.focus();
                    break;
                }
                case "redirect": {
                    let { url } = commandData;
                    location.assign(url);
                    break;
                }
                case "dispatch_event": {
                    let = { target, detail, buubles, cancelable, composed } =
                        commandData;
                    document.querySelector(target)?.dispatchEvent(
                        new CustomEvent(event, {
                            detail,
                            buubles,
                            cancelable,
                            composed,
                        }),
                    );
                    break;
                }
                case "send_state": {
                    let { component_id, state } = commandData;
                    let component = document.getElementById(component_id);
                    if (component) {
                        component.dataset.hxState = state;
                    }
                    break;
                }
                case "push_url": {
                    let { url } = commandData;
                    history.pushState({}, document.title, url);
                    break;
                }

                default:
                    console.error(
                        "Can't process command:",
                        event.detail.message,
                    );
                    break;
            }
        }
    });

    document.addEventListener("hxFocus", (event) => {
        event.detail.value.map((selector) => {
            document.querySelector(selector).focus();
        });
    });
})();

function getStatesAndSubscriptions(elements) {
    let states = [];
    let subscriptions = new Map();
    let ids = new Set();
    if (elements === undefined) {
        elements = document.querySelectorAll("[data-hx-state]");
    }
    elements.forEach((element) => {
        let hxSubscriptions = element.dataset.hxSubscriptions;
        if (hxSubscriptions !== undefined) {
            subscriptions[element.id] = element.dataset.hxSubscriptions;
        }
        states.push(element.dataset.hxState);
        ids.add(element.id);
    });
    return { ids, states, subscriptions };
}

// Local Variables:
// js-indent-level: 4
// End:
