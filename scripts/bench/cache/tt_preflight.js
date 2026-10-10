await window.__TAURITAVERN_MAIN_READY__;
const script = await import('/script.js');
const deadline = performance.now() + 60000;
let ready = false;
while (!ready) {
    if (performance.now() >= deadline) throw new Error('Legacy app settings did not become ready');
    await new Promise(resolve => setTimeout(resolve, 100));
    const onboarding = [...document.querySelectorAll('dialog[open]')].find(dialog => dialog.innerText.includes('Persona Name:'));
    if (onboarding) {
        const field = onboarding.querySelector('textarea, input[type="text"]');
        if (!field) throw new Error('Onboarding persona input missing');
        field.value = 'User';
        field.dispatchEvent(new Event('input', {bubbles: true}));
        onboarding.querySelector('.popup-button-ok').click();
    }
    try { ready = script.settingsReady; } catch (error) {
        if (!(error instanceof ReferenceError)) throw error;
    }
}
return {
    url: location.href,
    api: Object.keys(window.__TAURITAVERN__.api),
    settingsReady: script.settingsReady,
    mainApi: script.main_api,
    characters: script.characters.map(character => ({name: character.name, avatar: character.avatar})),
    body: document.body.innerText.slice(0, 2000),
    profiles: await window.__TAURITAVERN__.api.agent.profiles.list(),
    tools: await window.__TAURITAVERN__.api.agent.tools.list(),
};
