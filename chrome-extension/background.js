const colabPatterns = ["https://colab.research.google.com/*", "https://colab.google.com/*"];
const colabOrigins = new Set(["https://colab.research.google.com", "https://colab.google.com"]);

function tabInfo(tab, currentId) {
    const url = new URL(tab.url);
    if (!colabOrigins.has(url.origin)) {
        throw new Error("Choose a Colab tab.");
    }
    url.hash = "";
    return {tab_id: tab.id, title: tab.title, url: url.href, current: tab.id === currentId};
}

async function pageCall(tabId, func, args = []) {
    const results = await chrome.scripting.executeScript({target: {tabId}, world: "MAIN", func, args});
    const result = results[0]?.result;
    if (!result || typeof result !== "object") {
        throw new Error("The Colab tab did not respond. Reload it before trying again.");
    }
    if (result?.error) {
        throw new Error(result.error);
    }
    return result;
}

async function handle(action, parameters, sender) {
    if (!sender.tab || sender.frameId !== 0 || !colabOrigins.has(new URL(sender.url).origin)) {
        throw new Error("Only a Colab notebook tab may request these operations.");
    }
    if (action === "list_tabs") {
        const tabs = await chrome.tabs.query({url: colabPatterns});
        return {tabs: tabs.map(tab => tabInfo(tab, sender.tab.id))};
    }
    if (!Number.isInteger(parameters?.tab_id)) {
        throw new Error("A numeric Colab tab_id is required.");
    }
    const target = tabInfo(await chrome.tabs.get(parameters.tab_id), sender.tab.id);
    if (action === "prepare_tab") {
        await pageCall(target.tab_id, token => {
            const service = window.colab?.global?.notebook?.localColabMcpService;
            if (!service) {
                return {error: "Wait for the target Colab notebook to finish loading."};
            }
            if (service.isConnected() && sessionStorage.getItem("mcp_proxy_token") !== token) {
                return {error: "That tab is connected to another MCP server. Disconnect it before switching."};
            }
            return {ready: true};
        }, [parameters.token]);
        return target;
    }
    if (action !== "connect_tab") {
        throw new Error("Unknown Colab-tab operation.");
    }
    if (target.current) {
        return target;
    }
    const {token, port} = parameters;
    if (typeof token !== "string" || !/^[A-Za-z0-9_-]+$/.test(token)
        || !Number.isInteger(port) || port < 1 || port > 65535) {
        throw new Error("Invalid local MCP connection details.");
    }
    await pageCall(sender.tab.id, async () => {
        try {
            const service = window.colab.global.notebook.localColabMcpService;
            if (service.isConnected()) {
                await service.disconnect();
            }
            return {};
        } catch (error) {
            return {error: error.message};
        }
    });
    try {
        await pageCall(target.tab_id, async (nextToken, nextPort) => {
            try {
                const service = window.colab.global.notebook.localColabMcpService;
                if (service.isConnected()) {
                    if (sessionStorage.getItem("mcp_proxy_token") !== nextToken) {
                        return {error: "The selected tab is connected to another MCP server."};
                    }
                    await service.disconnect();
                }
                sessionStorage.setItem("mcp_proxy_token", nextToken);
                sessionStorage.setItem("mcp_proxy_port", String(nextPort));
                const url = new URL(location.href);
                const fragment = new URLSearchParams(url.hash.slice(1));
                fragment.set("mcpProxyToken", nextToken);
                fragment.set("mcpProxyPort", String(nextPort));
                url.hash = fragment.toString();
                history.replaceState(history.state, "", url.href);
                await service.connect();
                window.dispatchEvent(new Event("colab-mcp-credentials-changed"));
                return {};
            } catch (error) {
                return {error: error.message || "Could not connect the selected notebook."};
            }
        }, [token, port]);
    } catch (error) {
        // Restore the source connection if the target tab closed or could not connect.
        await pageCall(sender.tab.id, async () => {
            try {
                await window.colab.global.notebook.localColabMcpService.connect();
                return {};
            } catch (failure) {
                return {error: failure.message};
            }
        });
        throw error;
    }
    return target;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    handle(message.action, message.parameters, sender)
        .then(result => sendResponse({result}))
        .catch(error => sendResponse({error: error.message}));
    return true;
});
