// node --test mu/vscode: the Rewriter on protocol messages, and a live run
// of the proxy against the debugpy the Python Debugger extension ships.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn, execFileSync } = require("child_process");
const { Rewriter, Framer } = require("./adapter");

const ROOT = path.resolve(__dirname, "..", "..");
const PY = "/py/.mu_current.py";
const MU = "/py/current.mu";
// mu line 3 makes Python lines 10 and 11; mu line 5 makes Python line 13
const MAP = { file: "current.mu", lines: { 9: 2, 10: 3, 11: 3, 13: 5 } };

test("breakpoints in the mu file move to the first python line of that mu line", () => {
  const rw = new Rewriter(MAP, PY, MU);
  const out = rw.toAdapter({
    seq: 1, type: "request", command: "setBreakpoints",
    arguments: { source: { path: MU }, breakpoints: [{ line: 3 }, { line: 4 }, { line: 5 }, { line: 99 }] },
  });
  assert.equal(out.arguments.source.path, PY);
  // line 4 is a comment: it moves down to line 5, once
  assert.deepEqual(out.arguments.breakpoints.map((b) => b.line), [10, 13]);
  assert.deepEqual(out.arguments.lines, [10, 13]);
});

test("breakpoints in other files pass through", () => {
  const rw = new Rewriter(MAP, PY, MU);
  const msg = { seq: 1, type: "request", command: "setBreakpoints", arguments: { source: { path: "/py/other.py" }, breakpoints: [{ line: 3 }] } };
  assert.equal(rw.toAdapter(msg), msg);
});

test("verified breakpoints come back on mu lines", () => {
  const rw = new Rewriter(MAP, PY, MU);
  const { messages } = rw.toClient({
    seq: 2, type: "response", request_seq: 1, command: "setBreakpoints", success: true,
    body: { breakpoints: [{ verified: true, line: 10, source: { path: PY } }, { verified: true, line: 13 }] },
  });
  assert.deepEqual(messages[0].body.breakpoints.map((b) => [b.line, b.source.path]), [[3, MU], [5, MU]]);
});

test("stack frames in the transpiled file are reported in the mu file", () => {
  const rw = new Rewriter(MAP, PY, MU);
  rw.toAdapter({ seq: 5, type: "request", command: "stackTrace", arguments: { threadId: 1 } });
  const { messages } = rw.toClient({
    seq: 6, type: "response", request_seq: 5, command: "stackTrace", success: true,
    body: { stackFrames: [{ id: 1, name: "twoSum", line: 11, source: { path: PY } }, { id: 2, name: "<module>", line: 3, source: { path: "/py/helper.py" } }], totalFrames: 2 },
  });
  const [top, below] = messages[0].body.stackFrames;
  assert.deepEqual([top.line, top.source.path, top.source.name], [3, MU, "current.mu"]);
  assert.deepEqual([below.line, below.source.path], [3, "/py/helper.py"]);
});

test("a step that stays on the same mu line is repeated, one that moves is reported", () => {
  const rw = new Rewriter(MAP, PY, MU);
  // the client saw the stack at mu line 3, depth 2, then stepped
  rw.toAdapter({ seq: 5, type: "request", command: "stackTrace", arguments: { threadId: 1 } });
  rw.toClient({ seq: 6, type: "response", request_seq: 5, command: "stackTrace", success: true, body: { stackFrames: [{ id: 1, line: 10, source: { path: PY } }], totalFrames: 2 } });
  rw.toAdapter({ seq: 7, type: "request", command: "next", arguments: { threadId: 1 } });
  // debugpy stopped on python line 11: still mu line 3
  let r = rw.toClient({ seq: 8, type: "event", event: "stopped", body: { reason: "step", threadId: 1 } });
  assert.deepEqual(r.messages, []);
  assert.equal(r.requests[0].command, "stackTrace");
  const own = r.requests[0].seq;
  r = rw.toClient({ seq: 9, type: "response", request_seq: own, command: "stackTrace", success: true, body: { stackFrames: [{ id: 1, line: 11, source: { path: PY } }], totalFrames: 2 } });
  assert.deepEqual(r.messages, []);
  assert.deepEqual([r.requests[0].command, r.requests[0].arguments.threadId], ["next", 1]);
  // now on python line 13: mu line 5, the stop is released
  r = rw.toClient({ seq: 10, type: "event", event: "stopped", body: { reason: "step", threadId: 1 } });
  const own2 = r.requests[0].seq;
  r = rw.toClient({ seq: 11, type: "response", request_seq: own2, command: "stackTrace", success: true, body: { stackFrames: [{ id: 1, line: 13, source: { path: PY } }], totalFrames: 2 } });
  assert.deepEqual(r.requests, []);
  assert.equal(r.messages[0].event, "stopped");
});

test("a stop for a breakpoint or exception is never held", () => {
  const rw = new Rewriter(MAP, PY, MU);
  for (const reason of ["breakpoint", "exception", "pause", "entry"]) {
    const r = rw.toClient({ seq: 1, type: "event", event: "stopped", body: { reason, threadId: 1 } });
    assert.equal(r.messages.length, 1);
    assert.deepEqual(r.requests, []);
  }
});

test("the framer round-trips messages split across chunks", () => {
  const f = new Framer();
  const a = f.encode({ seq: 1, type: "request", command: "initialize" });
  const b = f.encode({ seq: 2, type: "event", event: "initialized" });
  const all = Buffer.concat([a, b]);
  const got = [...f.decode(all.slice(0, 7)), ...f.decode(all.slice(7, a.length + 3)), ...f.decode(all.slice(a.length + 3))];
  assert.deepEqual(got.map((m) => m.seq), [1, 2]);
});

// The live run: the proxy in front of the real debugpy adapter, driven as
// VS Code would drive it, on a Two Sum solve in a scratch folder.

function bundledDebugpy() {
  const dir = path.join(os.homedir(), ".vscode", "extensions");
  if (!fs.existsSync(dir)) return null;
  const hits = fs.readdirSync(dir).filter((d) => d.startsWith("ms-python.debugpy-")).sort();
  for (const h of hits.reverse()) {
    const libs = path.join(dir, h, "bundled", "libs");
    if (fs.existsSync(path.join(libs, "debugpy"))) return libs;
  }
  return null;
}

const VENV = path.join(ROOT, ".venv", "bin", "python3");
const LIBS = bundledDebugpy();
const live = fs.existsSync(VENV) && LIBS ? test : test.skip;

const PROBLEM = `"""
URL: https://leetcode.com/problems/two-sum/description/

1. Two Sum

Given nums and target, return the indices of the two numbers that add up
to target.
"""


class Solution:
    def twoSum(self, nums: List[int], target: int) -> List[int]:
        pass


sol = Solution()

# assert sol.twoSum([2, 7, 11, 15], 9) == [0, 1]
`;

const TWO_SUM = `class Solution
  def twoSum(nums: [int], target: int) -> [int]
    seen = {}
    for >i, x in nums
      if target - x in seen
        return [seen[target - x], i]
      seen[x] = i
`;

// console: internalConsole runs the solve under the adapter; integratedTerminal
// makes the adapter ask the client to run the launcher, which the test does.
for (const console of ["internalConsole", "integratedTerminal"]) live(`stepping through a solve stays on current.mu, one stop per mu line (${console})`, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mu-debug-"));
  fs.writeFileSync(path.join(dir, "current.py"), PROBLEM);
  const env = { ...process.env, PYTHONPATH: [ROOT, path.join(ROOT, "utils"), path.join(ROOT, "utils", "harness")].join(path.delimiter) };
  const session = path.join(ROOT, "mu", "session.py");
  execFileSync(VENV, [session, "stub"], { cwd: dir, env });
  const stub = fs.readFileSync(path.join(dir, "current.mu"), "utf8");
  const work = stub.replace("class Solution\n  def twoSum(nums: [int], target: int) -> [int]\n    pass\n", TWO_SUM).replace("# assert", "assert");
  fs.writeFileSync(path.join(dir, "current.mu"), work);
  const target = execFileSync(VENV, [session, "debug"], { cwd: dir, env }).toString().trim();
  assert.equal(target, ".mu_current.py");
  const map = JSON.parse(fs.readFileSync(path.join(dir, ".mu_current.map.json"), "utf8"));
  const muPath = path.join(dir, "current.mu");
  const pyPath = path.join(dir, target);
  const muLines = work.split("\n");
  const at = (text) => muLines.indexOf(text) + 1;

  const rw = new Rewriter(map, pyPath, muPath);
  const framer = new Framer();
  const child = spawn(VENV, ["-m", "debugpy.adapter"], { cwd: dir, env: { ...env, PYTHONPATH: LIBS } });
  const inbox = [];
  const waiters = [];
  const children = [];
  let seq = 1;
  const send = (m) => child.stdin.write(framer.encode(m));
  child.stdout.on("data", (chunk) => {
    for (const raw of framer.decode(chunk)) {
      const { messages, requests } = rw.toClient(raw);
      for (const r of requests) send(r);
      for (const m of messages) {
        if (m.type === "request" && m.command === "runInTerminal") {
          const a = m.arguments;
          const p = spawn(a.args[0], a.args.slice(1), { cwd: a.cwd, env: { ...env, ...(a.env || {}) } });
          children.push(p);
          send({ seq: seq++, type: "response", request_seq: m.seq, success: true, command: "runInTerminal", body: { processId: p.pid } });
          continue;
        }
        inbox.push(m);
        const pending = waiters.splice(0);
        for (const w of pending) if (!w()) waiters.push(w);
      }
    }
  });
  const request = async (command, args) => {
    const s = seq++;
    send(rw.toAdapter({ seq: s, type: "request", command, arguments: args || {} }));
    return until((m) => m.type === "response" && m.request_seq === s);
  };
  const until = (pred) =>
    new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error(`timed out; inbox: ${JSON.stringify(inbox.slice(-5))}`)), 20000);
      const check = () => {
        const k = inbox.findIndex(pred);
        if (k >= 0) {
          clearTimeout(t);
          resolve(inbox.splice(k, 1)[0]);
          return true;
        }
        return false;
      };
      if (!check()) waiters.push(check);
    });
  const stopped = () => until((m) => m.type === "event" && m.event === "stopped");
  const top = async (threadId) => {
    const r = await request("stackTrace", { threadId, startFrame: 0, levels: 1 });
    const f = r.body.stackFrames[0];
    return [path.basename(f.source.path), f.line];
  };

  try {
    await request("initialize", { adapterID: "mu", clientID: "test", linesStartAt1: true, columnsStartAt1: true, pathFormat: "path", supportsRunInTerminalRequest: true });
    const launch = request("launch", { type: "python", request: "launch", name: "t", program: pyPath, python: [VENV], cwd: dir, console, justMyCode: false });
    await until((m) => m.type === "event" && m.event === "initialized");
    const bps = await request("setBreakpoints", { source: { path: muPath }, breakpoints: [{ line: at("    seen = {}") }] });
    assert.equal(bps.body.breakpoints[0].verified, true);
    assert.equal(path.basename(bps.body.breakpoints[0].source.path), "current.mu");
    assert.equal(bps.body.breakpoints[0].line, at("    seen = {}"));
    await request("configurationDone");
    await launch;

    let ev = await stopped();
    assert.equal(ev.body.reason, "breakpoint");
    const threadId = ev.body.threadId;
    assert.deepEqual(await top(threadId), ["current.mu", at("    seen = {}")]);

    // step over: the loop header, then the if, then the assignment, then back to the header
    const walk = [];
    for (let k = 0; k < 4; k++) {
      await request("next", { threadId });
      ev = await stopped();
      assert.equal(ev.body.reason, "step");
      walk.push((await top(threadId))[1]);
    }
    assert.deepEqual(walk, [at("    for >i, x in nums"), at("      if target - x in seen"), at("      seen[x] = i"), at("    for >i, x in nums")]);

    await request("continue", { threadId });
    await until((m) => m.type === "event" && (m.event === "terminated" || m.event === "exited"));
  } finally {
    child.kill();
    for (const p of children) p.kill();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
