# Orb - Agentic RP Frontend

[![CodeQL](https://github.com/OrbFrontend/Orb/actions/workflows/codeql.yml/badge.svg)](https://github.com/OrbFrontend/Orb/actions/workflows/codeql.yml)

![Orb](Orb.png)
## Problem Statement

LLM Roleplaying and Creative Writing have a low floor and a high ceiling. Common problems: passiveness and directionlessness, slop (overused, cliche word choices), various types of repetition (degradation as context grows), writing style inertia.

## Solution Overview

A **Director** sits between the user and the model. It intercepts each user message, runs a short analytical pass to "read the room," then dynamically assembles prompt directives that shape the model's writing before the actual roleplay generation happens.

We essentially break the RP task into smaller, more focused tasks before the final response is generated.

An **Editor** audits the LLM's response then surgically fixes it.

## Notable Features
1. **Director**: Grounding the story + actively steering the writing style = better output
2. **Customizability**: Customizable prompt injection that's automatically used by Director model
3. **Anti-slop**: Get rid of overused words, phrases, and patterns often seen in LLM outputs
4. **Anti-repetition**: Detect various types of repetition from outputs and surgically fix them
5. **Length Guard**: Actively or passively protect from length degradation as context grows
6. **Super-regenerate**: Normal regens may give samey outputs, ask for a different take (mileage varies)
7. **Magic Rewrite**: Rewrite the target message in a user-defined direction
8. **Compress History**: Summarize chat context and move it to a new conversation
9. **Mobile-compatibility**: UI for mobile devices
10. **TTS**: Easy Text-to-speech that supports multiple providers
11. **Character Browser**: Fetch character cards from various sites on the Internet
12. **AI Feedback**: Give suggestions and commentary on what to do next, solving writer's block
13. **Text Completion**: Advanced harness optimizations when an endpoint supports raw text completion
14. **Assisted Document Mode**: A version of Mikupad where you don't need to worry about special tokens

## Architecture

### Three-Pass Design

The system uses a three-pass architecture, with the agent and writer optionally being the same or different models:

1. **Director Pass** - Tool-calling phase where the LLM selects moods, plot direction, and potentially rewrites user prompts
2. **Writer Pass** - Story generation phase where the LLM writes the actual roleplay response
3. **Editor Pass** - A ReAct loop - Self-audit for slop and length optimization phase. This is surgical, errors will be programmatically detected, 
the model only needs to write replacement for targeted sentences

### Single and Dual Model Modes

In most local setups, the user doesn't have enough resource to load more than one model at a time. Single-Model Mode addresses this by using the same model for both writing and agentic tasks. KV cache is respected by design so prompt reprocessing is avoided.

For the best experience, use Dual-Model Mode. Some harnesses are dropped in this mode so the models should perform better.

### KV Cache Reuse Strategy

For optimal KV cache reuse, the following will remain consistent across passes:

#### 1. System Prompt
- The system prompt (character card, instructions, etc.) is identical across all passes
- Built once and reused forever
- Includes character description, scenario, example dialogue, and additional instructions

#### 2. Chat History
- The conversation history (previous messages) is identical across all passes
- Maintains exact same message content, attachments, and ordering

#### 3. Tool Schemas
- The same tool definitions must be sent in each LLM call for kv cache reuse
- Inconsistent tool schemas break KV cache alignment

For a stepped visual walkthrough of the cache mechanism across all three passes and the reasoning-mode fork, open [kv-cache-animation](https://orbfrontend.github.io/Orb/architecture/kv-cache-animation.html) in a browser. The full write-up is in [docs/architecture/kv-cache.md](docs/architecture/kv-cache.md).

## Design Principles

1. Prioritize small models - if a feature fails half of the time on Gemma-4-26B4A, it probably doesn't belong here
2. Only use agentic functionalities when absolutely needed - we will not have useless tools like `dice_roll`
3. Algorithm-first - if something can be done with an algorithm, don't use LLMs. Avoid making LLMs eyeball for errors
4. Keep agentic scope small to reduce hallucination, avoid giving agents too much freedom of choice

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

Check this out before opening a PR: https://github.com/OrbFrontend/Orb/blob/main/CONTRIBUTING.md

Ideas, help requests, and questions go here: https://github.com/OrbFrontend/Orb/discussions
