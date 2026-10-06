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
	const context = vm.createContext({
		RTCDataChannel: Channel, console: {info() {}},
		ArrayBuffer, Uint8Array,
		performance: {now: () => 10, timeOrigin: 1000},
		document: {visibilityState: "visible", addEventListener() {}, removeEventListener() {}},
		addEventListener(type, handler) { listeners.set(type, handler); },
		removeEventListener(type) { listeners.delete(type); },
		setTimeout(handler) { timeout = handler; return 1; }, clearTimeout() {},
	});
	vm.runInContext(fs.readFileSync(path.resolve(__dirname, "../../contrib/capture-keyboard-browser.js"), "utf8"), context);
	return {context, Channel, original, listeners, expire: () => timeout()};
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
	assert.equal(JSON.parse(context.kvmKeyboardCapture.json()).reason, "90-second-limit");
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
