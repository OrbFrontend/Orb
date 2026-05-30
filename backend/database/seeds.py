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
    "hide_streaming_until_baked": 0,
    "prevent_prompt_overrides": 0,
    "agent_same_as_writer": True,
    "agent_shared_system_prompt": "",
}

SEED_PHRASE_BANK = [
    ["a mix of", "a mixture of"],
    ["dripped with", "dripping with", "drips with"],
    [
        "the air was heavy",
        "the air is heavy",
        "the air was charged",
        "the air is charged",
        "the air was thick",
        "the air is thick",
    ],
    ["tension in the air"],
    ["filling the air", "fills the air", "filled the air"],
    [
        "hang in the air",
        "hung in the air",
        "hangs in the air",
        "hanging in the air",
        "the air between them",
    ],
    ["dangerous voice", "dangerous tone"],
    [
        "voice dropping",
        "voice low",
        "voice dangerous",
        "voice a dangerous",
        "voice a low",
        "voice is a low",
        "voice is a dangerous",
    ],
    ["low hiss", "dangerous hiss"],
    ["barely a whisper", "barely above a whisper", "barely audible"],
    ["voice cracks", "voice cracking", "voice cracked"],
    ["a low, guttural", "a guttural sound"],
    [
        "a predatory smirk",
        "I don't bite",
        "they don't bite",
        "it doesn't bite",
        "predatory glee",
    ],
    [
        "very brave or very stupid",
        "either very brave or very foolish",
        "brave or stupid",
    ],
    ["sending shivers", "sending a shiver"],
    ["a dance of", "a dance between", "dancing with"],
    [
        "eyes narrowing",
        "eyes narrowed",
        "mischievous glint",
        "glint with mischief",
        "gaze sharpen",
        "eyes widen",
        "eyes wide",
    ],
    [
        "eyes never leaving his",
        "eyes never leaving hers",
        "eyes never leave his",
        "eyes never leave hers",
    ],
    ["breath hitches", "breath hitched", "breath hitching", "breath catching"],
    ["ozone"],
    ["purr", "purred", "purrs"],
    ["conspiratorial"],
    ["testament to"],
    ["honeyed", "velvet", "porcelain", "intoxicating"],
    ["like a vice", "like a vise"],
    ["void", "shadowed"],
    ["incredulous"],
    ["predatory", "primal"],
    ["vulnerability", "vulnerable"],
    ["don't you dare stop"],
    ["electric", "electrifying"],
    ["thick and suffocating", "thick, suffocating"],
    ["mind races", "mind racing", "mind raced"],
    ["knuckles whitening", "knuckles whitened", "whitened knuckles"],
    ["stark contrast", "pure, unadulterated"],
]
