# Design Proposal: Dynamic Tool Call Token Suppression

## 1. Objective
To prevent the LLM from generating tool calls during the **Writer Pass** by utilizing `logit_bias`. We will identify and suppress the specific single token that the model uses to initiate a tool call, while retaining the tool schemas in the prompt for KV Cache reuse.

This approach avoids banning function name tokens (which might appear in narrative text) and focuses strictly on the control token that triggers the tool call structure.

## 2. The "Tool Start" Token Concept
Most modern instruction-tuned models utilizing tool use rely on a specific control token to initiate a tool call sequence (e.g., `<|tool_call|>`, `<|python_tag|>`, or similar model-specific markers).

By lowering the logit probability of this single "start" token to -100, we can mathematically prevent the model from entering the tool call generation state, forcing it to continue generating standard text instead.

## 3. Implementation Strategy

We will implement a **Discovery Phase** that runs once per model to identify this token ID, followed by a **Suppression Phase** during the Writer Pass.

### 3.1 Phase 1: Discovery (Probe & Resolve)
Since we do not have access to a local tokenizer and cannot rely on hardcoded token IDs, we will "probe" the model via API to discover the token ID dynamically.

**Step A: Force a Tool Call**
We make a minimal, inexpensive API call designed to force the model to output a tool call:
1.  **Payload**: A simple prompt (e.g., "Help me") + a dummy tool schema.
2.  **Config**: `tool_choice="auto"`.
3.  **Goal**: The model attempts to call the tool.

**Step B: Capture the Lead Token**
1.  The API returns the generated content. If the model calls a tool, the response typically contains the start of the tool call structure.
2.  We capture the **first token string** from the streamed response or the generation output.

**Step C: Resolve to Integer ID**
1.  Once we have the string (e.g., `<|tool_call|>`), we use the backend's `/tokenize` endpoint (available in vLLM, TabbyAPI, and most OpenAI-compatible servers) to convert this string into its integer token ID.
2.  *Note*: If the API does not expose a `/tokenize` endpoint, we can infer the ID by requesting `logprobs` during Step A and reading the ID directly from the logprobs data.

**Step D: Cache**
Store the discovered ID in the storage backend (e.g., `tool_start_token:{model_id} = 12890`).

### 3.2 Phase 2: Writer Pass Suppression
When the **Writer Pass** executes:

1.  **Retrieve**: Load the cached token ID for the current model.
2.  **Configure**: Construct the `logit_bias` parameter: `{discovered_id: -100}`.
3.  **Execute**: Send the Writer Pass request with:
    *   `tools`: All schemas (preserves KV Cache).
    *   `logit_bias`: Applied to the tool start token.
    *   `tool_choice`: "none" (as a secondary safety layer).

## 4. Edge Case Handling

### 4.1 Model Fallback (No Special Token)
Some models (e.g., older base models or specific fine-tunes) do not use a special control token; they simply output JSON objects or function names directly.
*   **Detection**: If the "Discovery Phase" returns a generic character (like `{` or a letter), applying a bias would "break the writer" (prevent valid narrative).
*   **Mitigation**: We implement a heuristic check. If the discovered token string is a common printable character (alphanumeric or standard punctuation), we **discard** the result and disable logit bias for that model. We rely solely on `tool_choice="none"`.

### 4.2 API Compatibility
*   **Tokenize Endpoint**: We require either a `/tokenize` endpoint or `logprobs` support in the generation API to resolve the ID. If neither is available, the feature gracefully degrades (logs a warning and skips biasing).

## 5. System Workflow Diagram

1.  **Model Load / First Run**:
    *   System checks cache for `tool_start_token_id`.
    *   *If missing*: Run Discovery Probe -> Resolve ID -> Save to Cache.
2.  **Writer Pass**:
    *   Load `tool_start_token_id`.
    *   Validate ID is not a generic character.
    *   Add to `logit_bias` map.
    *   Send API Request.

## 6. Benefits
*   **Non-Destructive**: Does not interfere with narrative tokens (like function names).
*   **KV Cache Preserved**: Tools remain in the prompt context.
*   **Model Agnostic**: Automatically adapts to any model that uses special control tokens for tooling (Llama 3.x, Mistral, Qwen, etc.).