import { parentPort, workerData } from "node:worker_threads";

globalThis.WorkerGlobalScope = class {};
globalThis.self = new WorkerGlobalScope();
self.addEventListener = (_, listener) => parentPort.on("message", (data) => listener({ data }));
self.postMessage = (data) => parentPort.postMessage(data);
await import(workerData);
