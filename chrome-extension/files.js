(() => {
    "use strict";

    const bridge = window[Symbol.for("colab-mcp.companion")];
    const transfers = new Map();
    const chunkSize = 1024 * 1024;

    function apiPath(path, prefix = "/api/contents") {
        return prefix + "/" + path.slice(1).split("/").map(encodeURIComponent).join("/");
    }

    async function fetchFile(transfer, path, options = {}) {
        transfer.abort.signal.throwIfAborted();
        const kernel = window.colab?.global?.notebook?.getKernel();
        if (!kernel || kernel.runtime !== transfer.runtime || !kernel.isConnected()) {
            throw new Error("The runtime changed or disconnected during the transfer.");
        }
        const response = await transfer.xhr.fetch(path, {
            ...options, signal: transfer.abort.signal,
        });
        if (!response.ok && response.status !== 404) {
            throw new Error(`Colab file request failed (${response.status}): ${(await response.text()).slice(0, 300)}`);
        }
        return response;
    }

    async function metadata(transfer, path) {
        const response = await fetchFile(transfer, apiPath(path) + "?content=0&type=file");
        return response.status === 404 ? null : response.json();
    }

    async function remove(transfer, path) {
        await fetchFile(transfer, apiPath(path), {method: "DELETE"});
    }

    async function rename(transfer, from, to) {
        const response = await fetchFile(transfer, apiPath(from), {
            method: "PATCH", body: JSON.stringify({path: to.slice(1)}),
        });
        if (response.status === 404) {
            throw new Error(`Colab file disappeared: ${from}`);
        }
        await response.arrayBuffer();
    }

    async function createParents(transfer, path) {
        const parts = path.split("/").slice(1, -1);
        let parent = "";
        for (const part of parts) {
            parent += "/" + part;
            const response = await fetchFile(transfer, apiPath(parent) + "?content=0");
            if (response.status === 404) {
                const created = await fetchFile(transfer, apiPath(parent), {
                    method: "PUT", body: JSON.stringify({type: "directory"}),
                });
                if (!created.ok) {
                    throw new Error(`Could not create Colab directory: ${parent}`);
                }
                await created.arrayBuffer();
            } else if ((await response.json()).type !== "directory") {
                throw new Error(`Colab parent is not a directory: ${parent}`);
            }
        }
    }

    function createTransfer(parameters) {
        const {notebook, kernel} = bridge.context();
        const xhr = notebook.getKernelFiles()?.xhr;
        if (!kernel.isConnected() || !xhr) {
            throw new Error("Connect a runtime before transferring files.");
        }
        if (transfers.has(parameters.transfer_id)) {
            throw new Error("This transfer already exists.");
        }
        const transfer = {
            ...parameters, xhr, runtime: kernel.runtime, abort: new AbortController(),
            offset: 0, chunk: 1,
        };
        transfers.set(parameters.transfer_id, transfer);
        return transfer;
    }

    async function begin(transfer) {
        if (transfer.direction === "upload") {
            const existing = await metadata(transfer, transfer.path);
            if (existing && (!transfer.overwrite || existing.type === "directory")) {
                throw new Error("Colab destination exists; use overwrite=true to replace a file.");
            }
            await createParents(transfer, transfer.path);
            const parent = transfer.path.slice(0, transfer.path.lastIndexOf("/"));
            transfer.temporary = `${parent}/colab-mcp-${transfer.transfer_id}.part`;
            const response = await fetchFile(transfer, apiPath(transfer.temporary), {
                method: "PUT", body: JSON.stringify({type: "file", format: "base64", content: ""}),
            });
            if (!response.ok) {
                throw new Error("Could not create the Colab upload staging file.");
            }
            await response.arrayBuffer();
            return {temporary_path: transfer.temporary};
        }
        transfer.source = await metadata(transfer, transfer.path);
        if (!transfer.source || transfer.source.type === "directory") {
            throw new Error("Colab source is not a file.");
        }
        return {size: transfer.source.size};
    }

    async function write(transfer, parameters) {
        const length = atob(parameters.data).length;
        if (transfer.direction !== "upload" || parameters.offset !== transfer.offset
            || length > chunkSize || transfer.offset + length > transfer.size) {
            throw new Error("Invalid upload chunk or offset.");
        }
        const response = await fetchFile(transfer, apiPath(transfer.temporary), {
            method: "PUT",
            body: JSON.stringify({
                type: "file", format: "base64", content: parameters.data, chunk: transfer.chunk,
                name: transfer.temporary.slice(transfer.temporary.lastIndexOf("/") + 1),
                path: transfer.temporary.slice(0, transfer.temporary.lastIndexOf("/")),
            }),
        });
        if (!response.ok) {
            throw new Error("Colab upload staging file disappeared.");
        }
        await response.arrayBuffer();
        transfer.offset += length;
        transfer.chunk += 1;
        return {bytes_written: transfer.offset};
    }

    async function read(transfer, parameters) {
        const {offset, length} = parameters;
        if (transfer.direction !== "download" || offset !== transfer.offset
            || !Number.isInteger(length) || length < 1 || length > chunkSize
            || offset + length > transfer.source.size) {
            throw new Error("Invalid download range.");
        }
        const response = await fetchFile(transfer, apiPath(transfer.path, "/files"), {
            headers: new Headers({Range: `bytes=${offset}-${offset + length - 1}`}),
        });
        if (response.status !== 206 && !(response.status === 200 && offset === 0 && length === transfer.source.size)) {
            await response.body?.cancel();
            throw new Error("Colab did not honor the requested file range.");
        }
        const range = response.headers.get("Content-Range");
        if (range && range !== `bytes ${offset}-${offset + length - 1}/${transfer.source.size}`) {
            await response.body?.cancel();
            throw new Error("Colab returned a different file range; the file may have changed.");
        }
        const reader = response.body.getReader();
        const data = new Uint8Array(length);
        let received = 0;
        try {
            while (true) {
                const {done, value} = await reader.read();
                if (done) {
                    break;
                }
                if (received + value.length > length) {
                    throw new Error("Colab returned an oversized file chunk.");
                }
                data.set(value, received);
                received += value.length;
            }
        } finally {
            await reader.cancel();
            reader.releaseLock();
        }
        if (received !== length) {
            throw new Error("Colab returned an incomplete file chunk.");
        }
        let binary = "";
        for (let index = 0; index < data.length; index += 8192) {
            binary += String.fromCharCode(...data.subarray(index, index + 8192));
        }
        transfer.offset += length;
        return {data: btoa(binary)};
    }

    async function finish(transfer) {
        transfer.finalizing = true;
        let warning;
        if (transfer.direction === "download") {
            const current = await metadata(transfer, transfer.path);
            if (!current || current.size !== transfer.source.size
                || current.last_modified !== transfer.source.last_modified
                || transfer.offset !== current.size) {
                throw new Error("Colab source changed during download.");
            }
        } else {
            const staged = await metadata(transfer, transfer.temporary);
            if (!staged || staged.size !== transfer.size || transfer.offset !== transfer.size) {
                throw new Error("The uploaded file size does not match the source.");
            }
            const existing = await metadata(transfer, transfer.path);
            if (existing) {
                if (!transfer.overwrite || existing.type === "directory") {
                    throw new Error("Colab destination already exists.");
                }
                transfer.backup = transfer.temporary + ".backup";
                await rename(transfer, transfer.path, transfer.backup);
            }
            try {
                await rename(transfer, transfer.temporary, transfer.path);
                transfer.temporary = null;
            } catch (error) {
                if (transfer.backup) {
                    try {
                        await rename(transfer, transfer.backup, transfer.path);
                    } catch {
                        throw new Error(`Could not finalize the upload. The original file is preserved at ${transfer.backup}`);
                    }
                    transfer.backup = null;
                }
                throw error;
            }
            if (transfer.backup) {
                try {
                    await remove(transfer, transfer.backup);
                } catch {
                    warning = `Uploaded successfully; the previous file remains at ${transfer.backup}`;
                }
                transfer.backup = null;
            }
        }
        transfers.delete(transfer.transfer_id);
        return {completed: true, ...(warning ? {warning} : {})};
    }

    async function abort(transfer) {
        // Let a final rename finish; interrupting it can strand the previous file.
        if (!transfer.finalizing) {
            transfer.abort.abort();
        }
        try {
            await transfer.pending;
        } catch {
            // The failed operation is reported by its original request.
        }
        transfer.abort = new AbortController();
        try {
            if (transfer.temporary) {
                await remove(transfer, transfer.temporary);
            }
        } finally {
            transfers.delete(transfer.transfer_id);
        }
        return {};
    }

    bridge.files = {
        async request(method, parameters) {
            const transfer = method === "files/begin"
                ? createTransfer(parameters)
                : transfers.get(parameters.transfer_id);
            if (!transfer) {
                if (method === "files/abort") {
                    return {};
                }
                throw new Error("Unknown file transfer.");
            }
            let pending;
            switch (method) {
                case "files/begin":
                    pending = begin(transfer);
                    break;
                case "files/write":
                    pending = write(transfer, parameters);
                    break;
                case "files/read":
                    pending = read(transfer, parameters);
                    break;
                case "files/finish":
                    pending = finish(transfer);
                    break;
                case "files/abort":
                    return abort(transfer);
                default:
                    throw new Error("Unknown file operation.");
            }
            transfer.pending = pending;
            return pending;
        },
        close() {
            for (const transfer of transfers.values()) {
                abort(transfer).catch(error => console.warn("Colab MCP temporary-file cleanup:", error.message));
            }
        },
        get active() {
            return transfers.size > 0;
        },
    };
})();
