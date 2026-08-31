# Document Mode

Document mode is a plain-text editor for freeform writing. It has no character
card and no chat history. Use it for stories, notes, or prompt experiments.

## Open and manage documents

Select the **Chat/Document** button beside the Orb title. The sidebar changes to a
**Documents** list.

- **+ New Document** creates a document.
- Select a document to open it, **✏** to rename it, or **✕** to delete it.
- Documents autosave after you stop typing. The header shows the save status.
- The token count estimates the whole document.

Orb reopens the last document when you return to Document mode.

## Generate text

Place the cursor where the Writer should continue and select **Generate**. You can
also use Ctrl/⌘+Enter. Generated text streams into the document and is tinted so
you can distinguish it from your own text. Type anywhere in the document, even
inside generated text, and your text keeps the normal styling.

Select **Stop** or press Escape to stop a generation. Select **Generate** again to
continue. Each run uses up to the Writer model's **Max Tokens** setting.

## Raw and Assisted prompts

The mode switch at the bottom of the editor controls how Orb sends the document.

- **Raw** sends the document exactly as written. Add your model's chat-template
  markers yourself and place the cursor after the opening assistant marker.
- **Assisted** treats lines beginning with `### SYSTEM:`, `### USER:`, and
  `### ASSISTANT:` as instructions. Other lines are prose. Orb applies the model's
  chat template for you.

Example:

```
### SYSTEM: Match the voice and style.
### USER: Write an ornate story about a monkey.
Once upon a time, beneath the gilded canopy...
### USER: Continue in short sentences.
The monkey woke. He|
```

Use **How to prompt** in the editor for the same syntax guide.

## Output Auditor

After each run, the Output Auditor checks the new text for the same phrase and
repetition patterns used by Chat mode. Enable **Auto-patch** to let Orb apply a
repair automatically, or review the findings yourself.

Document mode also supports undo and redo for both your typing and generated
text. It works on mobile-sized screens.
