const cp = require("child_process");
const path = require("path");
const vscode = require("vscode");

const SECRET_API_KEY = "chiploop.apiKey";

let output;
let lastWorkflowId;

function channel() {
  if (!output) {
    output = vscode.window.createOutputChannel("ChipLoop");
  }
  return output;
}

function config() {
  return vscode.workspace.getConfiguration("chiploop");
}

function workspaceRoot() {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || process.cwd();
}

async function env(context) {
  const apiKey = await context.secrets.get(SECRET_API_KEY);
  const baseUrl = config().get("baseUrl");
  if (!baseUrl) {
    throw new Error("Configure chiploop.baseUrl first.");
  }
  if (!apiKey) {
    throw new Error("Configure your ChipLoop API key first.");
  }
  return {
    ...process.env,
    CHIPLOOP_BASE_URL: baseUrl,
    CHIPLOOP_API_KEY: apiKey,
  };
}

function runCli(args, options = {}) {
  return new Promise((resolve, reject) => {
    const out = channel();
    out.show(true);
    out.appendLine(`chiploop ${args.join(" ")}`);

    const child = cp.spawn("chiploop", args, {
      cwd: options.cwd || workspaceRoot(),
      env: options.env || process.env,
      shell: process.platform === "win32",
    });

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (data) => {
      const text = data.toString();
      stdout += text;
      out.append(text);
    });
    child.stderr.on("data", (data) => {
      const text = data.toString();
      stderr += text;
      out.append(text);
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) {
        resolve({ stdout, stderr });
        return;
      }
      reject(new Error(stderr || `chiploop exited with code ${code}`));
    });
  });
}

function parseJsonOutput(stdout) {
  const firstBrace = stdout.indexOf("{");
  const lastBrace = stdout.lastIndexOf("}");
  if (firstBrace < 0 || lastBrace < firstBrace) {
    return {};
  }
  return JSON.parse(stdout.slice(firstBrace, lastBrace + 1));
}

async function configure(context) {
  const baseUrl = await vscode.window.showInputBox({
    title: "ChipLoop Base URL",
    value: config().get("baseUrl") || "",
    ignoreFocusOut: true,
    prompt: "Example: https://api.chiploop.com",
  });
  if (baseUrl !== undefined) {
    await config().update("baseUrl", baseUrl.trim(), vscode.ConfigurationTarget.Global);
  }

  const apiKey = await vscode.window.showInputBox({
    title: "ChipLoop API Key",
    password: true,
    ignoreFocusOut: true,
    prompt: "Stored in VS Code SecretStorage. Do not paste this into source files.",
  });
  if (apiKey) {
    await context.secrets.store(SECRET_API_KEY, apiKey.trim());
  }

  vscode.window.showInformationMessage("ChipLoop configuration saved.");
}

async function runWorkflowFromFile(context, workflowName) {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    throw new Error("Open a spec file first.");
  }
  const filePath = editor.document.uri.fsPath;
  const workflow = workflowName || config().get("defaultWorkflow") || "arch2rtl";
  const result = await runCli(["run", workflow, "--spec", filePath, "--json"], {
    env: await env(context),
  });
  const data = parseJsonOutput(result.stdout);
  lastWorkflowId = data.workflow_id || data.raw?.workflow_id;
  if (lastWorkflowId) {
    vscode.window.showInformationMessage(`ChipLoop workflow started: ${lastWorkflowId}`);
  } else {
    vscode.window.showInformationMessage("ChipLoop workflow started.");
  }
}

async function checkStatus(context) {
  const workflowId = await vscode.window.showInputBox({
    title: "ChipLoop Workflow ID",
    value: lastWorkflowId || "",
    ignoreFocusOut: true,
  });
  if (!workflowId) return;
  const result = await runCli(["status", workflowId, "--json"], { env: await env(context) });
  const data = parseJsonOutput(result.stdout);
  vscode.window.showInformationMessage(`ChipLoop status: ${data.status || data.raw?.status || "unknown"}`);
}

async function downloadArtifacts(context) {
  const workflowId = await vscode.window.showInputBox({
    title: "ChipLoop Workflow ID",
    value: lastWorkflowId || "",
    ignoreFocusOut: true,
  });
  if (!workflowId) return;
  const outputDir = config().get("outputDir") || "chiploop_outputs";
  const artifactList = await runCli(["artifacts", "list", workflowId, "--json"], { env: await env(context) });
  const data = parseJsonOutput(artifactList.stdout);
  const files = data.files || [];
  for (const name of files) {
    await runCli(["artifacts", "download", workflowId, "--name", name, "--out", outputDir], {
      env: await env(context),
    });
  }
  const fullOutputDir = path.join(workspaceRoot(), outputDir);
  vscode.window.showInformationMessage(`Downloaded ${files.length} ChipLoop artifact(s) to ${fullOutputDir}`);
}

function openPath(baseUrl, suffix) {
  const url = config().get("baseUrl");
  if (!url) {
    vscode.window.showWarningMessage("Configure chiploop.baseUrl first.");
    return;
  }
  const origin = String(url).replace(/\/api\/?$/, "").replace(/\/$/, "");
  vscode.env.openExternal(vscode.Uri.parse(`${origin}${suffix}`));
}

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("chiploop.configure", () => configure(context)),
    vscode.commands.registerCommand("chiploop.runCurrentFile", () =>
      runWorkflowFromFile(context).catch((error) => vscode.window.showErrorMessage(error.message))
    ),
    vscode.commands.registerCommand("chiploop.runArch2Rtl", () =>
      runWorkflowFromFile(context, "arch2rtl").catch((error) => vscode.window.showErrorMessage(error.message))
    ),
    vscode.commands.registerCommand("chiploop.checkStatus", () =>
      checkStatus(context).catch((error) => vscode.window.showErrorMessage(error.message))
    ),
    vscode.commands.registerCommand("chiploop.downloadArtifacts", () =>
      downloadArtifacts(context).catch((error) => vscode.window.showErrorMessage(error.message))
    ),
    vscode.commands.registerCommand("chiploop.openApps", () => openPath(config().get("baseUrl"), "/apps")),
    vscode.commands.registerCommand("chiploop.openStudio", () => openPath(config().get("baseUrl"), "/workflow"))
  );
}

function deactivate() {}

module.exports = {
  activate,
  deactivate,
};
