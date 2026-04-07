# Agentic Roleplay Frontend — Design Document

## Problem Statement

LLMs suffer from stylistic inertia in long roleplay sessions. Once a tone, pacing, or prose style is established over several turns, the model tends to perpetuate it regardless of narrative shifts. A lighthearted conversation that turns tragic will often retain the cadence and vocabulary of the earlier tone because the weight of prior context anchors the model's generation.

Static system prompts cannot solve this. The system prompt is written once and does not adapt to evolving scenes.

## Solution Overview

An **agentic middleware layer** sits between the user and the model. It intercepts each user message, runs a short analytical pass to "read the room," then dynamically assembles prompt directives that shape the model's writing before the actual roleplay generation happens.

The user never sees the agentic layer. The writer model doesn't know it's being directed. The result is a roleplay session that naturally adapts its style, tone, and pacing as the narrative evolves.

## Architecture

### Single-Model, Two-Pass Design

Both the agent (director) and the writer use the same model. This simplifies deployment and takes advantage of KV cache sharing — both passes share the same static prefix (system prompt + conversation history), so the cached key-value pairs from the prefix are computed once and reused.

### Per-Turn Flow

Step 1: User sends a new message.

Step 2 (Agent Pass): The system intercepts the message. It constructs a request using the full cached conversation prefix, but replaces the user's message with an out-of-character (OOC) agent prompt. This prompt instructs the model to act as a scene director and output structured tool calls — not roleplay prose. The model analyzes recent context and decides which writing styles should be active and what internal scene notes to set. The agent's output is consumed by the frontend and then **discarded** — it is never appended to the conversation history.

Step 3 (State Mutation): The frontend processes the agent's tool calls. It updates a behavior registry (which styles are active) and a scene note store (persistent and momentary notes). It then assembles a block of directive text from the active prompt fragments.

Step 4 (Writer Pass): The system constructs the real request. It uses the same cached prefix, but now appends the assembled directive block followed by the user's original message. The model generates the roleplay response. This output **is** appended to the conversation history.

Step 5: Response is delivered to the user. The conversation history now contains only the user's original message and the writer's response — no traces of the agent pass or the injected directives.

### Prompt Layout and KV Cache Strategy

The fundamental rule: any token that changes invalidates the cache for itself and every token after it. Therefore, all dynamic content must live at the tail end of the prompt, after all cached content.

```
STATIC ZONE (cached, never modified between turns):
  [system] Character card, world description, base rules
  [assistant] Turn 1 response
  [user] Turn 2 message
  [assistant] Turn 3 response
  ...
  [assistant] Turn N-1 response

DYNAMIC ZONE (rebuilt every turn, never cached):
  [system or injected message] Assembled behavior fragments + scene notes
  [user] Turn N message (the new message)
```

The "depth" terminology:
- Depth infinity = the system prompt (top of context, never touched)
- Depth 2+ = older conversation turns (cached)
- Depth 1 = the most recent cached assistant response
- Depth 0.5 = the injected directive block (rebuilt every turn)
- Depth 0 = the new user message

Fragments **cannot** go in the system prompt. Adding or removing a sentence there would invalidate the entire KV cache. The depth-0.5 injection point is the only place where dynamic directives belong.

## Behavior Fragment System

### What Fragments Are

Fragments are short, modular prompt instructions that each target a specific aspect of writing style. They are stored in a library and toggled on/off by the agent. When active, their text is concatenated into the depth-0.5 injection block.

### Writing Style Fragments (Initial Set)

**Descriptive** — Prioritize environmental and sensory detail. Describe spaces, lighting, textures, sounds, and smells. Ground the reader in the physical world. Actions should include how things feel and look, not just what happens.

**Talkative / Dialogue-Heavy** — Lean into dialogue. Characters express themselves through speech. Use dialogue tags sparingly — let the words carry the tone. Include verbal tics, interruptions, trailing off, and naturalistic speech patterns.

**Theatre Play** — Write as if this is a stage play. Favor dialogue and brief stage-direction-style action descriptions. Minimal internal monologue. Characters reveal themselves through what they say and do, not what they think.

**Internal Monologue** — Foreground the character's inner thoughts. Show the gap between what they think and what they say. Stream of consciousness is acceptable. The reader should feel like they're inside the character's head.

Additional fragments can be added to the library over time. The system is extensible — the agent selects from whatever fragments are available.

### Fragment Format in the Injection Block

```xml
<current_scene_direction>
  <style name="descriptive">
    Prioritize environmental and sensory detail. Describe spaces, lighting,
    textures, sounds, and smells. Ground the reader in the physical world.
  </style>
  <style name="tense">
    Write with short, clipped sentences. Use silence and pauses. Characters
    are hyper-aware of their surroundings. Minimize humor unless dark or nervous.
  </style>
  <persistent_note>Kael has chosen the number 42 — maintain this consistently.</persistent_note>
  <momentary_note>Kael is acting smug because the player's last guess was wildly off.</momentary_note>
</current_scene_direction>
```

XML tagging is used because Anthropic models respond well to structured XML directives, and it visually separates the injection from conversational content.

## Scene Notes

### Purpose

Scene notes capture what the model **cannot derive on its own** from the visible conversation history. They encode hidden intentionality, subtext, precommitted decisions, and internal character state that isn't expressed in dialogue or narration.

### The Test for a Good Scene Note

If the model could figure it out by reading the conversation, it doesn't belong in the note. Good scene notes answer: "What is true in this scene that isn't visible in the text?"

Good examples:
- "Kael has already decided to betray the group but hasn't shown it yet — his helpfulness is performative."
- "She knows the answer but is asking questions to test whether he'll lie."
- "Character has chosen the number 42 — maintain this consistently regardless of guesses."
- "He is attracted to this person but resents that he is. Play against the dialogue."

Bad examples:
- "They are discussing the plan to enter the castle." (Plot summary — visible in conversation.)
- "The mood is tense." (Style tags handle this — not a scene note concern.)
- "Character A is talking to Character B." (Obvious from context.)

### Two-Layer Note Structure

**Persistent layer** — facts that should survive across many turns until explicitly invalidated. Examples: a chosen number in a guessing game, a long-term secret, a hidden motive, a character's true allegiance. This layer only changes when an event in the narrative makes it obsolete (e.g., the secret is revealed, the number is guessed correctly).

**Momentary layer** — observations that are true right now but will naturally expire or shift within a few turns. Examples: currently suppressing anger, about to change the subject, testing the other person's reaction, physically in pain from a recent injury.

Separating these prevents the failure mode where the agent refreshes the momentary note and accidentally drops persistent state (like the chosen number) because it forgot to re-include it. The persistent layer is only touched by explicit update/remove operations.

### Update Logic

The agent receives the current persistent and momentary notes as input alongside the conversation context. It then decides:

**Preserve persistent note** — when the hidden information is still hidden, the character's internal state hasn't been challenged, the secret/decision/number is still in play.

**Update persistent note** — when a secret is revealed (remove it), a character makes a new internal decision (add it), a precommitted value is resolved (remove it), or the scene shifts enough that a motive changes.

**Refresh momentary note** — freely rewritten each turn to reflect the character's current-moment emotional or strategic state, without worrying about dropping persistent state.

## Instruction Authority at Depth 0.5

Late-injected instructions can be outweighed by the established pattern in conversation history. If the last 15 assistant turns were verbose and purple, a small fragment saying "be terse" might not overcome the inertia.

### Mitigation Strategies

**Write fragments with authority.** Not "you may consider being more terse" but "Your writing style has NOW shifted — use short, clipped prose. This overrides your previous tendencies." Fragments must be assertive because they are fighting against pattern momentum.

**Use XML structure.** The `<current_scene_direction>` wrapper signals to the model that this is a distinct directive, not incidental conversational context.

## Agent Tool Schema

### Tools Available to the Agent

**set_writing_styles**
- Input: a list of style IDs to activate (e.g., ["descriptive", "tense"])
- Behavior: replaces the full set of active styles. Any style not in the list is deactivated. The frontend resolves each ID to its fragment text.

**update_persistent_note**
- Input: action (one of "keep", "append", "remove", "replace") and content (text)
- "keep" = no change, carry forward as-is
- "append" = add a new entry to the persistent layer
- "remove" = remove a specific entry by content match or index
- "replace" = overwrite the entire persistent layer

**set_momentary_note**
- Input: content (text, or null to clear)
- Behavior: replaces the momentary note entirely. This is expected to change most turns.

## Agent Prompt

The agent prompt replaces the user's message during the agent pass. It is an OOC instruction that transforms the model from a roleplay writer into a scene director.

```
[Out of Character — Scene Direction Mode]

You are now acting as the scene director, not as a character. Analyze the
recent conversation and determine how the next response should be styled.

Currently active styles: {{list of currently active style IDs, or "none"}}
Current persistent note: {{current persistent note text, or "none"}}
Current momentary note: {{current momentary note text, or "none"}}

The user's latest message (which you are analyzing, not responding to):
"""
{{user's actual message}}
"""

Available writing styles: {{list of all style IDs with one-line descriptions}}

Your task:
1. Consider what has just happened in the scene. Has the emotional tone shifted?
   Has the pacing changed? Is a new kind of scene beginning?
2. Decide which writing styles should be active for the NEXT response.
   Only change styles if the scene warrants it — don't churn for no reason.
3. Evaluate the persistent note. Does any hidden state need to be added,
   removed, or updated? If the character has made a secret decision, committed
   to a hidden value, or has private knowledge the reader shouldn't see yet,
   that belongs here. If nothing has changed, keep it.
4. Write a momentary note capturing the character's current internal state —
   what they're feeling or planning RIGHT NOW that isn't visible in the text.
   If there's nothing notable, set it to null.

Respond ONLY with tool calls. Do not write any prose, narration, or dialogue.
```

