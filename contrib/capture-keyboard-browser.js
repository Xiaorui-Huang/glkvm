(function(root) {
	"use strict";
	if (root.kvmKeyboardCapture) {
		throw new Error("Keyboard capture already installed in this page");
	}
	let active = false;
	let timer = null;
	let started = null;
	let stopped = null;
	let reason = null;
	let records = [];
	let hooks = [];
	let connections = new WeakMap();
	let nextConnection = 1;

	function record(kind, details) {
		if (!active) {
			return;
		}
		records.push({kind, time: root.performance.now(), epoch: Date.now(), ...details});
		if (records.length >= 4096) {
			stop("record-limit");
		}
	}

	function payload(data) {
		let bytes = null;
		if (data instanceof ArrayBuffer) {
			bytes = new Uint8Array(data);
		} else if (ArrayBuffer.isView(data)) {
			bytes = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
		}
		if (bytes) {
			return {
				format: "binary", size: bytes.length,
				hex: Array.from(bytes.subarray(0, 128), value => value.toString(16).padStart(2, "0")).join(""),
				truncated: bytes.length > 128,
			};
		}
		if (typeof data === "string") {
			try {
				const parsed = JSON.parse(data);
				if (parsed.event_type === "key" && parsed.event && typeof parsed.event.key === "string") {
					return {format: "key-json", size: data.length, key: parsed.event.key,
						state: parsed.event.state, finish: parsed.event.finish};
				}
			} catch (_) {
			}
			return {format: "text-not-recorded", size: data.length};
		}
		return {format: "unsupported", size: data && typeof data.size === "number" ? data.size : null};
	}

	function installHook(constructor, transport) {
		if (!constructor || !constructor.prototype || typeof constructor.prototype.send !== "function") {
			return;
		}
		const prototype = constructor.prototype;
		const original = prototype.send;
		function wrapped(data) {
			let details = null;
			if (active) {
				try {
					if (!connections.has(this)) {
						connections.set(this, nextConnection++);
					}
					details = {transport, connection: connections.get(this), readyState: this.readyState,
						bufferedAmount: this.bufferedAmount, ...payload(data)};
					if (transport === "webrtc") {
						details.label = this.label;
						details.ordered = this.ordered;
						details.maxRetransmits = this.maxRetransmits;
						details.maxPacketLifeTime = this.maxPacketLifeTime;
					}
					record("send-call", details);
				} catch (_) {
				}
			}
			try {
				const result = Reflect.apply(original, this, arguments);
				if (details) {
					try {
						record("send-return", {transport, connection: details.connection, bufferedAmount: this.bufferedAmount});
					} catch (_) {
					}
				}
				return result;
			} catch (error) {
				if (details) {
					try {
						record("send-error", {transport, connection: details.connection, error: error.name});
					} catch (_) {
					}
				}
				throw error;
			}
		}
		prototype.send = wrapped;
		if (prototype.send !== wrapped) {
			throw new Error("Could not observe " + transport + " sends");
		}
		hooks.push({prototype, original, wrapped});
	}

	function keyEvent(event) {
		record(event.type, {code: event.code, repeat: event.repeat, trusted: event.isTrusted,
			shift: event.shiftKey, ctrl: event.ctrlKey, alt: event.altKey, meta: event.metaKey,
			eventTime: event.timeStamp});
	}

	function focusEvent(event) {
		record(event.type, {visibility: root.document.visibilityState});
	}

	function stop(stopReason = "manual") {
		if (!active) {
			return records.length;
		}
		active = false;
		root.clearTimeout(timer);
		for (const type of ["keydown", "keyup"]) {
			root.addEventListener && root.removeEventListener(type, keyEvent, true);
		}
		for (const type of ["blur", "focus"]) {
			root.removeEventListener(type, focusEvent, true);
		}
		root.document.removeEventListener("visibilitychange", focusEvent, true);
		for (const hook of hooks) {
			if (hook.prototype.send === hook.wrapped) {
				hook.prototype.send = hook.original;
			}
		}
		hooks = [];
		stopped = Date.now();
		reason = stopReason;
		root.console.info("Keyboard capture stopped:", reason, records.length, "records");
		return records.length;
	}

	function start() {
		if (active) {
			throw new Error("Capture is already running");
		}
		records = [];
		connections = new WeakMap();
		nextConnection = 1;
		started = Date.now();
		stopped = null;
		reason = null;
		active = true;
		try {
			installHook(root.RTCDataChannel, "webrtc");
			installHook(root.WebSocket, "websocket");
			for (const type of ["keydown", "keyup"]) {
				root.addEventListener(type, keyEvent, true);
			}
			for (const type of ["blur", "focus"]) {
				root.addEventListener(type, focusEvent, true);
			}
			root.document.addEventListener("visibilitychange", focusEvent, true);
			timer = root.setTimeout(() => stop("90-second-limit"), 90000);
		} catch (error) {
			stop("setup-error");
			throw error;
		}
		root.console.info("Keyboard capture running for at most 90 seconds. Use only the agreed test text.");
		return started;
	}

	function json() {
		return JSON.stringify({version: 1, active, started, stopped, reason,
			timeOrigin: root.performance.timeOrigin, records}, null, 2);
	}

	root.kvmKeyboardCapture = {start, stop, json};
	root.console.info("Keyboard capture prepared, not recording. Start with kvmKeyboardCapture.start().");
})(globalThis);
