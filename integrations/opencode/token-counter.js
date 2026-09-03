/** OpenCode plugin: forward only local numeric-accounting identifiers, never payloads. */
function attachHeaders(input, output, environment = process.env) {
  if (input.model?.providerID !== "bifrost-litellm") return;
  const base = input.provider?.options?.baseURL ?? input.model?.api?.url;
  let target;
  try { target = new URL(base); } catch { return; }
  const port = environment.TOKEN_COUNTER_PORT || "8001";
  if (target.protocol !== "http:" || target.username || target.password || target.search || target.hash || !["127.0.0.1", "localhost"].includes(target.hostname) || target.port !== port || target.pathname.replace(/\/$/, "") !== "/v1") return;
  if (typeof input.sessionID !== "string" || !input.sessionID || input.sessionID.length > 256 || /[\x00-\x1f\x7f]/.test(input.sessionID)) return;
  const instance = environment.TOKEN_COUNTER_CLIENT_INSTANCE_ID || "opencode-local";
  const values = {
    "X-Token-Counter-Client": "opencode",
    "X-Token-Counter-Instance-Id": instance,
    "X-Token-Counter-Session-Id": input.sessionID,
    "X-Token-Counter-Message-Id": input.message?.id,
    "X-Token-Counter-Agent": input.agent,
  };
  // Classification is explicit. Unknown/custom agent names are never guessed from text or stream.
  const kinds = {build:"main", plan:"main", general:"main", explore:"main", title:"auxiliary", summary:"auxiliary", compaction:"compaction"};
  values["X-Token-Counter-Request-Kind"] = kinds[input.agent] || "unknown";
  for (const [key,value] of Object.entries(values)) {
    if (typeof value === "string" && value.length > 0 && value.length <= 256 && !/[\x00-\x1f\x7f]/.test(value)) output.headers[key] = value;
  }
}

export const TokenCounterPlugin = async () => ({
  "chat.headers": async (input, output) => { attachHeaders(input, output); },
});
