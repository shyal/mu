// The mu debug adapter: a proxy between VS Code and debugpy that rewrites
// the Debug Adapter Protocol so the editor stays on current.mu.
//
// Towards debugpy, a breakpoint set in the mu file becomes one on the
// first Python line that mu line produced. Towards VS Code, a stack frame
// in the transpiled file is reported at its mu line, in the mu file. A
// step that lands on another Python line of the same mu line is repeated,
// so one step is one mu line. Nothing else is touched.
//
// Rewriter is pure: it knows the map and the two paths, and takes and
// returns protocol messages. The transport is in the extension.

class Rewriter {
  // map: {file, lines: {pythonLine: muLine}}; pyPath, muPath: absolute.
  constructor(map, pyPath, muPath) {
    this.pyPath = pyPath;
    this.muPath = muPath;
    this.pyToMu = new Map();
    this.muToPy = new Map();
    for (const [py, mu] of Object.entries(map.lines)) {
      const line = Number(py);
      this.pyToMu.set(line, mu);
      if (!this.muToPy.has(mu) || line < this.muToPy.get(mu)) this.muToPy.set(mu, line);
    }
    this.muLines = [...this.muToPy.keys()].sort((a, b) => a - b);
    this.lastStep = new Map(); // threadId -> {command, muLine, depth}
    this.topLine = new Map(); // threadId -> {muLine, depth} of the last stack seen
    this.pendingStack = new Map(); // seq -> threadId, for stackTrace requests
    this.nextSeq = 1 << 30; // seqs of the proxy's own requests
  }

  same(a, b) {
    return a && b && a.toLowerCase() === b.toLowerCase();
  }

  // The Python line for a mu line: a comment or blank line moves down to
  // the next line of code. null when nothing below it is code.
  pyLine(muLine) {
    const mu = this.muLines.find((l) => l >= muLine);
    return mu === undefined ? null : this.muToPy.get(mu);
  }

  muLine(pyLine) {
    return this.pyToMu.has(pyLine) ? this.pyToMu.get(pyLine) : null;
  }

  // A protocol source object: the mu file when the Python line came from it.
  source(src, line) {
    if (!src || !this.same(src.path, this.pyPath)) return { source: src, line };
    const mu = this.muLine(line);
    if (mu === null) return { source: src, line };
    return { source: { name: this.muPath.split("/").pop(), path: this.muPath }, line: mu };
  }

  // A message from VS Code, rewritten for debugpy.
  toAdapter(msg) {
    if (msg.type !== "request") return msg;
    if (msg.command === "setBreakpoints" && msg.arguments && this.same(msg.arguments.source.path, this.muPath)) {
      const args = msg.arguments;
      const seen = new Set();
      const breakpoints = [];
      for (const bp of args.breakpoints || []) {
        const py = this.pyLine(bp.line);
        if (py === null || seen.has(py)) continue;
        seen.add(py);
        breakpoints.push({ ...bp, line: py });
      }
      return {
        ...msg,
        arguments: {
          ...args,
          source: { name: this.pyPath.split("/").pop(), path: this.pyPath },
          breakpoints,
          lines: breakpoints.map((b) => b.line),
        },
      };
    }
    if (["next", "stepIn", "stepOut", "continue"].includes(msg.command) && msg.arguments) {
      const t = msg.arguments.threadId;
      const top = this.topLine.get(t);
      this.lastStep.set(t, { command: msg.command, args: msg.arguments, muLine: top ? top.muLine : null, depth: top ? top.depth : null });
    }
    if (msg.command === "stackTrace" && msg.arguments) this.pendingStack.set(msg.seq, msg.arguments.threadId);
    return msg;
  }

  // A message from debugpy, rewritten for VS Code. Returns {messages,
  // requests}: what to forward to VS Code, and what to send back to debugpy
  // (a repeated step, or a stackTrace of the proxy's own).
  toClient(msg) {
    if (msg.type === "response" && msg.command === "setBreakpoints" && msg.body && msg.body.breakpoints) {
      const breakpoints = msg.body.breakpoints.map((bp) => {
        if (!bp.source && bp.line === undefined) return bp;
        const { source, line } = this.source(bp.source || { path: this.pyPath }, bp.line);
        return { ...bp, source, line };
      });
      return { messages: [{ ...msg, body: { ...msg.body, breakpoints } }], requests: [] };
    }
    if (msg.type === "response" && msg.command === "stackTrace" && msg.body && msg.body.stackFrames) {
      const threadId = this.pendingStack.get(msg.request_seq);
      this.pendingStack.delete(msg.request_seq);
      const frames = msg.body.stackFrames.map((f) => {
        const { source, line } = this.source(f.source, f.line);
        return { ...f, source, line };
      });
      const own = msg.request_seq >= 1 << 30;
      const top = frames[0];
      if (threadId !== undefined && top) {
        const topMu = top.source && this.same(top.source.path, this.muPath) ? top.line : null;
        const depth = msg.body.totalFrames !== undefined ? msg.body.totalFrames : frames.length;
        this.topLine.set(threadId, { muLine: topMu, depth });
        if (own) return this.afterOwnStack(threadId, topMu, depth);
      }
      return { messages: [{ ...msg, body: { ...msg.body, stackFrames: frames } }], requests: [] };
    }
    if (msg.type === "event" && msg.event === "breakpoint" && msg.body && msg.body.breakpoint) {
      const bp = msg.body.breakpoint;
      if (bp.source || bp.line !== undefined) {
        const { source, line } = this.source(bp.source || { path: this.pyPath }, bp.line);
        return { messages: [{ ...msg, body: { ...msg.body, breakpoint: { ...bp, source, line } } }], requests: [] };
      }
      return { messages: [msg], requests: [] };
    }
    if (msg.type === "event" && msg.event === "stopped" && msg.body && msg.body.reason === "step") {
      const t = msg.body.threadId;
      const step = this.lastStep.get(t);
      if (step && step.muLine !== null && step.command !== "continue") {
        // ask where we are; the answer decides whether to step again
        const seq = this.nextSeq++;
        this.pendingStack.set(seq, t);
        this.held = { event: msg, threadId: t };
        return {
          messages: [],
          requests: [{ seq, type: "request", command: "stackTrace", arguments: { threadId: t, startFrame: 0, levels: 1 } }],
        };
      }
      return { messages: [msg], requests: [] };
    }
    return { messages: [msg], requests: [] };
  }

  // The proxy's own stackTrace came back after a step: on the same mu line
  // at the same depth, step once more; otherwise release the stopped event.
  afterOwnStack(threadId, topMu, depth) {
    const held = this.held;
    this.held = null;
    const step = this.lastStep.get(threadId);
    if (!held) return { messages: [], requests: [] };
    if (step && topMu !== null && topMu === step.muLine && depth === step.depth) {
      const seq = this.nextSeq++;
      return { messages: [], requests: [{ seq, type: "request", command: step.command, arguments: step.args }] };
    }
    return { messages: [held.event], requests: [] };
  }
}

// Content-Length framing for the debugpy side.
class Framer {
  constructor() {
    this.buf = Buffer.alloc(0);
  }

  encode(msg) {
    const body = Buffer.from(JSON.stringify(msg), "utf8");
    return Buffer.concat([Buffer.from(`Content-Length: ${body.length}\r\n\r\n`, "ascii"), body]);
  }

  // Feed bytes; returns the complete messages so far.
  decode(chunk) {
    this.buf = Buffer.concat([this.buf, chunk]);
    const out = [];
    for (;;) {
      const end = this.buf.indexOf("\r\n\r\n");
      if (end < 0) break;
      const head = this.buf.slice(0, end).toString("ascii");
      const m = /Content-Length:\s*(\d+)/i.exec(head);
      if (!m) throw new Error(`mu debug: bad header ${JSON.stringify(head)}`);
      const len = Number(m[1]);
      if (this.buf.length < end + 4 + len) break;
      out.push(JSON.parse(this.buf.slice(end + 4, end + 4 + len).toString("utf8")));
      this.buf = this.buf.slice(end + 4 + len);
    }
    return out;
  }
}

module.exports = { Rewriter, Framer };
