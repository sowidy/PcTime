// Exercise the actual worker with deterministic browser events and a fake clock.
// No browser profile, real socket, or network access is used.
import {readFile} from "node:fs/promises";
import vm from "node:vm";
import {randomUUID} from "node:crypto";
import assert from "node:assert/strict";
import test from "node:test";

const protocol = (await readFile(new URL("./protocol.js", import.meta.url), "utf8"))
  .replaceAll("export function", "function");
const worker = (await readFile(new URL("./service-worker.js", import.meta.url), "utf8"))
  .replace(/^import .*;\s*/, "");

async function harness(saved = {}, initialTime = 1_800_000_000_000, userAgent = "Chrome/155.0") {
  let now = initialTime;
  const data = structuredClone(saved), sockets = [], timers = new Map();
  const event = () => ({listeners: [], addListener(fn) { this.listeners.push(fn); },
    fire(...args) { for (const fn of this.listeners) fn(...args); }});
  const changes = event();
  const chrome = {
    storage: {onChanged: changes, local: {
      async get(keys) { return Object.fromEntries(keys.filter(k => k in data).map(k => [k, structuredClone(data[k])])); },
      async set(values) {
        const delta = {};
        for (const [key, value] of Object.entries(values)) {
          if (JSON.stringify(data[key]) !== JSON.stringify(value)) {
            delta[key] = {oldValue: structuredClone(data[key]), newValue: structuredClone(value)};
            data[key] = structuredClone(value);
          }
        }
        if (Object.keys(delta).length) changes.fire(delta, "local");
      }
    }},
    windows: {onFocusChanged: event(), async getLastFocused() { return {id: 1, focused: state.focused}; }},
    tabs: {onActivated: event(), onUpdated: event(), onRemoved: event(), async query() { return state.tab ? [state.tab] : []; }},
    runtime: {onStartup: event(), onInstalled: event(), openOptionsPage() {}},
    alarms: {onAlarm: event(), create() {}}, action: {onClicked: event()}
  };
  const state = {focused: true, tab: {id: 1, url: "https://a.example/private?SECRET=1", incognito: false}};
  class Socket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 0; this.messages = []; sockets.push(this); }
    open() { this.readyState = 1; this.onopen?.(); }
    send(raw) { this.messages.push(JSON.parse(raw)); }
    receive(message) { this.onmessage?.({data: JSON.stringify(message)}); }
    close() { this.readyState = 3; this.onclose?.(); }
  }
  class Clock extends Date { static now() { return now; } }
  const context = vm.createContext({chrome, WebSocket: Socket, URL, Date: Clock, navigator: {userAgent},
    crypto: {randomUUID}, setInterval: (fn, ms) => timers.set(ms, fn)});
  vm.runInContext(protocol + "\n" + worker, context);
  const settle = async () => { for (let n = 0; n < 20; n++) await vm.runInContext("serial", context); };
  const tick = async ms => { await timers.get(ms)(); await settle(); };
  await settle();
  return {data, sockets, chrome, state, settle, tick, now: () => now,
    advance: ms => { now += ms; },
    async pair() {
      const socket = sockets.at(-1); assert.ok(socket, "expected a connection");
      socket.open(); socket.receive({type: "hello_ack"}); await settle();
      socket.receive({type: "resume"}); await settle(); return socket;
    },
    async changeTab(tab) { state.tab = tab; chrome.tabs.onUpdated.fire(); await settle(); }
  };
}
const config = {browser: "chrome", profile: randomUUID(), token: "local-test-token"};

test("fresh install automatically connects without a token, waits for desktop approval to count", async () => {
  const h = await harness(); h.advance(20000); await h.tick(20000);
  assert.equal(h.sockets.length, 1); assert.equal(h.data.pending_events.length, 0);
  h.sockets[0].open();
  assert.equal(h.sockets[0].messages[0].auto_connect, true);
  assert.equal(h.sockets[0].messages[0].token, "");
  assert.equal(h.sockets[0].messages[0].browser, "chrome");
});

test("fresh Edge install detects its browser without manual selection and reports status", async () => {
  const h = await harness({}, 1_800_000_000_000, "Chrome/155.0 Edg/155.0");
  const ws = await h.pair();
  assert.equal(ws.messages[0].browser, "edge");
  assert.equal(h.data.connection_status, "connected");
  h.advance(20000); await h.tick(20000);
  assert.equal(h.data.pending_events[0].browser, "edge");
  ws.close(); await h.settle(); assert.equal(h.data.connection_status, "waiting");
});

test("requires desktop resume, ignores title changes, splits URL changes without saving private fields", async () => {
  const h = await harness({config}); const ws = h.sockets.at(-1);
  ws.open(); ws.receive({type: "hello_ack"}); await h.settle();
  h.advance(20000); await h.tick(20000); assert.equal(h.data.pending_events.length, 0);
  ws.receive({type: "resume"}); await h.settle(); h.advance(2000);
  await h.changeTab({...h.state.tab, title: "SECRET TITLE"});
  assert.equal(h.data.pending_events.length, 0);
  h.advance(3000); await h.changeTab({...h.state.tab, url: "https://b.example/SECRET#fragment"});
  const first = h.data.pending_events[0];
  assert.equal(first.host, "a.example"); assert.equal(first.ended_at_ms - first.started_at_ms, 5000);
  assert.ok(!JSON.stringify(h.data.pending_events).includes("SECRET"));
  assert.deepEqual(Object.keys(first).sort(), ["type", "protocol", "event_id", "session_id", "sequence",
    "browser", "profile_instance_id", "scheme", "host", "started_at_ms", "ended_at_ms"].sort());
});

test("retry keeps event identity, ACK removes durable queue, duplicate ACK is harmless", async () => {
  const h = await harness({config}); const ws = await h.pair();
  h.advance(20000); await h.tick(20000); await h.tick(500);
  const first = ws.messages.find(m => m.type === "segment"); assert.ok(first);
  h.advance(5000); await h.tick(500);
  assert.equal(ws.messages.filter(m => m.type === "segment").length, 2);
  assert.equal(ws.messages.at(-1).event_id, first.event_id);
  ws.receive({type: "ack", event_id: first.event_id}); await h.settle();
  ws.receive({type: "ack", event_id: first.event_id}); await h.settle();
  assert.equal(h.data.pending_events.length, 0);
});

test("suspend flushes once and stops time until resumed", async () => {
  const h = await harness({config}); const ws = await h.pair(); h.advance(4000);
  ws.receive({type: "suspend"}); await h.settle();
  assert.equal(h.data.pending_events.length, 1);
  h.advance(20000); await h.tick(20000); assert.equal(h.data.pending_events.length, 1);
  ws.receive({type: "resume"}); await h.settle(); h.advance(3000);
  ws.receive({type: "flush"}); await h.settle();
  assert.equal(h.data.pending_events[1].ended_at_ms - h.data.pending_events[1].started_at_ms, 3000);
});

test("incognito, internal pages, closed tabs and lost window focus never accrue time", async () => {
  const h = await harness({config}); await h.pair(); h.advance(1000);
  await h.changeTab({...h.state.tab, incognito: true});
  const count = h.data.pending_events.length;
  for (const tab of [{id: 2, url: "chrome://settings/"}, null]) {
    await h.changeTab(tab); h.advance(20000); await h.tick(20000);
    assert.equal(h.data.pending_events.length, count);
  }
  h.state.focused = false;
  await h.changeTab({id: 3, url: "https://c.example/"});
  h.advance(20000); await h.tick(20000); assert.equal(h.data.pending_events.length, count);
});

test("reconnect replays persisted events but never includes the offline gap", async () => {
  const h = await harness({config}); const ws = await h.pair();
  h.advance(20000); await h.tick(20000);
  const queued = h.data.pending_events[0].event_id;
  ws.close(); await h.settle(); h.advance(60000); await h.tick(20000);
  const next = await h.pair(); await h.tick(500);
  assert.equal(next.messages.at(-1).event_id, queued);
  h.advance(20000); await h.tick(20000);
  assert.equal(h.data.pending_events[1].ended_at_ms - h.data.pending_events[1].started_at_ms, 20000);
});

test("worker restart restores queue, discards expired events and reports the loss", async () => {
  const h = await harness({config}); await h.pair(); h.advance(20000); await h.tick(20000);
  const event = h.data.pending_events[0];
  const restored = await harness({...h.data, pending_events: [event, {...event, event_id: randomUUID(),
    started_at_ms: h.now() - 700000}]}, h.now());
  const ws = await restored.pair(); await restored.tick(500);
  assert.equal(ws.messages.at(-1).event_id, event.event_id);
  assert.equal(restored.data.pending_events.length, 1); assert.equal(restored.data.dropped_events, 1);
});

test("long sleep does not turn into a long usage segment", async () => {
  const h = await harness({config}); await h.pair();
  h.advance(300000); await h.tick(20000); assert.equal(h.data.pending_events.length, 0);
  h.advance(20000); await h.tick(20000);
  assert.equal(h.data.pending_events[0].ended_at_ms - h.data.pending_events[0].started_at_ms, 20000);
});
