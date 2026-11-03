(() => {
    "use strict";

    // Capture the fragment before Colab consumes it. Session storage survives reloads.
    const initialParameters = new URLSearchParams(location.hash.slice(1));
    let socket = null;
    let retryTimer = null;
    let retryDelay = 1000;
    let lastOperation = null;
    const extensionRequests = new Map();
    const bridge = {context: notebookContext};
    window[Symbol.for("colab-mcp.companion")] = bridge;

    function extensionRequest(action, parameters = {}) {
        const id = crypto.randomUUID();
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                extensionRequests.delete(id);
                reject(new Error("The Chrome tab service did not respond. Reload the extension and this tab."));
            }, 30000);
            extensionRequests.set(id, {resolve, reject, timer});
            window.postMessage({channel: "colab-mcp-extension-request", id, action, parameters}, location.origin);
        });
    }

    window.addEventListener("message", event => {
        if (event.source !== window || event.origin !== location.origin
            || event.data?.channel !== "colab-mcp-extension-response") {
            return;
        }
        const pending = extensionRequests.get(event.data.id);
        if (!pending) {
            return;
        }
        clearTimeout(pending.timer);
        extensionRequests.delete(event.data.id);
        if (event.data.error) {
            pending.reject(new Error(event.data.error));
        } else {
            pending.resolve(event.data.result);
        }
    });

    function notebookContext() {
        const notebook = window.colab?.global?.notebook;
        if (!notebook || typeof notebook.getKernel !== "function") {
            throw new Error("Colab is still loading, or its notebook API has changed.");
        }
        if (!notebook.localColabMcpService?.isConnected()) {
            throw new Error("Allow this tab's Colab MCP connection first.");
        }
        return {notebook, kernel: notebook.getKernel(), model: notebook.getNotebookModel()};
    }

    // Colab's frontend API is undocumented. Keep its calls together here.
    function runtimeStatus() {
        const {kernel, model} = notebookContext();
        const runtime = kernel.runtime;
        return {
            extension_connected: true,
            runtime_connected: kernel.isConnected(),
            kernel_state: kernel.getState(),
            settings: {
                accelerator: model.getAccelerator() || "CPU",
                accelerator_model: model.getAcceleratorModel(),
                high_memory: model.getMachineShape() === "hm",
                runtime_version: model.getRuntimeVersion(),
            },
            last_known_runtime: runtime ? {
                kind: runtime.kind,
                accelerator: runtime.getAccelerator() || "CPU",
                accelerator_model: runtime.getAcceleratorModel() || "",
                high_memory: runtime.getMachineShape() === "hm",
            } : null,
            last_operation: lastOperation,
        };
    }

    async function performAction(parameters) {
        const {notebook, kernel, model} = notebookContext();
        switch (parameters.action) {
            case "connect":
                if (!kernel.isConnected()) {
                    await notebook.attemptAutoconnect(2);
                }
                if (!kernel.isConnected()) {
                    throw new Error("Colab did not connect. Check the tab for a prompt or error.");
                }
                break;
            case "disconnect":
                kernel.disconnect(true);
                break;
            case "restart":
                if (!kernel.isConnected()) {
                    throw new Error("Connect a runtime before restarting it.");
                }
                await kernel.restart();
                if (!kernel.isConnected()) {
                    throw new Error("Colab disconnected while restarting. Check the runtime status.");
                }
                break;
            case "interrupt":
                if (!kernel.isConnected()) {
                    throw new Error("There is no connected runtime to interrupt.");
                }
                kernel.interrupt();
                return "Interrupt requested; check kernel_state for the result.";
            case "delete":
                // Keep Colab's own confirmation dialog for deleting runtime state.
                await kernel.unassignCurrentVm();
                break;
            case "configure":
                model.setRuntime({
                    accelerator: parameters.accelerator === "CPU" ? "" : parameters.accelerator,
                    acceleratorModel: parameters.accelerator_model ?? "",
                    machineShape: parameters.high_memory === undefined
                        ? model.getMachineShape()
                        : parameters.high_memory ? "hm" : "s",
                });
                return "Settings saved. Colab may reconnect or show a prompt; check the runtime status.";
            default:
                throw new Error("Unknown runtime action.");
        }
        return "Completed.";
    }

    function startAction(parameters) {
        const {kernel} = notebookContext();
        if (bridge.files?.active && parameters.action !== "interrupt") {
            throw new Error("Finish or cancel the file transfer before changing the runtime.");
        }
        if (lastOperation?.state === "running") {
            throw new Error("A runtime operation is still running. Check runtime_status and the Colab tab.");
        }
        const transitioning = ["connecting", "allocating", "initializing", "restarting", "authorizing"];
        if (transitioning.includes(kernel.getState())) {
            throw new Error("Colab is changing runtime state. Wait for it to finish before another operation.");
        }
        const operation = {
            action: parameters.action,
            state: "running",
            started_at: new Date().toISOString(),
        };
        lastOperation = operation;
        performAction(parameters).then(message => {
            operation.state = ["configure", "interrupt"].includes(parameters.action)
                ? "requested" : "completed";
            operation.message = message;
        }).catch(error => {
            operation.state = "failed";
            operation.message = error.message || "Colab cancelled or rejected the operation.";
        });
        return runtimeStatus();
    }

    async function handleMessage(event, connection) {
        let request;
        try {
            request = JSON.parse(event.data);
        } catch {
            connection.send(JSON.stringify({
                jsonrpc: "2.0", id: null,
                error: {code: -32700, message: "Invalid JSON"},
            }));
            return;
        }
        if (request?.method === "browser/connect_tab" && !Object.hasOwn(request, "id")) {
            extensionRequest("connect_tab", request.params)
                .catch(error => console.warn("Colab MCP tab switch:", error.message));
            return;
        }
        if (!request || !Object.hasOwn(request, "id") || !request.method) {
            return;
        }
        const response = {jsonrpc: "2.0", id: request.id};
        try {
            switch (request.method) {
                case "runtime/status":
                    response.result = runtimeStatus();
                    break;
                case "runtime/action":
                    response.result = startAction(request.params);
                    break;
                case "browser/list_tabs":
                    response.result = await extensionRequest("list_tabs");
                    break;
                case "browser/prepare_tab":
                    response.result = await extensionRequest("prepare_tab", request.params);
                    break;
                default:
                    if (request.method.startsWith("files/") && bridge.files) {
                        response.result = await bridge.files.request(request.method, request.params);
                    } else {
                        response.error = {code: -32601, message: "Unknown companion method"};
                    }
            }
        } catch (error) {
            response.error = {code: -32000, message: error.message};
        }
        if (connection.readyState === WebSocket.OPEN) {
            connection.send(JSON.stringify(response));
        }
    }

    function credentials() {
        const parameters = new URLSearchParams(location.hash.slice(1));
        const token = parameters.get("mcpProxyToken")
            || sessionStorage.getItem("mcp_proxy_token")
            || initialParameters.get("mcpProxyToken");
        const port = parameters.get("mcpProxyPort")
            || sessionStorage.getItem("mcp_proxy_port")
            || initialParameters.get("mcpProxyPort");
        if (!token || !/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) {
            return null;
        }
        return {token, port};
    }

    // Auto-accept only the MCP dialog whose known token matches this tab's connection.
    // Sign-in, browser permissions, and runtime deletion dialogs remain Colab's responsibility.
    customElements.whenDefined("colab-local-mcp-connect-dialog").then(() => {
        const Dialog = customElements.get("colab-local-mcp-connect-dialog");
        const originalShow = Dialog.prototype.show;
        Dialog.prototype.show = function (...args) {
            const auth = credentials();
            if (auth && this.knownInput === `${auth.token}&${auth.port}`) {
                return Promise.resolve(this.knownInput);
            }
            return originalShow.apply(this, args);
        };
    });

    function scheduleConnection() {
        clearTimeout(retryTimer);
        retryTimer = setTimeout(connect, retryDelay);
    }

    function connect() {
        if (socket) {
            return;
        }
        const auth = credentials();
        if (!auth) {
            return;
        }
        // The normal Colab MCP connection remains separate from the companion channel.
        if (!window.colab?.global?.notebook?.localColabMcpService?.isConnected()) {
            scheduleConnection();
            return;
        }
        const connection = new WebSocket(
            `ws://127.0.0.1:${auth.port}/runtime?access_token=${encodeURIComponent(auth.token)}`,
            "mcp",
        );
        socket = connection;
        connection.onopen = () => {
            retryDelay = 1000;
        };
        connection.onmessage = event => handleMessage(event, connection);
        connection.onclose = () => {
            socket = null;
            bridge.files?.close();
            retryDelay = Math.min(retryDelay * 2, 10000);
            scheduleConnection();
        };
    }

    window.addEventListener("hashchange", () => {
        if (!socket) {
            connect();
        }
    });
    window.addEventListener("colab-mcp-credentials-changed", connect);
    window.addEventListener("pagehide", () => {
        clearTimeout(retryTimer);
        if (socket) {
            socket.onclose = null;
            socket.close();
            socket = null;
        }
    });
    window.addEventListener("pageshow", connect);
    connect();
})();
