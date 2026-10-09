await window.__TAURITAVERN_MAIN_READY__;
const script = await import('/script.js');
const openai = await import('/scripts/openai.js');
const {power_user} = await import('/scripts/power-user.js');
const api = window.__TAURITAVERN__.api;
const deadline = performance.now() + 60000;
while (!script.settingsReady) {
    if (performance.now() > deadline) throw new Error('Application settings are not ready');
    await new Promise(resolve => setTimeout(resolve, 100));
}
const {fixture, applied} = input;
const modelId = applied.model_config.model_name;
const fields = applied.fragments.filter(fragment => fragment.enabled && ['string', 'array'].includes(fragment.field_type));
const moods = applied.moods.filter(mood => mood.enabled);
// Prose, not JSON: Gemma 4 writes tool arguments in its own quoting syntax, and JSON
// examples in a prompt lead it to close argument strings with plain quotes.
const fieldLines = fields.map(fragment => `- ${fragment.id} (${fragment.required ? 'required' : 'optional'}, ${fragment.field_type === 'array' ? 'a list' : 'text'}): ${fragment.description}`);
const moodLines = moods.map(mood => `- ${mood.id}: ${mood.description} When selected: ${mood.prompt_text}`);
// Field order follows Orb's direct_scene schema, whose first property is moods:
// "List of moods to activate. Leave empty for a neutral tone."
const common = applied.settings.shared_system_prompt + '\n\n'
    + 'Every reply follows a scene direction chosen just before it is written. Direction fields:\n'
    + '- moods (required, a list): the mood ids to activate, from the moods below; leave it empty for a neutral tone.\n'
    + fieldLines.join('\n') + '\n'
    + 'Moods. A selected mood\'s instruction applies to the reply.\n'
    + moodLines.join('\n') + '\n\n'
    + 'Replies are safe-for-work prose written as Mara, respecting the visiting surveyor\'s autonomy.';
const wireExtras = {
    cache_prompt: true, timings: true, temperature: 0.8, top_k: 40, top_p: 0.95,
    min_p: 0, repetition_penalty: 1, max_tokens: 4096,
    chat_template_kwargs: {enable_thinking: false, thinking: false},
    reasoning: {effort: 'none', enabled: false}, thinking: {type: 'disabled'},
    stream_options: {include_usage: true},
};
const settings = {
    ...structuredClone(openai.oai_settings),
    chat_completion_source: 'custom', custom_url: 'http://127.0.0.1:5001/v1', custom_model: modelId,
    custom_api_format: 'openai_compat', custom_include_body: JSON.stringify(wireExtras), custom_exclude_body: '',
    additional_parameters_by_source: {custom: {include_body: JSON.stringify(wireExtras), exclude_body: '', include_headers: ''}},
    custom_include_headers: '', openai_max_context: 49152, openai_max_tokens: 4096,
    temp_openai: 0.8, top_p_openai: 0.95, top_k_openai: 40, min_p_openai: 0,
    repetition_penalty_openai: 1, freq_pen_openai: 0, pres_pen_openai: 0,
    stream_openai: true, seed: -1, new_chat_prompt: '', new_example_chat_prompt: '',
    names_behavior: 0, function_calling: true,
};
settings.prompts = [
    {identifier: 'main', name: 'Common task', system_prompt: true, role: 'system', content: common},
    ...['worldInfoBefore', 'charDescription', 'charPersonality', 'scenario', 'personaDescription', 'dialogueExamples', 'chatHistory'].map(identifier => ({identifier, name: identifier, system_prompt: true, marker: true})),
    {identifier: 'agentSystemPrompt', name: 'Stage instructions', system_prompt: true, role: 'user', marker: true, content: ''},
    {identifier: 'agentTask', name: 'Handoff task', system_prompt: true, role: 'user', marker: true, content: ''},
];
settings.prompt_order = [{character_id: 100001, order: settings.prompts.map(prompt => ({identifier: prompt.identifier, enabled: true}))}];
Object.assign(openai.oai_settings, settings);
power_user.persona_description = fixture.persona;
power_user.persona_description_position = 0;
power_user.persona_descriptions[script.user_avatar] = {description: fixture.persona, position: 0};
// The native headless assembler consumes effective setting keys directly.
// Keep both effective and UI-export keys in the customizable saved preset.
const preset = {...openai.getChatCompletionPreset(settings), ...structuredClone(settings)};
const savedPreset = await fetch('/api/presets/save', {method: 'POST', headers: script.getRequestHeaders(), body: JSON.stringify({apiId: 'openai', name: 'Orb benchmark common chat', preset})});
if (!savedPreset.ok) throw new Error('Preset save failed: ' + await savedPreset.text());
const secretResponse = await fetch('/api/secrets/write', {method: 'POST', headers: script.getRequestHeaders(), body: JSON.stringify({key: 'api_key_custom', value: 'local-benchmark-no-auth', label: 'Local benchmark placeholder'})});
if (!secretResponse.ok) throw new Error('Local placeholder credential save failed');
const {id: secretId} = await secretResponse.json();
await api.llmConnections.save({
    schemaVersion: 1, kind: 'tauritavern.llmConnection', id: 'orb-benchmark-gemma', displayName: 'Local benchmark Gemma',
    provider: {chatCompletionSource: 'custom', customApiFormat: 'openai_compat'},
    endpoint: {baseUrl: 'http://127.0.0.1:5001/v1'}, auth: {secretRef: {key: 'api_key_custom', id: secretId, labelSnapshot: 'Local benchmark placeholder'}},
    adapterHints: {customIncludeBody: JSON.stringify(wireExtras)},
});
const servers = await api.mcp.servers.list();
let auditor = servers.servers.find(server => server.displayName === 'Orb benchmark auditor');
if (!auditor) auditor = await api.mcp.servers.create({displayName: 'Orb benchmark auditor', endpoint: 'http://127.0.0.1:5002/mcp', headers: {}, protocolVersion: '2025-03-26'});
await api.mcp.servers.setState({registrationId: auditor.id, state: 'active'});
const discovery = await api.mcp.servers.discover(auditor.id);
await api.mcp.tools.setPermission({registrationId: auditor.id, nativeName: 'audit_draft', permission: 'allow'});
const auditorToolId = `mcp/${auditor.id}:audit_draft`;
const tools = ['workspace.read_file', 'workspace.write_file', 'workspace.apply_patch', 'workspace.commit', 'workspace.finish'].map(name => 'builtin:' + name);
const handoffDescription = {
    description: 'Transfer control to the next stage. agentId is required at the top level: benchmark-writer after the Director, benchmark-editor after the Writer; the Editor has no successor. handoff is required and must contain objective and workspaceRefs.',
    properties: {
        agentId: 'Required. benchmark-writer from the Director, benchmark-editor from the Writer.',
        handoff: 'Required. Contains objective (one sentence) and workspaceRefs (the list of file paths the next stage needs).',
    },
};
const directionFile = 'save it with workspace_write_file to scratch/direction.md as plain text, one field per line, each line being the field name, a colon and the value. '
    + 'Always include the lines moods (the mood ids to activate, left empty for a neutral tone), keywords and next_event; user_intent and detected_repetitions are optional. Separate list items with semicolons. For example:\nmoods: grounded; tense\nkeywords: harbor records; tide chart\nnext_event: Mara finds a new lead.\n';
const writing = 'Write the next roleplay reply as Mara, following the direction\'s next_event and keywords and the instructions of the selected moods. Save it with workspace_write_file to output/main.md; the content is the reply prose only, with no headings, labels or notes.';
const editing = 'Call audit_draft on output/main.md. If the audit reports no issues, call workspace_commit on output/main.md and then workspace_finish. '
    + 'Otherwise read output/main.md with workspace_read_file, then fix every flagged sentence with workspace_apply_patch: old_string is the exact flagged text copied from the file, and new_string is complete replacement text that fits its context. '
    + 'Change only flagged text and keep all other prose exactly as it is. You may send several patches in one response. '
    + 'After each batch of patches, call audit_draft again. Stop editing after three batches even if issues remain. '
    + 'Then call workspace_commit on output/main.md and workspace_finish, giving as the reason whether the last audit was clean or the edit limit was reached. '
    + 'Never rewrite the whole draft with workspace_write_file.';
const definitions = [
    {id: 'benchmark-director', name: 'Benchmark Director', caller: null,
        text: 'You are the Director stage. You never write the reply itself; the Writer stage does. You take exactly two actions.\n'
            + '1. Decide the scene direction for the next reply from the chat, and ' + directionFile
            + '2. Call agent_handoff with agentId benchmark-writer; in handoff, set objective to "Write the next reply following scratch/direction.md." and workspaceRefs to a list containing scratch/direction.md.'},
    {id: 'benchmark-writer', name: 'Benchmark Writer', caller: 'benchmark-director',
        text: 'You are the Writer stage. First read scratch/direction.md with workspace_read_file. Then: ' + writing
            + ' Then call agent_handoff with agentId benchmark-editor; in handoff, set objective to "Audit and repair output/main.md, then commit and finish." and workspaceRefs to a list containing output/main.md. Do not audit, commit or finish.'},
    {id: 'benchmark-editor', name: 'Benchmark Editor', caller: 'benchmark-writer',
        text: 'You are the Editor stage, the last stage. ' + editing},
    {id: 'benchmark-single', name: 'Benchmark Single Profile', caller: null,
        text: 'Complete this whole task in one invocation, in order.\n'
            + '1. Direction: decide the scene direction for the next reply and ' + directionFile
            + '2. Draft: ' + writing + '\n'
            + '3. Edit: ' + editing},
];
const profiles = [];
for (const definition of definitions) {
    const handoff = definition.id !== 'benchmark-single';
    const profile = {
        schemaVersion: 3, kind: 'tauritavern.agentProfile', id: definition.id, displayName: definition.name,
        preset: {mode: 'ref', ref: {apiId: 'openai', name: 'Orb benchmark common chat'}, required: true},
        model: {mode: 'connectionRef', connectionRef: 'orb-benchmark-gemma', modelId},
        run: {presentation: 'foreground', stream: true, directRunnable: !definition.caller, modelRetry: {maxRetries: 3, intervalMs: 3000}},
        context: {initialChatHistoryMessages: -1, includeActivatedWorldInfo: false},
        delegation: {canDelegate: false, canHandoff: handoff, callable: Boolean(definition.caller), allowAsSubagent: false, allowAsHandoffTarget: Boolean(definition.caller), allowNestedDelegation: false, allowedCallers: [definition.caller || definition.id], maxConcurrentInvocations: 1, maxInvocationsPerRun: handoff ? 3 : 1, resultBudgetTokens: 8000, maxHandoffDepth: 2},
        instructions: {agentSystemPrompt: definition.text},
        tools: {allow: [...tools, ...(handoff ? ['builtin:agent.handoff'] : []), auditorToolId], deny: [], toolDescriptions: handoff ? {'builtin:agent.handoff': handoffDescription} : {}, maxRounds: 32, maxCallsPerRun: 80, mcpResultInlineCharLimit: 50000, maxCallsPerTool: {}},
        skills: {visible: ['*'], deny: ['*'], maxReadCharsPerCall: 20000, maxReadCharsPerRun: 80000},
        workspace: {visibleRoots: ['output', 'scratch'], writableRoots: ['output', 'scratch']},
        plan: {mode: 'none', beta: true, nodes: []},
        output: {artifacts: [{id: 'main', path: 'output/main.md', kind: 'markdown', target: 'messageBody', required: true, assemblyOrder: 0}]},
    };
    await api.agent.profiles.save(profile);
    profiles.push({profile: (await api.agent.profiles.load(profile.id)).profile, diagnosis: await api.agent.profiles.diagnose(profile.id)});
}
await api.agent.retention.updateSettings({autoPruneEnabled: false, keepRecentTerminalRuns: 10000, keepFullRecentRuns: 10000});
const card = structuredClone(fixture.card);
delete card.id;
const form = new FormData();
form.append('avatar', new Blob([JSON.stringify(card)], {type: 'application/json'}), 'benchmark-mara.json');
form.append('file_type', 'json');
const imported = await fetch('/api/characters/import', {method: 'POST', headers: script.getRequestHeaders({omitContentType: true}), body: form});
if (!imported.ok) throw new Error('Character import failed: ' + await imported.text());
const importedCard = await imported.json();
await script.getCharacters();
const character = script.characters.findIndex(character => character.avatar === importedCard.file_name || character.name === card.name);
if (character < 0) throw new Error('Imported benchmark character was not found');
await script.selectCharacterById(character);
await script.doNewChat();
script.replaceChatContents(fixture.history.map(row => ({name: row.role === 'user' ? 'User' : card.name, is_user: row.role === 'user', is_system: false, send_date: '2026-10-09T00:00:00Z', mes: row.content})));
await script.saveChat();
await script.saveSettings();
return {
    modelId, preset, profiles, connection: await api.llmConnections.load('orb-benchmark-gemma'),
    discovery, auditor, tools: await api.agent.tools.list(), chatRef: api.chat.current.ref(),
    chat: script.chat, persona: power_user.persona_description,
};
