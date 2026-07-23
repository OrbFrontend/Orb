// DOM-free, facade-free policy predicates shared by the settings panel.
//
// Separate from config_panel.js for the same reason render.js is separate from
// widget.js: anything importing the plugin facade pulls in the chat spine and
// touches the DOM at load, so it cannot be exercised under `node --test`. These
// rules decide what the user is warned about, which is exactly the kind of thing
// that should have a test.

// Loopback gets no privacy banner: none of the warning's claims — your prompts
// leave this machine, other clients can read the queue, files stay on that disk
// — describe a boundary being crossed when the server is this machine. A warning
// shown on every configuration is one users learn to click through.
export function isLoopbackUrl(apiUrl) {
  let parsed;
  try {
    parsed = new URL(apiUrl);
  } catch {
    // An unparseable URL is rejected by the backend normalizer before it can
    // reach a server, so there is no remote boundary to warn about.
    return true;
  }
  // URL.hostname keeps the brackets on an IPv6 literal, so `[::1]` is the form
  // that actually arrives here; comparing against a bare `::1` never matches.
  const host = parsed.hostname.toLowerCase().replace(/^\[/, "").replace(/\]$/, "");
  return host === "127.0.0.1" || host === "localhost" || host === "::1" || host === "0:0:0:0:0:0:0:1";
}
