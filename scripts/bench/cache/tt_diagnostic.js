await window.__TAURITAVERN_MAIN_READY__;
await window.__TAURITAVERN__.api.dev.frontendLogs.setConsoleCaptureEnabled(true);
await new Promise(resolve => setTimeout(resolve, 3000));
return {
    url: location.href,
    readyState: document.readyState,
    startupStage: globalThis.__TAURITAVERN_STARTUP_STAGE__ ?? null,
    body: document.body.innerText.slice(0, 4000),
    frontendLogs: await window.__TAURITAVERN__.api.dev.frontendLogs.list({limit: 100}),
    backendLogMethods: Object.keys(window.__TAURITAVERN__.api.dev.backendLogs),
    scriptResources: performance.getEntriesByType('resource').filter(entry => entry.name.includes('script.js')).map(entry => ({name: entry.name, duration: entry.duration})),
};
