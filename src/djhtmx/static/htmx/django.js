(() => {
	// WebSocket Management
	const sentComponents = new Set();
	const observedSSECommandSinks = new WeakSet();

	function sendRemovedComponents(event) {
		const removedComponents = Array.from(sentComponents).filter(
			(id) => !document.getElementById(id),
		);
		for (const id of removedComponents) {
			sentComponents.delete(id);
		}
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
		const states = [];
		const subscriptions = new Map();
		const ids = new Set();

		for (const element of Array.from(
			document.querySelectorAll("[data-hx-state]"),
		).filter((el) => !sentComponents.has(el.id))) {
			const hxSubscriptions = element.dataset.hxSubscriptions;
			if (hxSubscriptions !== undefined) {
				subscriptions[element.id] = element.dataset.hxSubscriptions;
			}
			states.push(element.dataset.hxState);
			ids.add(element.id);
		}
		for (const id of ids) {
			sentComponents.add(id);
		}

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

	function removeHtmxIndicator() {
		// remove indicator
		for (const el of document.querySelectorAll(".htmx-request")) {
			el.classList.remove("htmx-request");
		}
	}

	function installSSECommandProcessors() {
		for (const sink of document.querySelectorAll("[data-djhtmx-sse-command-sink]")) {
			if (!observedSSECommandSinks.has(sink)) {
				observedSSECommandSinks.add(sink);
				const session = sink.dataset.djhtmxSseCommandSink;
				const observer = new MutationObserver((records) => {
					for (const record of records) {
						for (const node of record.addedNodes) {
							processSSECommandNode(node, sink, session);
						}
					}
				});
				observer.observe(sink, { childList: true });
				for (const child of sink.children) {
					processSSECommandNode(child, sink, session);
				}
			}
		}
	}

	function processSSECommandNode(node, sink, session) {
		if (node instanceof HTMLElement) {
			const elements = [
				...(node.matches("[data-command]") ? [node] : []),
				...node.querySelectorAll("[data-command]"),
			];
			for (const element of elements) {
				if (sink.contains(element)) {
					processSSECommandElement(element, session);
				}
			}
		}
	}

	function processSSECommandElement(element, session) {
		if (element.dataset.session !== session) {
			console.error("Ignoring SSE command for the wrong session");
			element.remove();
			return;
		}

		const command = element.dataset.command;
		if (command === "open") {
			processSSEOpenCommand(element);
		} else {
			console.error("Unknown SSE command:", command);
		}
		element.remove();
	}

	function processSSEOpenCommand(element) {
		const rawUrl = element.dataset.url;
		if (rawUrl) {
			let url;
			try {
				url = new URL(rawUrl, window.location.href);
			} catch {
				console.error("Ignoring SSE open command with invalid URL:", rawUrl);
				return;
			}

			if (url.origin === window.location.origin) {
				const target = element.dataset.target || "_blank";
				if (["_blank", "_self", "_parent", "_top"].includes(target)) {
					openURL({
						url: url.href,
						name: element.dataset.name,
						target,
						rel: element.dataset.rel,
					});
				} else {
					console.error("Ignoring SSE open command with invalid target:", target);
				}
			} else {
				console.error("Ignoring cross-origin SSE open command:", url.href);
			}
		}
	}

	function openURL({ url, name, target, rel }) {
		const link = document.createElement("a");
		link.href = url;
		link.target = target || "_blank";
		if (name) {
			link.download = name;
		}
		link.rel = rel || "noopener noreferrer";
		link.click();
	}

	document.addEventListener("DOMContentLoaded", installSSECommandProcessors);
	document.addEventListener("htmx:load", installSSECommandProcessors);

	document.addEventListener("htmx:wsOpen", (event) => {
		console.log("OPEN", event);
		sentComponents.clear();
		removeHtmxIndicator();
	});

	document.addEventListener("htmx:wsClose", (event) => {
		console.log("CLOSE", event);
		sentComponents.clear();
		removeHtmxIndicator();
	});

	document.addEventListener("htmx:wsConfigSend", (event) => {
		// add indicator
		const indicatorSelector = event.detail.elt
			.closest("[hx-indicator]")
			?.getAttribute("hx-indicator");
		if (indicatorSelector) {
			for (const el of document.querySelectorAll(indicatorSelector)) {
				el.classList.add("htmx-request");
			}
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
		removeHtmxIndicator();

		// process message
		if (event.detail.message.startsWith("{")) {
			const commandData = JSON.parse(event.detail.message);
			event.preventDefault();
			const { command } = commandData;
			switch (command) {
				case "destroy": {
					const { component_id } = commandData;
					document.getElementById(component_id)?.remove();
					break;
				}
				case "focus": {
					const { selector } = commandData;
					document.querySelector(selector)?.focus();
					break;
				}
				case "scroll_into_view": {
					const { selector, behavior = "smooth", block = "center", if_not_visible = false } = commandData;
					const element = document.querySelector(selector);
					if (element) {
						const should_scroll = !if_not_visible || (({ top, left, bottom, right }) =>
							top < 0 ||
							left < 0 ||
							bottom > (window.innerHeight || document.documentElement.clientHeight) ||
							right > (window.innerWidth || document.documentElement.clientWidth)
						)(element.getBoundingClientRect());
						if (should_scroll) {
							element.scrollIntoView({ behavior, block });
						}
					}
					break;
				}
				case "redirect": {
					const { url } = commandData;
					location.assign(url);
					break;
				}
				case "dispatch_event": {
					const { target, detail, buubles, cancelable, composed } = commandData;
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
					const { component_id, state } = commandData;
					const component = document.getElementById(component_id);
					if (component) {
						component.dataset.hxState = state;
					}
					break;
				}
				case "push_url": {
					const { url } = commandData;
					history.pushState({}, document.title, url);
					break;
				}

				default:
					console.error("Can't process command:", event.detail.message);
					break;
			}
		}
	});

	document.addEventListener("hxDispatchDOMEvent", (event) => {
		for (const {
			event: eventName,
			target,
			detail,
			bubbles,
			cancelable,
			composed,
		} of event.detail.value) {
			const el = document.querySelector(target);
			if (el) {
				// This setTimeout basically queues the dispatch of the event
				// to avoid dispatching events within events handlers.
				setTimeout(
					() =>
						el.dispatchEvent(
							new CustomEvent(eventName, {
								detail,
								bubbles,
								cancelable,
								composed,
							}),
						),
					0,
				);
			}
		}
	});

	document.addEventListener("hxFocus", (event) => {
		for (const selector of event.detail.value) {
			document.querySelector(selector).focus();
		}
	});

	document.addEventListener("hxScrollIntoView", (event) => {
		for (const item of event.detail.value) {
			const selector = typeof item === "string" ? item : item.selector;
			const behavior = typeof item === "object" ? item.behavior || "smooth" : "smooth";
			const block = typeof item === "object" ? item.block || "center" : "center";
			const if_not_visible = typeof item === "object" ? item.if_not_visible || false : false;
			const element = document.querySelector(selector);
			if (element) {
				const should_scroll = !if_not_visible || (({ top, left, bottom, right }) =>
					top < 0 ||
					left < 0 ||
					bottom > (window.innerHeight || document.documentElement.clientHeight) ||
					right > (window.innerWidth || document.documentElement.clientWidth)
				)(element.getBoundingClientRect());
				if (should_scroll) {
					element.scrollIntoView({ behavior, block });
				}
			}
		}
	});

	document.addEventListener("hxOpenURL", (event) => {
		for (const { url, name, target, rel } of event.detail.value) {
			openURL({ url, name, target, rel });
		}
	});
})();
// Local Variables:
// js-indent-level: 4
// End:
