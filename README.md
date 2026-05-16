# Orb - Agentic RP Frontend

![Orb](Orb.png)
## Problem Statement

LLMs suffer from stylistic inertia in long roleplay sessions. Once a tone, pacing, or prose style is established over several turns, the model tends to perpetuate it regardless of narrative shifts. A lighthearted conversation that turns tragic will often retain the cadence and vocabulary of the earlier tone because the weight of prior context anchors the model's generation.

Static system prompts cannot solve this. The system prompt is written once and does not adapt to evolving scenes.

## Solution Overview

An **agentic middleware layer** sits between the user and the model. It intercepts each user message, runs a short analytical pass to "read the room," then dynamically assembles prompt directives that shape the model's writing before the actual roleplay generation happens.

The user never sees the agentic layer. The writer model doesn't know it's being directed. The result is a roleplay session that naturally adapts its style, tone, and pacing as the narrative evolves.

## Notable Features
1. **Clear direction for Writer**: Grounding the story + actively steering the writing style = better output
2. **Customizability**: Customizable prompt injection that's automatically used by Director model
3. **Anti-slop**: Get rid of overused words, phrases, and patterns often seen in LLM outputs
4. **Length Guard**: Actively or passively protect from length degradation as context grows
5. **Super-regenerate**: Normal regens may give samey outputs, ask for a different take
6. **Magic Rewrite**: Rewrite the target message in a user-defined direction
7. **Compress History**: Summarize chat context and move it to a new conversation
8. **Mobile-compatibility**: UI for mobile devices
9. **Integrated TTS**: Easy Text-to-speech that supports multiple providers

## Architecture

### Three-Pass Design

The system uses a three-pass architecture, with the agent and writer optionally being the same or different models:

1. **Director Pass** - Tool-calling phase where the LLM selects moods, plot direction, and potentially rewrites user prompts
2. **Writer Pass** - Story generation phase where the LLM writes the actual roleplay response
3. **Editor Pass** - A ReAct loop - Self-audit for slop and length optimization phase. This is surgical, errors will be programmatically detected, 
the model only needs to write replacement for targeted sentences

### KV Cache Reuse Strategy

For optimal KV cache reuse, the following will remain consistent across passes:

#### 1. System Prompt
- The system prompt (character card, instructions, etc.) is identical across all passes
- Built once and reused forever
- Includes character description, scenario, example dialogue, and additional instructions

#### 2. Chat History
- The conversation history (previous messages) is identical across all passes
- Maintains exact same message content and ordering

#### 3. Tool Schemas
- The same tool definitions must be sent in each LLM call for kv cache reuse
- Tool schemas affect the model's internal representation
- Inconsistent tool schemas break KV cache alignment

## Design Principles

1. Prioritize small models - if a feature fails half of the time on Gemma-4-26B4A, it will be scrapped
2. Only use agentic functionalities when absolutely needed - we will not have useless tools like `dice_roll`
3. Scanning should be algorithmic, avoid making LLMs eyeball for errors
4. Keep agentic scope small, avoid giving the agent too much freedom of choice

## Drawbacks

1. **Speed**: Multiple passes will obviously have a longer time to final response
2. **Cost**: Neligible cost increase, which comes naturally with multiple passes, somewhat alleviated by KV cache reuse strategy

## Requirements
1. A model with solid tool/function calling capabilities (recommended: Gemma 4)
2. OpenAI-compatible LLM inference backend API that supports prompt-caching
3. Python 3.9+

## Wiki

Full documentation is at **https://orbfrontend.github.io/Orb/**

## Contributing & Discussions

Read this before opening a PR: https://github.com/OrbFrontend/Orb/blob/main/CONTRIBUTING.md

Ideas, help requests, and questions go here: https://github.com/OrbFrontend/Orb/discussions
