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

	// ------------------------------------------------------------------
	// Unified browser command executor.
	//
	// Three transports feed this function:
	// - SSE: payload decoded from the session-scoped command sink.
	// - HTMX triggers (HX-Trigger-After-Settle): per-event-name handlers
	//   below convert event detail into command payloads.
	// - WebSocket: JSON command messages.
	//
	// `commandData.command` is the discriminator; other fields depend
	// on the command type.
	// ------------------------------------------------------------------
	function executeBrowserCommand(commandData) {
		switch (commandData.command) {
			case "open-tab": {
				const { url: rawUrl, name, target = "_blank", rel } = commandData;
				let url;
				try {
					url = new URL(rawUrl, window.location.href);
				} catch {
					console.error("Ignoring open-tab with invalid URL:", rawUrl);
					return;
				}
				if (url.origin !== window.location.origin) {
					console.error("Ignoring cross-origin open-tab:", url.href);
					return;
				}
				if (!["_blank", "_self", "_parent", "_top"].includes(target)) {
					console.error("Ignoring open-tab with invalid target:", target);
					return;
				}
				openURL({ url: url.href, name, target, rel });
				break;
			}
			case "focus": {
				const { selector } = commandData;
				document.querySelector(selector)?.focus();
				break;
			}
			case "scroll_into_view": {
				const {
					selector,
					behavior = "smooth",
					block = "center",
					if_not_visible = false,
				} = commandData;
				const element = document.querySelector(selector);
				if (element) {
					const should_scroll =
						!if_not_visible ||
						(({ top, left, bottom, right }) =>
							top < 0 ||
							left < 0 ||
							bottom > (window.innerHeight || document.documentElement.clientHeight) ||
							right > (window.innerWidth || document.documentElement.clientWidth))(
							element.getBoundingClientRect(),
						);
					if (should_scroll) {
						element.scrollIntoView({ behavior, block });
					}
				}
				break;
			}
			case "redirect": {
				let url;
				try {
					url = new URL(commandData.url, window.location.href);
				} catch {
					console.error("Ignoring redirect with invalid URL:", commandData.url);
					return;
				}
				location.assign(url.href);
				break;
			}
			case "push_url": {
				history.pushState({}, document.title, commandData.url);
				break;
			}
			case "replace_url": {
				history.replaceState({}, document.title, commandData.url);
				break;
			}
			case "dispatch_dom_event": {
				const {
					target,
					event,
					detail,
					bubbles = false,
					cancelable = false,
					composed = false,
				} = commandData;
				const el = document.querySelector(target);
				if (el) {
					el.dispatchEvent(
						new CustomEvent(event, { detail, bubbles, cancelable, composed }),
					);
				}
				break;
			}
			case "destroy": {
				const { component_id } = commandData;
				document.getElementById(component_id)?.remove();
				break;
			}
			default:
				console.error("Unknown browser command:", commandData.command, commandData);
		}
	}

	// ------------------------------------------------------------------
	// SSE command sink processing.
	//
	// `SSEEventRouter` renders a hidden, session-scoped `<div>` that
	// HTMX OOB-swaps `<template data-djhtmx-browser-command ...>` nodes
	// into.  A MutationObserver picks them up, decodes the payload, and
	// dispatches to `executeBrowserCommand`.
	// ------------------------------------------------------------------
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
				...(node.matches("[data-djhtmx-browser-command]") ? [node] : []),
				...node.querySelectorAll("[data-djhtmx-browser-command]"),
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
		const payload = element.dataset.payload;
		if (!payload) {
			element.remove();
			return;
		}
		let commandData;
		try {
			const padded = payload + "=".repeat((4 - (payload.length % 4)) % 4);
			const b64 = padded.replaceAll("-", "+").replaceAll("_", "/");
			commandData = JSON.parse(atob(b64));
		} catch (e) {
			console.error("Invalid SSE command payload:", e, payload);
			element.remove();
			return;
		}
		executeBrowserCommand(commandData);
		element.remove();
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
		if (!event.detail.message.startsWith("{")) {
			return;
		}
		event.preventDefault();
		const commandData = JSON.parse(event.detail.message);
		// Legacy WS used `dispatch_event`; normalize to `dispatch_dom_event`.
		if (commandData.command === "dispatch_event") {
			commandData.command = "dispatch_dom_event";
		}
		if (commandData.command === "send_state") {
			// WS-only: not a browser command, mutate the component state buffer.
			const { component_id, state } = commandData;
			const component = document.getElementById(component_id);
			if (component) {
				component.dataset.hxState = state;
			}
			return;
		}
		executeBrowserCommand(commandData);
	});

	// HTMX HX-Trigger-After-Settle event handlers.  These carry browser
	// commands sent from the HTTP path; each handler converts the event
	// payload to a `commandData` object and feeds it to the unified
	// `executeBrowserCommand`.
	document.addEventListener("hxDispatchDOMEvent", (event) => {
		for (const {
			event: eventName,
			target,
			detail,
			bubbles,
			cancelable,
			composed,
		} of event.detail.value) {
			// queue the dispatch to avoid firing events inside event handlers
			setTimeout(
				() =>
					executeBrowserCommand({
						command: "dispatch_dom_event",
						target,
						event: eventName,
						detail,
						bubbles,
						cancelable,
						composed,
					}),
				0,
			);
		}
	});

	document.addEventListener("hxFocus", (event) => {
		for (const selector of event.detail.value) {
			executeBrowserCommand({ command: "focus", selector });
		}
	});

	document.addEventListener("hxScrollIntoView", (event) => {
		for (const item of event.detail.value) {
			const payload =
				typeof item === "string" ? { selector: item } : { ...item };
			executeBrowserCommand({ command: "scroll_into_view", ...payload });
		}
	});

	document.addEventListener("hxOpenURL", (event) => {
		for (const { url, name, target, rel } of event.detail.value) {
			executeBrowserCommand({ command: "open-tab", url, name, target, rel });
		}
	});
})();
// Local Variables:
// js-indent-level: 4
// End:
