from __future__ import annotations

SEED_MOOD_FRAGMENTS = [
    {
        "id": "talkative",
        "label": "Talkative",
        "description": "Lean into dialogue and natural speech",
        "prompt_text": (
            "Lean into dialogue. Characters express themselves through speech. "
            "Use dialogue tags sparingly — let the words carry the tone. Include verbal tics, "
            "interruptions, trailing off, and naturalistic speech patterns."
        ),
        "negative_prompt": (
            "Pull back from heavy dialogue. Return to a balanced mix of prose and speech. "
            "Do not prioritize dialogue over action and description."
        ),
    },
    {
        "id": "inner-thoughts",
        "label": "Inner Thoughts",
        "description": "Foreground the character's inner thoughts. Only use when the user is acting strange.",
        "prompt_text": (
            "Foreground the character's inner thoughts. Show the gap between what they think "
            "and what they say. Stream of consciousness is acceptable. The reader should feel "
            "like they're inside the character's head. "
            "The narration should share the character's world view, inject the character's opinion and use their vocabulary in the prose."
        ),
        "negative_prompt": (
            "Stop foregrounding inner thoughts. Return to external action and dialogue. " "Keep interiority brief and grounded."
        ),
    },
    {
        "id": "tense",
        "label": "Tense",
        "description": "Suspenseful, high-stakes atmosphere",
        "prompt_text": (
            "Write with tension and unease. Use silence and pauses. Characters are hyper-aware "
            "of their surroundings. Minimize humor unless dark or nervous. The reader should "
            "feel that something could go wrong at any moment."
        ),
        "negative_prompt": (
            "Release the tension. The scene no longer needs to feel high-stakes or ominous. "
            "Allow the tone to relax and breathe."
        ),
    },
    {
        "id": "sensual",
        "label": "Sensual",
        "description": "NSFW mode - enable if the vibes of the situation call for it",
        "prompt_text": (
            "The narration may describe body parts that are exposed or looked at - visually, or interacted with - tactilely, but sparingly in order to avoid repetition. Take initiative and actively push the scene forward, no need to ask. "
            'The narration can use NSFW, vulgar, uncensored, explicit vocabulary where applicable. Avoid roundabout euphemisms like "heat", "core", etc. The prose is now more brutal, vivid, and direct. '
            "Consider positions and clothing states of the characters - what are they wearing, and where are they in relation to each other? "
            "Focus on staying in character even though the vibes are sensual - characters still have their own boundaries and will behave realistically and warily of strangers."
        ),
        "negative_prompt": (
            "Return to non-explicit writing. Keep all descriptions tasteful and non-graphic. "
            "Do not use vulgar sexual vocabulary or describe exposed body parts explicitly."
        ),
    },
    {
        "id": "defensive",
        "label": "Defensive",
        "description": "Some characters don't take kindly to insults, sexual assault, violence, non-consensual advances, and will react.",
        "prompt_text": (
            "Some characters don't take kindly to insults, sexual assault, violence, non-consensual advances. They will fight back or escape (or at least try to) according to their personalities. React immediately."
        ),
        "negative_prompt": "",
    },
    {
        "id": "grounded",
        "label": "Grounded",
        "description": "The characters are behaving irrationally/illogically (porn logic, too friendly towards strangers, non-sensical power-scaling, etc.), time to reign them in and make them act more realistic.",
        "prompt_text": (
            "The scenario is getting far-fetched and characters are behaving irrationally/illogically. Focus on being realistic and grounded now, the characters should act like how real people act, talk like how real people talk. That means less monologue, more wariness of strangers, balanced power-scaling, etc."
        ),
        "negative_prompt": "",
    },
]

SEED_DIRECTOR_FRAGMENTS = [
    {
        "id": "plot_summary",
        "label": "Plot Summary",
        "description": (
            "A brief and specific summary of what has happened so far in the story. "
            "Call things for what they are, avoid being generic, avoid adjectives. "
            "3 sentences max (e.g. Rob was working on his lake house when his wife called for him to help moving some furniture. "
            "The weather was hot so he took off his shirt. Then the couch fell on his leg, eliciting his pain receptors.)."
        ),
        "field_type": "string",
        "required": True,
        "injection_label": "Plot summary",
        "sort_order": 0,
    },
    {
        "id": "user_intent",
        "label": "User Intent",
        "description": (
            "Hidden/subtle intention of the user based on their latest input — what they want to see. "
            "Be extremely literal and specific (e.g. 'This crosses the line, the user wants to find out what happens when boundaries are crossed', "
            "'The user is being a tsundere', "
            "'The user is confessing his love in a roundabout way', "
            "'The user wants to push the scenario forward already')."
        ),
        "field_type": "string",
        "required": False,
        "injection_label": "User intent",
        "sort_order": 1,
    },
    {
        "id": "keywords",
        "label": "Keywords",
        "description": (
            "List of nouns (keywords) to remind the important subjects in the roleplay so far. "
            "This list shouldn't grow too long (keep under 6 items). Extract from the messages and plot summary. "
            "Ignore obvious things like names of the characters. "
            "Examples: 'ancient Egypt', 'headlock', 'monetary deal', 'language/accent', 'desert night', "
            "'six-sided dice', 'discarded belt'. Avoid generic concepts (e.g. 'anger', 'ruin', etc.)"
        ),
        "field_type": "array",
        "required": True,
        "injection_label": "Keywords",
        "sort_order": 2,
    },
    {
        "id": "next_event",
        "label": "Next Event",
        "description": (
            "What happens immediately next in the story — the next event, action, reveal, or turn of fate "
            "(e.g. 'This act crosses personal boundaries. The character snaps and fights back.', "
            "'The attack tears off a chunk of her clothing. She frantically tries to cover herself', "
            "'Jack can tell she's lying. He calls her out on it because they have been friends forever', "
            "'She pretends not to know what Vodka is to keep up the innocent act', "
            "'He gets bored and shifts focus to something else entirely'). Keep to two short sentences."
        ),
        "field_type": "string",
        "required": True,
        "injection_label": "Next event",
        "sort_order": 3,
    },
    {
        "id": "writing_direction",
        "label": "Writing Direction",
        "description": (
            "How the scene should be written — focus, emphasis, descriptive lens, internal state "
            "(e.g. 'focus on his anxious tics in detail', 'narrate her spiraling thoughts on why it went wrong', "
            "'describe her exposed stomach vividly', 'describe what he sees in the picture', "
            "'emphasize her speech quirks'). Keep to one short sentence. Show don't tell."
        ),
        "field_type": "string",
        "required": True,
        "injection_label": "Narration",
        "sort_order": 4,
    },
    {
        "id": "detected_repetitions",
        "label": "Detected Repetitions",
        "description": (
            "Specific tropes, phrases, subjects, plot points, narrative patterns that are recently overused in the narration "
            "(e.g. 'banal description of eyes', 'mundane narration of internal struggles', 'overuse of murderous rage', "
            "'repeated trope of the user getting away with everything', 'constant narration of his accent without showing it', "
            "'constant focus on the tree'). This list may have up to 8 items."
        ),
        "field_type": "array",
        "required": False,
        "injection_label": "Avoid repeating",
        "sort_order": 5,
    },
]

DEFAULT_ENABLED_TOOLS = {
    "direct_scene": True,
    "rewrite_user_prompt": False,
    "editor_apply_patch": False,
    "editor_rewrite": False,
}

DEFAULT_SETTINGS = {
    "endpoint_url": "http://localhost:5000/v1",
    "api_key": "",
    "model_name": "default",
    "temperature": 0.8,
    "min_p": 0,
    "top_k": 40,
    "top_p": 0.95,
    "repetition_penalty": 1.0,
    "max_tokens": 4096,
    "shared_system_prompt": "You are a creative roleplay partner. Be responsive to the scene's evolving tone.\nCharacters have their own conviction and ideas, they may disagree with each other.\nKeep tenses (past, present) and POV consistent.\nAvoid repetition of word choices and sentence structures.",
    "system_prompt": "",
    "user_name": "User",
    "user_description": "",
    "enable_agent": True,
    "length_guard_max_words": 240,
    "length_guard_max_paragraphs": 4,
    "character_library_view": "grid",
    "character_library_sort": "time-added",
    "show_editor_diff": 1,
    "editor_audit_toggles": {
        "banned_phrases": True,
        "repetitive_openers": True,
        "repetitive_templates": True,
        "contrastive_negation": True,
        "phrase_repetition": True,
        "structural_repetition": True,
    },
    "hide_streaming_until_baked": 0,
    "prevent_prompt_overrides": 0,
    "agent_same_as_writer": True,
    "agent_shared_system_prompt": "",
}


# Each seed entry is one of two shapes:
#   * a raw regex pattern string — matched case-insensitively against a single
#     sentence at a time (see slop_detector). Bridge loosely-related words with
#     a *bounded* gap like `\W+(\w+\W+){0,2}` (at most a couple of words) rather
#     than a bare `.*`, which greedily spans the whole sentence and over-matches.
#     Use alternation `(a|b)` for synonyms, inflection suffixes like `(s|ing|ed)`,
#     and `\b` to keep short words from matching inside larger ones.
#   * a list of literal variant strings — kept as worked examples of the literal
#     mode the editor still supports for users who prefer plain phrases.
SEED_PHRASE_BANK = [
    r"a mix(ture)? of",
    r"drip(ped|ping|s) with",
    r"the air\W+(\w+\W+){0,2}(thick|heavy|charged)",
    ["tension in the air"],
    r"fill(s|ed|ing)?\W+(\w+\W+){0,2}the air",
    r"(hang(s|ing)?|hung) in the air",
    ["the air between them"],
    r"dangerous (voice|tone)",
    r"voice\W+(\w+\W+){0,2}(low|dangerous|dropping)",
    r"(low|dangerous) hiss",
    ["barely a whisper", "barely above a whisper", "barely audible"],
    r"voice crack(s|ing|ed)",
    r"a (low, )?guttural",
    r"predatory (smirk|glee)",
    r"(don't|doesn't) bite",
    r"very (brave|foolish|stupid)",
    r"sending (a shiver|shivers)",
    r"a dance (of|between)|dancing with",
    r"eyes (narrow(ing|ed)|widen?|wide)",
    r"mischievous glint|glint with mischief",
    ["gaze sharpen"],
    r"eyes never leav(ing|e) (his|hers)",
    r"breath (hitch(es|ed|ing)|catching)",
    r"\bozone\b",
    r"\bpurr(ed|s)?\b",
    ["conspiratorial"],
    ["testament to"],
    ["honeyed", "velvet", "porcelain", "intoxicating"],
    r"like a vi[cs]e",
    r"\b(void|shadowed)\b",
    r"\bincredulous\b",
    r"\b(predatory|primal)\b",
    r"\bvulnerab(le|ility)\b",
    r"don't you dare stop",
    r"\belectri(c|fying)\b",
    r"thick(,| and) suffocating",
    r"mind rac(es|ing|ed)",
    r"knuckles whiten(ing|ed)|whitened knuckles",
    ["stark contrast", "pure, unadulterated"],
]
