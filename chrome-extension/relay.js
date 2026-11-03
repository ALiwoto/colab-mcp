// The page code can request only the narrow Colab-tab operations in background.js.
window.addEventListener("message", async event => {
    if (event.source !== window || event.origin !== location.origin
        || event.data?.channel !== "colab-mcp-extension-request") {
        return;
    }
    const {id, action, parameters} = event.data;
    let response;
    try {
        response = await chrome.runtime.sendMessage({action, parameters});
    } catch (error) {
        response = {error: error.message};
    }
    window.postMessage({channel: "colab-mcp-extension-response", id, ...response}, location.origin);
});
