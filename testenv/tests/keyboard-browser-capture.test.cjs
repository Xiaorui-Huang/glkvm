const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const path = require("node:path");

function prepare() {
	const listeners = new Map();
	class Channel {
		send(data) {
			if (data === "throw") {
				throw this.error;
			}
			this.last = data;
			return "original-result";
		}
	}
	const original = Channel.prototype.send;
	let timeout;
	let timeoutMilliseconds;
	const context = vm.createContext({
		RTCDataChannel: Channel, console: {info() {}},
		ArrayBuffer, Uint8Array,
		performance: {now: () => 10, timeOrigin: 1000},
		document: {visibilityState: "visible", addEventListener() {}, removeEventListener() {}},
		addEventListener(type, handler) { listeners.set(type, handler); },
		removeEventListener(type) { listeners.delete(type); },
		setTimeout(handler, milliseconds) { timeout = handler; timeoutMilliseconds = milliseconds; return 1; }, clearTimeout() {},
	});
	vm.runInContext(fs.readFileSync(path.resolve(__dirname, "../../contrib/capture-keyboard-browser.js"), "utf8"), context);
	return {context, Channel, original, listeners, expire: () => timeout(), timeoutMilliseconds: () => timeoutMilliseconds};
}

test("prepared script does not record until started and restores sends", () => {
	const {context, Channel, original, listeners, expire} = prepare();
	assert.equal(Channel.prototype.send, original);
	context.kvmKeyboardCapture.start();
	const channel = new Channel();
	channel.ordered = false;
	channel.maxRetransmits = 0;
	assert.equal(channel.send(new Uint8Array([1, 1, 4])), "original-result");
	listeners.get("keyup")({type: "keyup", code: "KeyA", repeat: false});
	const captured = JSON.parse(context.kvmKeyboardCapture.json());
	assert.equal(captured.records[0].hex, "010104");
	assert.equal(captured.records[0].ordered, false);
	assert.equal(captured.records[0].maxRetransmits, 0);
	assert.equal(captured.records[2].code, "KeyA");
	expire();
	assert.equal(Channel.prototype.send, original);
	assert.equal(listeners.size, 0);
	assert.equal(JSON.parse(context.kvmKeyboardCapture.json()).reason, "duration-limit");
});

test("preserves original send exceptions without logging unrelated strings", () => {
	const {context, Channel} = prepare();
	context.kvmKeyboardCapture.start();
	const channel = new Channel();
	channel.error = new Error("original error");
	assert.throws(() => channel.send("throw"), error => error === channel.error);
	channel.send("unrelated text not to store");
	const captured = context.kvmKeyboardCapture.json();
	assert.ok(!captured.includes("unrelated text not to store"));
	assert.ok(!captured.includes("original error"));
	assert.equal(JSON.parse(captured).records[1].kind, "send-error");
	context.kvmKeyboardCapture.stop();
});

test("capture failure cannot suppress the application's send", () => {
	const {context, Channel} = prepare();
	context.kvmKeyboardCapture.start();
	const channel = new Channel();
	Object.defineProperty(channel, "bufferedAmount", {get() { throw new Error("diagnostic getter"); }});
	assert.equal(channel.send("test"), "original-result");
	assert.equal(channel.last, "test");
	context.kvmKeyboardCapture.stop();
});

test("arbitrary keys retain order, repeat and modifier information", () => {
	const {context, listeners, expire, timeoutMilliseconds} = prepare();
	context.kvmKeyboardCapture.start(300);
	assert.equal(timeoutMilliseconds(), 300000);
	const input = [
		{type: "keydown", code: "KeyZ", key: "Z", shiftKey: true},
		{type: "keydown", code: "KeyA", key: "A", shiftKey: true},
		{type: "keyup", code: "KeyZ", key: "Z", shiftKey: true},
		{type: "keydown", code: "KeyA", key: "A", shiftKey: true, repeat: true},
		{type: "keyup", code: "KeyA", key: "A", shiftKey: true},
	];
	for (const event of input) {
		listeners.get(event.type)(event);
	}
	const captured = JSON.parse(context.kvmKeyboardCapture.json());
	assert.deepEqual(captured.records.map(entry => entry.code), input.map(entry => entry.code));
	assert.deepEqual(captured.records.map(entry => entry.sequence), [1, 2, 3, 4, 5]);
	assert.equal(captured.records[3].repeat, true);
	assert.equal(captured.records[0].key, "Z");
	assert.equal(captured.durationSeconds, 300);
	assert.equal(context.kvmKeyboardCapture.status().counts.keydown, 3);
	assert.equal(context.kvmKeyboardCapture.status().webrtcSendsObserved, false);
	expire();
	assert.equal(context.kvmKeyboardCapture.status().active, false);
});

test("invalid durations do not start recording", () => {
	const {context, Channel, original} = prepare();
	for (const duration of [0, 601, 1.5, "300"]) {
		assert.throws(() => context.kvmKeyboardCapture.start(duration), /duration/);
	}
	assert.equal(Channel.prototype.send, original);
	assert.equal(context.kvmKeyboardCapture.status().active, false);
});

test("updating an inactive recorder preserves its previous data", () => {
	const {context, listeners} = prepare();
	context.kvmKeyboardCapture.start();
	listeners.get("keyup")({type: "keyup", code: "KeyZ"});
	const script = fs.readFileSync(path.resolve(__dirname, "../../contrib/capture-keyboard-browser.js"), "utf8");
	assert.throws(() => vm.runInContext(script, context), /Stop the current capture/);
	context.kvmKeyboardCapture.stop();
	vm.runInContext(script, context);
	assert.equal(JSON.parse(context.kvmKeyboardCapturePrevious.json()).records[0].code, "KeyZ");
	assert.equal(context.kvmKeyboardCapture.status().active, false);
});

test("download exports the complete stopped log and revokes its temporary URL", () => {
	const {context, listeners, expire} = prepare();
	let exported;
	let clicked = false;
	let revoked;
	const anchor = {click() { clicked = true; }};
	context.Blob = class {
		constructor(parts) { this.parts = parts; }
	};
	context.URL = {
		createObjectURL(blob) { exported = blob.parts[0]; return "blob:test"; },
		revokeObjectURL(url) { revoked = url; },
	};
	context.document.createElement = () => anchor;
	context.kvmKeyboardCapture.start(240);
	assert.throws(() => context.kvmKeyboardCapture.download(), /Stop recording/);
	listeners.get("keydown")({type: "keydown", code: "Backspace", key: "Backspace"});
	context.kvmKeyboardCapture.stop();
	context.kvmKeyboardCapture.download();
	assert.equal(clicked, true);
	assert.equal(anchor.href, "blob:test");
	assert.match(anchor.download, /^keyboard-browser-\d+\.json$/);
	assert.equal(JSON.parse(exported).records[0].code, "Backspace");
	expire();
	assert.equal(revoked, "blob:test");
});
