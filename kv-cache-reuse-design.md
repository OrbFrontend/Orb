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

The system maintains a consistent set of enabled tools across all passes for KV cache consistency:

```python
# From orchestrator.py
def _enabled_schemas(enabled_tools: dict | None) -> list[dict]:
    """Return tool schemas. None means 'all enabled', {} means 'all disabled'."""
    if enabled_tools is None:
        return ALL_SCHEMAS
    return [TOOLS[n]["schema"] for n in TOOLS if enabled_tools.get(n, False)]
```

### Post-Writer Tools Exclusion

Some tools are designated as "post-writer" tools and are excluded from being called in the agent pass:

```python
POST_WRITER_TOOLS = {"refine_apply_patch"}
```

- `refine_apply_patch`: Used only in refine pass for self-audit
- Excluded from agent pass tool calling (but still included in API for KV cache)
- This separation ensures tools are used in appropriate phases

### Critical Distinction: Tools in API vs Tools Called

There's an important distinction between:
- **Tools included in the inference API call**: All enabled tool schemas are sent for KV cache consistency
- **Tools actually called in agent pass**: Only specific tools are invoked based on `tool_choice` and agent logic

### Three-Pass Tool Consistency

1. **Director Pass (Agent Pass)**:
   - **API tools**: All enabled tools (`["direct_scene", "rewrite_user_prompt", "refine_apply_patch"]`)
   - **Agent-called tools**: Only tools not in `POST_WRITER_TOOLS` (excludes `refine_apply_patch`)
   - **Tool choice**: Sequential calls with specific `tool_choice`:
     - First: `{'type': 'function', 'function': {'name': 'rewrite_user_prompt'}}` (if enabled)
     - Second: `{'type': 'function', 'function': {'name': 'direct_scene'}}`
   - **Purpose**: Scene direction and optional prompt rewriting (rewrite runs first so users can stop early if they don't like the rewritten message)

2. **Writer Pass**:
   - **API tools**: All enabled tools (`["direct_scene", "rewrite_user_prompt", "refine_apply_patch"]`)
   - **Tool choice**: `none` (prevents tool calling during story generation)
   - **Purpose**: Generate the actual roleplay response

3. **Refine Pass**:
   - **API tools**: All enabled tools PLUS conditional `refine_rewrite` tool
   - **Base tools**: `["direct_scene", "rewrite_user_prompt", "refine_apply_patch"]`
   - **Conditional addition**: `"refine_rewrite"` (only when length guard triggered)
   - **Tool choice**: `auto` or specific refine tool based on context
   - **Purpose**: Self-audit and length optimization

### Conditional Tool Addition

The `refine_rewrite` tool is special:
- Not included in the base enabled tools set
- Added dynamically only when length guard is triggered (output exceeds word limit)
- This is a design choice because length guard rarely triggers
- Prevents unnecessary tool schema bloat in common cases

## Log Examples

```
# Director pass (first tool)
INFO:backend.llm_client:LLM complete: model=default,
  tools=["direct_scene", "rewrite_user_prompt", "refine_apply_patch"],
  tool_choice={'type': 'function', 'function': {'name': 'rewrite_user_prompt'}}

# Director pass (second tool)
INFO:backend.llm_client:LLM complete: model=default,
  tools=["direct_scene", "rewrite_user_prompt", "refine_apply_patch"],
  tool_choice={'type': 'function', 'function': {'name': 'direct_scene'}}

# Writer pass
INFO:backend.llm_client:LLM stream: model=default,
  tools=["direct_scene", "rewrite_user_prompt", "refine_apply_patch"],
  tool_choice=none

# Refine pass (with length guard triggered)
INFO:backend.llm_client:LLM complete: model=default,
  tools=["direct_scene", "rewrite_user_prompt", "refine_apply_patch", "refine_rewrite"],
  tool_choice=auto
```

## Benefits

1. **Reduced Latency**: Subsequent passes reuse cached computations
2. **Lower Token Processing**: Shared prefix doesn't need re-processing
3. **Cost Efficiency**: Fewer total tokens processed by the LLM
4. **Consistent Behavior**: Model maintains same tool understanding across passes

## Edge Cases and Considerations

### Dynamic Tool Addition
- The `refine_rewrite` tool is added conditionally in refine pass
- This is acceptable because:
  a) Length guard rarely triggers
  b) When it does, the cache miss penalty is acceptable
  c) Avoids schema bloat in 99% of cases

### Disabled Tools
- When tools are disabled via settings, they're excluded from all passes
- Empty tool list (`[]`) is valid and consistent across passes
