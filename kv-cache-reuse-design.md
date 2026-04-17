# KV Cache Reuse Design for Agentic Roleplay

## Overview

This document explains how the agentic roleplay system optimizes LLM inference by reusing KV (Key-Value) cache across multiple passes. KV cache reuse significantly reduces latency and computational cost when making sequential LLM calls with overlapping context.

## The Three-Pass Architecture

The system uses a three-pass architecture for each user message:

1. **Director Pass** - Tool-calling phase where the LLM selects moods, plot direction, and potentially rewrites user prompts
2. **Writer Pass** - Story generation phase where the LLM writes the actual roleplay response
3. **Refine Pass** - Optional self-audit and length optimization phase

## KV Cache Reuse Requirements

For optimal KV cache reuse, the following must remain consistent across passes:

### 1. System Prompt
- The system prompt (character card, instructions, etc.) is identical across all passes
- Built once using `build_prefix()` and reused
- Includes character description, scenario, example dialogue, and additional instructions

### 2. Chat History
- The conversation history (previous messages) is identical across all passes
- Processed through `build_prefix()` with placeholders replaced
- Maintains exact same message content and ordering

### 3. Tool Schemas
- **CRITICAL**: The same tool definitions must be sent in each LLM call for kv cache reuse
- Tool schemas affect the model's internal representation
- Inconsistent tool schemas break KV cache alignment

## Implementation Details

### Tool Inclusion Strategy

The system maintains a consistent set of enabled tools across all passes for KV cache consistency.

### Pre-Writer and Post-Writer Tool Sets

Two sets classify tools by which pass owns them. Both sets are sent in **all three API calls** (identical schemas = KV cache reuse); the sets only control which tools are actually *called*:

- **`PRE_WRITER_TOOLS`** (`rewrite_user_prompt`): Called only in the director pass. Writer suppresses all tool calls via `tool_choice=none`; refine forces its own tool choice, so `rewrite_user_prompt` is never invoked there.
- **`POST_WRITER_TOOLS`** (`editor_apply_patch`, `editor_rewrite`): Called only in the refine pass. The director filters them out of the tools it can actually call; writer uses `tool_choice=none`.

### Critical Distinction: Tools in API vs Tools Called

There's an important distinction between:
- **Tools included in the inference API call**: All enabled tool schemas are sent for KV cache consistency
- **Tools actually called in agent pass**: Only specific tools are invoked based on `tool_choice` and agent logic

### Three-Pass Tool Consistency

1. **Director Pass (Agent Pass)**:
   - **API tools**: All enabled tools (`["direct_scene", "rewrite_user_prompt", "editor_apply_patch", "editor_rewrite"]`)
   - **Agent-called tools**: Only tools not in `POST_WRITER_TOOLS` (excludes `editor_apply_patch`, `editor_rewrite`)
   - **Tool choice**: Sequential calls with specific `tool_choice`:
     - First: `{'type': 'function', 'function': {'name': 'rewrite_user_prompt'}}` (if enabled)
     - Second: `{'type': 'function', 'function': {'name': 'direct_scene'}}`
   - **Purpose**: Scene direction and optional prompt rewriting (rewrite runs first so users can stop early if they don't like the rewritten message)

2. **Writer Pass**:
   - **API tools**: All enabled tools — identical to director (`["direct_scene", "rewrite_user_prompt", "editor_apply_patch", "editor_rewrite"]`)
   - **Tool choice**: `none` (prevents all tool calling during story generation)
   - **Purpose**: Generate the actual roleplay response

3. **Refine Pass**:
   - **API tools**: All enabled tools — identical to director and writer (KV cache reuse)
   - **Tool choice**: `auto` or specific refine tool based on context
   - **Purpose**: Self-audit and length optimization

### Conditional Prompt Instruction (not tool schema)

The `editor_rewrite` tool follows the same design as `editor_apply_patch`:
- Always included in the schema set when its feature is enabled (length guard → `editor_rewrite`, audit → `editor_apply_patch`)
- The orchestrator sets `enabled_tools["editor_rewrite"] = True` whenever `length_guard` is enabled, so `enabled_schemas()` includes it for all three passes
- The only conditional is whether the length guard instruction is appended to the refine pass prompt — it is only appended when the draft actually exceeds the word limit

## Log Examples

```
# Director pass (first tool)
INFO:backend.llm_client:LLM complete: model=default,
  tools=["direct_scene", "rewrite_user_prompt", "editor_apply_patch", "editor_rewrite"],
  tool_choice={'type': 'function', 'function': {'name': 'rewrite_user_prompt'}}

# Director pass (second tool)
INFO:backend.llm_client:LLM complete: model=default,
  tools=["direct_scene", "rewrite_user_prompt", "editor_apply_patch", "editor_rewrite"],
  tool_choice={'type': 'function', 'function': {'name': 'direct_scene'}}

# Writer pass — identical schema to director for KV cache reuse
INFO:backend.llm_client:LLM stream: model=default,
  tools=["direct_scene", "rewrite_user_prompt", "editor_apply_patch", "editor_rewrite"],
  tool_choice=none

# Refine pass — identical schema to writer for KV cache reuse
INFO:backend.llm_client:LLM complete: model=default,
  tools=["direct_scene", "rewrite_user_prompt", "editor_apply_patch", "editor_rewrite"],
  tool_choice=auto
```

## Benefits

1. **Reduced Latency**: Subsequent passes reuse cached computations
2. **Lower Token Processing**: Shared prefix doesn't need re-processing
3. **Cost Efficiency**: Fewer total tokens processed by the LLM
4. **Consistent Behavior**: Model maintains same tool understanding across passes

## Edge Cases and Considerations

### Disabled Tools
- When tools are disabled via settings, they're excluded from all passes
- Empty tool list (`[]`) is valid and consistent across passes
