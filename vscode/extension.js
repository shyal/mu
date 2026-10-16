// Formats .mu files by piping the document through `python -m mulang --fmt -`,
// answers hover, completion, go-to-definition and signature help through
// `python -m mulang.ide` (one process per workspace root, JSON lines both
// ways), and debugs a mu file: the workspace's debug script (the `mu.debugScript`
// setting, `mu/session.py` in the leet repo) writes the Python with a line
// map, and the `mu` debug type runs debugpy on it behind a proxy (adapter.js)
// that keeps the editor on the mu file.
const vscode = require("vscode");
const path = require("path");
const fs = require("fs");
const { execFile, spawn } = require("child_process");
const { Rewriter, Framer } = require("./adapter");

// The repo this extension lives in holds the mulang package; on PYTHONPATH
// it serves an interpreter that has no mulang installed, and -S keeps the
// formatter from paying for the interpreter's site imports.
const MU_ROOT = path.join(__dirname, "..");
const env = () => ({
  ...process.env,
  PYTHONPATH: [MU_ROOT, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
});

function format(document) {
  const python = vscode.workspace.getConfiguration("mu").get("python");
  return new Promise((resolve) => {
    const child = execFile(python, ["-S", "-m", "mulang", "--fmt", "-"], { env: env() }, (err, stdout, stderr) => {
      if (err) {
        vscode.window.showErrorMessage(`mu fmt: ${(stderr || err.message).trim()}`);
        return resolve([]);
      }
      const text = document.getText();
      if (stdout === text) return resolve([]);
      const all = new vscode.Range(document.positionAt(0), document.positionAt(text.length));
      resolve([vscode.TextEdit.replace(all, stdout)]);
    });
    child.stdin.end(document.getText());
  });
}

// The workspace folder holding current.mu: the folder of the active file
// when it is a .mu or current.py, else the first workspace folder.
function workspaceRoot() {
  const doc = vscode.window.activeTextEditor && vscode.window.activeTextEditor.document;
  const folder = doc && vscode.workspace.getWorkspaceFolder(doc.uri);
  const first = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
  const f = folder || first;
  return f ? f.uri.fsPath : null;
}

// The interpreter that runs the solve: the repo's .venv, whose sitecustomize
// preloads the harness, else the configured python.
function solvePython(root) {
  const venv = path.join(root, ".venv", "bin", "python3");
  if (fs.existsSync(venv)) return venv;
  return vscode.workspace.getConfiguration("mu").get("python");
}

function run(cmd, args, cwd) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { cwd, env: env() }, (err, stdout, stderr) => {
      if (err) return reject(new Error((stderr || err.message).trim()));
      resolve(stdout);
    });
  });
}

// The debugpy the Python Debugger extension ships; the venv python runs it.
function debugpyLibs() {
  const ext = vscode.extensions.getExtension("ms-python.debugpy");
  if (!ext) return null;
  const libs = path.join(ext.extensionPath, "bundled", "libs");
  return fs.existsSync(path.join(libs, "debugpy")) ? libs : null;
}

// Fills in a `mu` launch config: builds .mu_current.py and its map, and
// names the program, the mu file and the interpreter.
class MuConfigurationProvider {
  async resolveDebugConfiguration(folder, config) {
    const root = (folder && folder.uri.fsPath) || workspaceRoot();
    if (!root) {
      vscode.window.showErrorMessage("mu debug: open the repo as a workspace folder");
      return undefined;
    }
    const script = vscode.workspace.getConfiguration("mu").get("debugScript");
    if (!script || !fs.existsSync(path.join(root, script))) {
      vscode.window.showErrorMessage(`mu debug: no debug script at ${script || "(mu.debugScript unset)"} under ${root}`);
      return undefined;
    }
    const python = solvePython(root);
    await vscode.workspace.saveAll();
    // the mu file in the editor, else current.mu
    const doc = vscode.window.activeTextEditor && vscode.window.activeTextEditor.document;
    const active = doc && doc.languageId === "mu" ? doc.uri.fsPath : path.join(root, "current.mu");
    const args = [path.join(root, script), "debug"];
    if (path.relative(root, active) !== "current.mu") args.push(active);
    let target;
    try {
      target = (await run(python, args, root)).trim();
    } catch (err) {
      vscode.window.showErrorMessage(`mu debug: ${err.message}`);
      return undefined;
    }
    const mapPath = path.join(root, ".mu_current.map.json");
    const mapped = target !== "current.py" && fs.existsSync(mapPath);
    const map = mapped ? JSON.parse(fs.readFileSync(mapPath, "utf8")) : null;
    return {
      ...config,
      type: "mu",
      request: "launch",
      name: config.name || `mu: ${path.basename(active)}`,
      program: path.join(root, target),
      muFile: map ? path.resolve(root, map.file) : active,
      mapFile: mapped ? mapPath : null,
      python: [python],
      cwd: root,
      console: config.console || "internalConsole",
      justMyCode: false,
    };
  }
}

// The adapter VS Code talks to: debugpy's own adapter over stdio, with
// every message passing through the Rewriter.
class MuDebugAdapter {
  constructor(session) {
    const cfg = session.configuration;
    const map = cfg.mapFile ? JSON.parse(fs.readFileSync(cfg.mapFile, "utf8")) : { lines: {} };
    this.rw = new Rewriter(map, cfg.program, cfg.muFile);
    this.emitter = new vscode.EventEmitter();
    this.onDidSendMessage = this.emitter.event;
    this.framer = new Framer();
    const libs = debugpyLibs();
    const python = cfg.python[0];
    const logDir = vscode.workspace.getConfiguration("mu").get("debugLogDir");
    const args = ["-m", "debugpy.adapter"];
    if (logDir) args.push("--log-dir", logDir, "--log-stderr");
    this.child = spawn(python, args, {
      cwd: cfg.cwd,
      env: { ...process.env, PYTHONPATH: libs ? libs : process.env.PYTHONPATH || "" },
    });
    this.child.stdout.on("data", (chunk) => {
      for (const msg of this.framer.decode(chunk)) this.fromAdapter(msg);
    });
    this.child.stderr.on("data", (d) => console.error(`mu debug: ${d}`));
    this.child.on("exit", (code) => {
      if (code) vscode.window.showErrorMessage(`mu debug: debugpy adapter exited with ${code}`);
      this.emitter.fire({ type: "event", event: "terminated", seq: 0 });
    });
    if (!libs) vscode.window.showErrorMessage("mu debug: the Python Debugger extension (ms-python.debugpy) is not installed");
  }

  // VS Code -> debugpy
  handleMessage(msg) {
    if (msg.type === "request" && msg.command === "launch") {
      const a = { ...msg.arguments };
      delete a.muFile;
      delete a.mapFile;
      msg = { ...msg, arguments: { ...a, type: "python" } };
    }
    this.send(this.rw.toAdapter(msg));
  }

  // debugpy -> VS Code, with the proxy's own follow-up requests
  fromAdapter(msg) {
    const { messages, requests } = this.rw.toClient(msg);
    for (const m of messages) this.emitter.fire(m);
    for (const r of requests) this.send(r);
  }

  send(msg) {
    if (this.child.stdin.writable) this.child.stdin.write(this.framer.encode(msg));
  }

  dispose() {
    if (this.child && !this.child.killed) this.child.kill();
    this.emitter.dispose();
  }
}

// The ide backend: started on the first request, again after it exits.
// A request that gets no reply within five seconds resolves to null.
class MuIde {
  constructor() {
    this.child = null;
    this.root = null;
    this.pending = new Map(); // id -> resolve
    this.seq = 0;
    this.buffer = "";
    this.warned = false;
  }

  start(root) {
    this.root = root;
    this.buffer = "";
    const child = spawn(solvePython(root), ["-m", "mulang.ide"], { cwd: root, env: env() });
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      this.buffer += chunk;
      let nl;
      while ((nl = this.buffer.indexOf("\n")) >= 0) {
        const line = this.buffer.slice(0, nl);
        this.buffer = this.buffer.slice(nl + 1);
        let msg;
        try {
          msg = JSON.parse(line);
        } catch {
          continue;
        }
        const resolve = this.pending.get(msg.id);
        if (!resolve) continue;
        this.pending.delete(msg.id);
        if (msg.error) console.error(`mu ide: ${msg.error}`);
        resolve(msg.error ? null : msg.result);
      }
    });
    child.stderr.on("data", (d) => {
      const text = String(d).trim();
      console.error(`mu ide: ${text}`);
      if (!this.warned && text.includes("jedi")) {
        this.warned = true;
        vscode.window.showErrorMessage(`mu ide: ${text}`);
      }
    });
    child.on("exit", () => {
      if (this.child === child) this.child = null;
      for (const resolve of this.pending.values()) resolve(null);
      this.pending.clear();
    });
    this.child = child;
  }

  request(cmd, document, position) {
    const folder = vscode.workspace.getWorkspaceFolder(document.uri);
    const root = (folder && folder.uri.fsPath) || workspaceRoot();
    if (!root) return Promise.resolve(null);
    if (!this.child || this.root !== root) {
      if (this.child) this.child.kill();
      this.start(root);
    }
    const id = ++this.seq;
    const req = { id, cmd, root, text: document.getText(), line: position.line, col: position.character };
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        if (this.pending.delete(id)) resolve(null);
      }, 5000);
      this.pending.set(id, (result) => {
        clearTimeout(timer);
        resolve(result);
      });
      this.child.stdin.write(JSON.stringify(req) + "\n");
    });
  }

  dispose() {
    if (this.child) this.child.kill();
  }
}

const KINDS = {
  function: vscode.CompletionItemKind.Function,
  class: vscode.CompletionItemKind.Class,
  module: vscode.CompletionItemKind.Module,
  variable: vscode.CompletionItemKind.Variable,
  keyword: vscode.CompletionItemKind.Keyword,
  property: vscode.CompletionItemKind.Property,
};

// the token types of the "primes" answer, in mulang.ide.SEMANTIC's order
const LEGEND = new vscode.SemanticTokensLegend(["variable", "operator", "number", "string", "keyword"]);

const fenced = (text) => new vscode.MarkdownString("```mu\n" + text + "\n```");

// The language features, each one request to the backend. A definition
// with no path is in the document itself.
function providers(ide) {
  const location = (document, d) =>
    new vscode.Location(d.path ? vscode.Uri.file(d.path) : document.uri, new vscode.Position(d.line, d.col));
  return [
    // `f'`, `r'` and `b'` on a bound name are primes, which the grammar
    // colours as a string: the backend colours that stretch again
    vscode.languages.registerDocumentSemanticTokensProvider(
      "mu",
      {
        async provideDocumentSemanticTokens(document) {
          const r = await ide.request("primes", document, new vscode.Position(0, 0));
          const tokens = new vscode.SemanticTokensBuilder(LEGEND);
          for (const [line, col, length, kind] of r || []) {
            tokens.push(line, col, length, LEGEND.tokenTypes.indexOf(kind));
          }
          return tokens.build();
        },
      },
      LEGEND
    ),
    vscode.languages.registerHoverProvider("mu", {
      async provideHover(document, position) {
        const r = await ide.request("hover", document, position);
        return r ? new vscode.Hover(new vscode.MarkdownString(r.contents)) : null;
      },
    }),
    vscode.languages.registerDefinitionProvider("mu", {
      async provideDefinition(document, position) {
        const r = await ide.request("definition", document, position);
        return (r || []).map((d) => location(document, d));
      },
    }),
    vscode.languages.registerCompletionItemProvider(
      "mu",
      {
        async provideCompletionItems(document, position) {
          const r = await ide.request("complete", document, position);
          return (r || []).map((c) => {
            const item = new vscode.CompletionItem(c.label, KINDS[c.kind] || vscode.CompletionItemKind.Variable);
            if (c.detail) item.detail = c.detail;
            if (c.doc) item.documentation = fenced(c.doc);
            return item;
          });
        },
      },
      "."
    ),
    vscode.languages.registerSignatureHelpProvider(
      "mu",
      {
        async provideSignatureHelp(document, position) {
          const r = await ide.request("signature", document, position);
          if (!r) return null;
          const sig = new vscode.SignatureInformation(r.label, r.doc ? fenced(r.doc) : undefined);
          sig.parameters = r.params.map((p) => new vscode.ParameterInformation(p));
          const help = new vscode.SignatureHelp();
          help.signatures = [sig];
          help.activeSignature = 0;
          help.activeParameter = Math.min(r.active, Math.max(r.params.length - 1, 0));
          return help;
        },
      },
      "(",
      ","
    ),
  ];
}

async function debug() {
  const root = workspaceRoot();
  if (!root) return vscode.window.showErrorMessage("mu debug: open the repo as a workspace folder");
  const folder = vscode.workspace.getWorkspaceFolder(vscode.Uri.file(root));
  await vscode.debug.startDebugging(folder, { type: "mu", request: "launch", name: "mu: current.mu" });
}

function activate(context) {
  const ide = new MuIde();
  context.subscriptions.push(
    ide,
    ...providers(ide),
    vscode.languages.registerDocumentFormattingEditProvider("mu", { provideDocumentFormattingEdits: format }),
    vscode.commands.registerCommand("mu.debug", debug),
    vscode.debug.registerDebugConfigurationProvider("mu", new MuConfigurationProvider()),
    vscode.debug.registerDebugAdapterDescriptorFactory("mu", {
      createDebugAdapterDescriptor: (session) => new vscode.DebugAdapterInlineImplementation(new MuDebugAdapter(session)),
    })
  );
}

module.exports = { activate };
