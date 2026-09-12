import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { promisify } from "node:util";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const execFileAsync = promisify(execFile);
const PROJECT_ROOT = "/home/ubuntu/.openclaw/workspace/tools/spotguard-agent-os";
const RISK_PILOT = `${PROJECT_ROOT}/riskpilot`;
const RISK_PILOT_CONFIG = `${PROJECT_ROOT}/config.json`;
const EXTENSION_MARKER = "20260912-direct-dispatch-runtime-proof";
const CANDIDATE_RE = /^c-[0-9a-f]{12}$/;
const PROPOSAL_RE = /^p-[0-9a-f]{12}$/;

function configuredOwner(ctx) {
  const ownerAllowFrom = ctx.config?.commands?.ownerAllowFrom;
  const telegramAllowFrom = ctx.config?.channels?.telegram?.allowFrom;
  if (!Array.isArray(ownerAllowFrom) || !Array.isArray(telegramAllowFrom)) return null;
  const owner = ownerAllowFrom.find((value) => typeof value === "string" && value.startsWith("telegram:"));
  if (!owner) return null;
  const ownerId = owner.slice("telegram:".length);
  return telegramAllowFrom.includes(ownerId) ? ownerId : null;
}

function authorizedTelegram(ctx) {
  const ownerId = configuredOwner(ctx);
  if (!ownerId || ctx.channel !== "telegram" || ctx.channelId !== "telegram") return false;
  if (ctx.senderId !== ownerId) return false;
  // Native Telegram command context carries the destination as telegram:<chat>.
  return ctx.to === `telegram:${ownerId}`;
}

function reviewArgs(args) {
  if (typeof args !== "string") return null;
  const match = /^review ([^\s]+)$/.exec(args.trim());
  if (!match || !CANDIDATE_RE.test(match[1])) return null;
  return match[1];
}

function safeCommandArgs(args) {
  const candidateId = reviewArgs(args);
  return candidateId ? `review ${candidateId}` : "<invalid>";
}

function failureText() {
  return "RiskPilot AI REVIEW FAILED. No proposal was created.";
}

async function executeReview(candidateId) {
  return runRiskPilot([
    "--config", RISK_PILOT_CONFIG, "--json", "agent-os", "review",
    "--candidate", candidateId, "--notify", "--dispatch-source", "telegram_direct",
  ], 180000);
}

function approvalArgs(args) {
  if (typeof args !== "string") return null;
  const match = /^(live-(?:approve|reject))\s+(p-[0-9a-f]{12})$/.exec(args.trim());
  return match ? { action: match[1], proposalId: match[2] } : null;
}

// This classifier is deliberately small and read-only.  It is used before an
// LLM turn only for the documented clear Telegram requests; it never matches a
// trade, review, or approval phrase.
export function readOnlyRoute(text) {
  if (typeof text !== "string") return null;
  const exact = text.trim().toLowerCase();
  if (exact === "/spot live balance") return "balance";
  if (exact === "/spot live positions") return "positions";
  const value = exact.normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, " ").trim();
  if (!value || /\b(paper|demo|simulasi|simulation|virtual)\b/.test(value)) return null;
  const balance = /\b(balance|saldo|funds?|dana|uang)\b/.test(value);
  const positions = /\b(open positions?|positions?|posisi|holdings?|portfolio|portofolio|protected holdings?)\b/.test(value);
  const live = /\b(live|spot)\b/.test(value);
  const check = /\b(check|current|cek|lihat|show|my|all)\b/.test(value);
  if (positions && (balance || live || check)) return "positions";
  if (balance && (live || check)) return "balance";
  return null;
}

function tradeIntentRoute(text) {
  if (typeof text !== "string") return null;
  const value = text.trim().replace(/\s+/g, " ");
  if (!value || /\b(?:paper|demo|simulasi|simulation|virtual)\b/i.test(value)) return null;
  // Claim only bounded, unambiguous entry syntax.  Amount validation,
  // allowlisting, exchange filters, readiness, and dormant-proposal creation
  // remain exclusively in RiskPilot's existing `trade-intent` CLI route.
  const naturalBuy = /^(?:buy|beli)\s+[a-z][a-z0-9]{1,19}\s+(?:0*[1-9]\d*(?:\.\d+)?|0+\.\d*[1-9]\d*)\s*(?:usd|usdt)?$/i;
  // Preserve the existing SELL matcher and its ordering/semantics.
  const naturalSell = /^(?:sell|jual)\s+\S/i;
  if (!naturalBuy.test(value) && !naturalSell.test(value)) return null;
  return value;
}

function trustedCommandIdentity(ctx) {
  const senderId = normalizeTelegramId(ctx.senderId, false);
  const chatId = normalizeTelegramId(ctx.to, true);
  return senderId && chatId ? { senderId, chatId } : null;
}

function trustedInteractiveIdentity(ctx) {
  const senderId = normalizeTelegramId(ctx.senderId, false);
  const chatId = normalizeTelegramId(ctx.callback?.chatId, true);
  return senderId && chatId ? { senderId, chatId } : null;
}

// Telegram ingress supplies raw IDs for senderId and either raw IDs or a
// `telegram:` routing target for a conversation.  Strip only that trusted
// transport prefix; reject all other shapes instead of inventing an identity.
export function normalizeTelegramId(value, allowTransportPrefix) {
  // Telegram IDs normally arrive as strings, but accepting a safe integer from
  // the typed transport contract avoids a number/string mismatch.  The result
  // is always the raw decimal string RiskPilot validates itself.
  let normalized;
  if (typeof value === "string") normalized = value.trim();
  else if (typeof value === "number" && Number.isSafeInteger(value)) normalized = String(value);
  else return null;
  if (allowTransportPrefix && normalized.startsWith("telegram:")) normalized = normalized.slice("telegram:".length);
  return /^-?\d+$/.test(normalized) ? normalized : null;
}

async function executeApproval(action, proposalId, identity) {
  return runRiskPilot([
    "--config", RISK_PILOT_CONFIG, "--json", action, proposalId,
    "--sender-id", identity.senderId, "--chat-id", identity.chatId,
  ], 180000);
}

async function executeReadOnly(route) {
  return runRiskPilot([
    "--config", RISK_PILOT_CONFIG, "--json", "live", route,
  ], 30_000);
}

export function tradeIntentArgs(text, identity) {
  return [
    "--config", RISK_PILOT_CONFIG, "--json", "trade-intent", "--text", text,
    "--sender-id", identity.senderId, "--chat-id", identity.chatId, "--notify",
  ];
}

async function executeTradeIntent(text, identity, onDiagnostic = undefined) {
  return runRiskPilot(tradeIntentArgs(text, identity), 30_000, onDiagnostic);
}

export function riskPilotInvocation(args, timeout) {
  return {
    file: RISK_PILOT, args,
    // The checked-in launcher establishes its own project-local PYTHONPATH.
    // Never use PATH lookup, `python -m`, or the OpenClaw process cwd here.
    cwd: PROJECT_ROOT, timeout, maxBuffer: 2 * 1024 * 1024, shell: false,
  };
}

// `riskpilot` uses exit status 2 for all safe, rendered CLI rejections.  The
// JSON error type is therefore the authoritative diagnostic, not the status
// code.  These categories are journal-only; Telegram still receives the
// rendered CLI presentation text.
export function cliFailureCategory(error, parsed) {
  if (parsed?.type === "SecurityError") return "riskpilot_security_error";
  if (parsed?.type === "PolicyError") return "riskpilot_policy_error";
  if (parsed?.type === "ValueError") return "riskpilot_validation_error";
  if (parsed?.type === "TelegramError") return "riskpilot_notification_error";
  if (parsed) return "riskpilot_unknown_error";
  if (error?.code === "ENOENT") return "launcher_unavailable";
  if (error?.code === 2) return "riskpilot_argparse_error";
  if (typeof error?.code === "number") return "cli_exit_without_json";
  return "cli_spawn_or_parse_failure";
}

function parseCliJson(stdout) {
  if (typeof stdout !== "string" || !stdout.trim()) return null;
  try { return JSON.parse(stdout); } catch { return null; }
}

async function runRiskPilot(args, timeout, onDiagnostic = undefined) {
  const invocation = riskPilotInvocation(args, timeout);
  onDiagnostic?.({ phase: "spawn", reached: true });
  try {
    const { stdout } = await execFileAsync(invocation.file, invocation.args, invocation);
    const parsed = parseCliJson(stdout);
    if (!parsed) {
      onDiagnostic?.({ phase: "exit", exitCode: 0, category: "invalid_cli_json" });
      throw new Error("RiskPilot returned invalid JSON");
    }
    onDiagnostic?.({ phase: "exit", exitCode: 0, category: "ok" });
    return parsed;
  } catch (error) {
    const parsed = parseCliJson(error?.stdout);
    const exitCode = typeof error?.code === "number" ? error.code : "unknown";
    const category = cliFailureCategory(error, parsed);
    onDiagnostic?.({ phase: "exit", exitCode, category });
    // RiskPilot deliberately returns JSON presentation text for safe rejections
    // with exit status 2.  Preserve that user-facing result, but never log its
    // raw stderr or environment.
    if (parsed) return parsed;
    throw error;
  }
}

function safeRefusalReason(result) {
  if (!new Set(["SecurityError", "PolicyError", "ValueError"]).has(result?.type)) return null;
  const reason = result?.error;
  // CLI error text is intentionally bounded and rendered by RiskPilot.  Keep
  // the Telegram boundary equally conservative: never relay multiline data,
  // paths, or a possible stack trace.
  if (typeof reason !== "string" || !reason.trim() || reason.length > 300
      || /[\r\n]|[\/\\]|\b(?:traceback|stack)\b/i.test(reason)) return null;
  return reason.trim();
}

function presentationText(result, fallback) {
  const reason = safeRefusalReason(result);
  if (reason) return `RiskPilot refused safely: ${reason}.`;
  const text = result?.presentation?.text || result?.message;
  return typeof text === "string" && text.trim() ? text : fallback;
}

function logDiagnostic(logger, source, ctx, validation) {
  if (!logger?.info) return;
  const sender = typeof ctx.senderId === "string" ? ctx.senderId : "<missing>";
  const chat = typeof ctx.to === "string" ? ctx.to : (ctx.callback?.chatId ?? "<missing>");
  const args = source === "telegram_direct" ? safeCommandArgs(ctx.args) :
    (typeof ctx.callback?.payload === "string" && CANDIDATE_RE.test(ctx.callback.payload)
      ? ctx.callback.payload : "<invalid>");
  logger.info(`[riskpilot-direct-review] entered source=${source} args=${args} sender=${sender} chat=${chat} validation=${validation}`);
}

function logRoute(logger, route, identity, status) {
  if (!logger?.info) return;
  logger.info(`[riskpilot-direct-review] route=${route} sender=${identity?.senderId ?? "<missing>"} chat=${identity?.chatId ?? "<missing>"} status=${status}`);
}

function logIngressHook(logger, route) {
  if (!logger?.info) return;
  // Deliberately omit message content and all Telegram metadata. This only
  // proves that the real typed-hook dispatcher entered this extension.
  logger.info(`[riskpilot-direct-review] ingress-hook-entered marker=${EXTENSION_MARKER} route=${route}`);
}

function fieldShape(value) {
  if (value === undefined || value === null || value === "") return "missing";
  if (typeof value === "number") return Number.isSafeInteger(value) ? "number:integer" : "number:invalid";
  if (typeof value !== "string") return typeof value;
  const trimmed = value.trim();
  if (/^-?\d+$/.test(trimmed)) return "string:numeric";
  if (/^telegram:-?\d+$/.test(trimmed)) return "string:telegram-numeric";
  return "string:non-telegram";
}

function safeRawId(value) {
  const shape = fieldShape(value);
  return shape === "string:numeric" || shape === "string:telegram-numeric" || shape === "number:integer"
    ? String(value).trim() : "<unusable>";
}

function logBeforeDispatchDiagnostic(logger, route, trace, status, cli = undefined) {
  if (!logger?.info) return;
  const suffix = cli
    ? ` cli_phase=${cli.phase} cli_spawn=${cli.reached ?? false} cli_exit=${cli.exitCode ?? "pending"} cli_category=${cli.category ?? "pending"}`
    : "";
  // before_dispatch exposes no isAuthorizedSender field in OpenClaw 2026.8.1.
  // The strict owner/chat equality check below is consequently the transport
  // authorization proof for this hook.
  logger.info(`[riskpilot-direct-review] route=${route} sender_field=event.senderId sender_present=${trace.senderPresent} sender_shape=${trace.senderShape} sender_raw=${safeRawId(trace.senderRaw)} sender_normalized=${trace.senderId ?? "<missing>"} chat_field=context.conversationId chat_present=${trace.chatPresent} chat_shape=${trace.chatShape} chat_raw=${safeRawId(trace.chatRaw)} chat_normalized=${trace.chatId ?? "<missing>"} transport_authorization=not_exposed_by_before_dispatch validation=${status}${suffix}`);
}

function configuredInteractiveOwner(api) {
  return configuredOwner({ config: api.config });
}

async function trustedCommandContext(ctx) {
  const identity = trustedCommandIdentity(ctx);
  if (!identity) return { identity: null, reason: "sender or chat metadata is unavailable" };
  const trust = await readSettings();
  if (ctx.channel !== "telegram" || ctx.channelId !== "telegram" || ctx.isAuthorizedSender !== true)
    return { identity: null, reason: "Telegram transport authorization is unavailable" };
  if (identity.senderId !== trust.owner) return { identity: null, reason: "sender metadata is not the configured owner" };
  if (identity.chatId !== trust.chat) return { identity: null, reason: "chat metadata is not the configured chat" };
  return { identity, reason: null };
}

async function trustedInteractiveContext(ctx) {
  const identity = trustedInteractiveIdentity(ctx);
  if (!identity) return { identity: null, reason: "callback sender or chat metadata is unavailable" };
  const trust = await readSettings();
  if (ctx.channel !== "telegram" || ctx.auth?.isAuthorizedSender !== true)
    return { identity: null, reason: "Telegram callback authorization is unavailable" };
  if (identity.senderId !== trust.owner) return { identity: null, reason: "callback sender metadata is not the configured owner" };
  if (identity.chatId !== trust.chat) return { identity: null, reason: "callback chat metadata is not the configured chat" };
  return { identity, reason: null };
}

async function renderReviewResult(result, respond) {
  const decision = result?.review_decision;
  if (!["APPROVE", "REJECT", "NO_TRADE"].includes(decision)) {
    await respond("RiskPilot AI REVIEW FAILED. No proposal was created.");
    return;
  }
    if (decision === "APPROVE") {
      if (result.proposal_deferred === true && result.deferred_reason === "LIVE_NOT_EXECUTION_READY") {
        await respond("✅ AI REVIEW: APPROVE\n\nFresh market data verified.\n\nLIVE proposal was not created because LIVE is not execution-ready.\nCandidate remains ACTIVE. No LIVE order was submitted.");
        return;
      }
      if (result.proposal === null && result.proposal_status === "SKIPPED_NOT_EXECUTION_READY") {
        await respond(presentationText(result, "✅ AI REVIEW completed. LIVE proposal was skipped because execution readiness requirements were not met. No LIVE order was submitted."));
        return;
      }
    if (!result.proposal || !result.notification?.delivered) {
      await respond("RiskPilot AI REVIEW FAILED. No proposal was created.");
      return;
    }
    return;
  }
  await respond(`RiskPilot AI REVIEW ${decision}. ${result.market_review?.reason || "No proposal was created."}`);
}

export function createRiskPilotReviewHandler(runReview = executeReview, logger = undefined, runApproval = executeApproval) {
  return async (ctx) => {
    const approval = approvalArgs(ctx.args);
    if (approval) {
      const trusted = await trustedCommandContext(ctx);
      const valid = Boolean(trusted.identity);
      logDiagnostic(logger, "telegram_direct_approval", ctx, valid ? "accepted" : "rejected");
      if (!valid) return { text: `RiskPilot LIVE action refused safely: ${trusted.reason}.`, isError: true, continueAgent: false };
      try {
        const result = await runApproval(approval.action, approval.proposalId, trusted.identity);
        return { text: result.presentation?.text || result.message || "RiskPilot LIVE action processed.", continueAgent: false };
      } catch {
        return { text: "RiskPilot LIVE action failed safely; no fallback action was taken.", isError: true, continueAgent: false };
      }
    }
    const candidateId = reviewArgs(ctx.args);
    const trusted = candidateId ? await trustedCommandContext(ctx) : { identity: null, reason: "review command is malformed" };
    const valid = Boolean(candidateId && trusted.identity);
    logDiagnostic(logger, "telegram_direct", ctx, valid ? "accepted" : "rejected");
    if (!valid) {
      return { text: `RiskPilot AI REVIEW refused safely: ${trusted.reason}. No proposal was created.`, isError: true, continueAgent: false };
    }
    try {
      const result = await runReview(candidateId);
      const decision = result.review_decision;
      if (!["APPROVE", "REJECT", "NO_TRADE"].includes(decision)) {
        return { text: failureText(), isError: true, continueAgent: false };
      }
      if (decision === "APPROVE") {
        if (result.proposal_deferred === true && result.deferred_reason === "LIVE_NOT_EXECUTION_READY") {
          return { text: "✅ AI REVIEW: APPROVE\n\nFresh market data verified.\n\nLIVE proposal was not created because LIVE is not execution-ready.\nCandidate remains ACTIVE. No LIVE order was submitted.", continueAgent: false };
        }
        if (result.proposal === null && result.proposal_status === "SKIPPED_NOT_EXECUTION_READY") {
          return { text: presentationText(result, "✅ AI REVIEW completed. LIVE proposal was skipped because execution readiness requirements were not met. No LIVE order was submitted."), continueAgent: false };
        }
        // The fixed command delivered the existing proposal controls through
        // the deterministic messenger. Never route this result to the LLM.
        if (!result.proposal || !result.notification?.delivered) {
          return { text: failureText(), isError: true, continueAgent: false };
        }
        return { suppressReply: true, continueAgent: false };
      }
      const reason = result.market_review?.reason || "No proposal was created.";
      return { text: `RiskPilot AI REVIEW ${decision}. ${reason}`, continueAgent: false };
    } catch {
      return { text: failureText(), isError: true, continueAgent: false };
    }
  };
}

export function extractBeforeDispatchIdentity(event, context, settings) {
  // These are the documented OpenClaw before_dispatch transport fields. Do not
  // use session keys, text, configured IDs, or an alternate context fallback.
  const senderRaw = event?.senderId;
  const chatRaw = context?.conversationId;
  const senderId = normalizeTelegramId(senderRaw, true);
  const chatId = normalizeTelegramId(chatRaw, true);
  const trace = {
    senderRaw, chatRaw, senderId, chatId,
    senderPresent: senderRaw !== undefined && senderRaw !== null && senderRaw !== "",
    chatPresent: chatRaw !== undefined && chatRaw !== null && chatRaw !== "",
    senderShape: fieldShape(senderRaw), chatShape: fieldShape(chatRaw),
  };
  if (!trace.senderPresent) return { identity: null, trace, reason: "missing_trusted_sender_metadata" };
  if (!senderId) return { identity: null, trace, reason: "malformed_trusted_sender_metadata" };
  if (!trace.chatPresent) return { identity: null, trace, reason: "missing_trusted_chat_metadata" };
  if (!chatId) return { identity: null, trace, reason: "malformed_trusted_chat_metadata" };
  if (senderId !== settings.owner) return { identity: null, trace, reason: "unauthorized_transport_sender" };
  if (chatId !== settings.chat) return { identity: null, trace, reason: "unauthorized_transport_chat" };
  return { identity: { senderId, chatId }, trace, reason: null };
}

async function readSettings() {
  const raw = JSON.parse(await readFile(RISK_PILOT_CONFIG, "utf8"));
  return {
    owner: String(raw?.openclaw?.telegram_owner_id ?? ""),
    chat: String(raw?.telegram?.chat_id ?? ""),
  };
}

// Exported for fixture-only regression tests.  The runtime passes executeReadOnly.
export function createRiskPilotReadOnlyHook(runReadOnly = executeReadOnly, runTradeIntent = executeTradeIntent, logger = undefined) {
  return async (event, context) => {
    const body = event.body ?? event.content;
    const route = readOnlyRoute(body);
    const sellIntent = route ? null : tradeIntentRoute(body);
    logIngressHook(logger, event.channel === "telegram" ? (route ?? (sellIntent ? "trade-intent" : "none")) : "non-telegram");
    if (event.channel !== "telegram") return undefined;
    if (!route && !sellIntent) return undefined;
    try {
      const routeName = route ?? "trade-intent";
      const trusted = extractBeforeDispatchIdentity(event, context, await readSettings());
      if (!trusted.identity) {
        logBeforeDispatchDiagnostic(logger, routeName, trusted.trace, trusted.reason);
        return { handled: true, text: "RiskPilot requires trusted Telegram sender and chat metadata for this action.", isError: true };
      }
      const onDiagnostic = route ? undefined : (cli) =>
        logBeforeDispatchDiagnostic(logger, routeName, trusted.trace, "accepted", cli);
      const result = route ? await runReadOnly(route, trusted.identity) : await runTradeIntent(sellIntent, trusted.identity, onDiagnostic);
      logRoute(logger, routeName, trusted.identity, "handled");
      return { handled: true, text: presentationText(result, "RiskPilot returned no displayable result.") };
    } catch {
      logRoute(logger, route ?? "trade-intent", null, "adapter_failure");
      return { handled: true, text: "RiskPilot could not complete the read-only request safely.", isError: true };
    }
  };
}

export function createRiskPilotInteractiveHandler(runReview = executeReview, api) {
  return async (ctx) => {
    const candidateId = CANDIDATE_RE.test(ctx.callback?.payload ?? "") ? ctx.callback.payload : null;
    const trusted = candidateId ? await trustedInteractiveContext(ctx) : { identity: null, reason: "review callback is malformed" };
    const valid = Boolean(candidateId && trusted.identity);
    logDiagnostic(api.logger, "telegram_presentation_callback", ctx, valid ? "accepted" : "rejected");
    if (!valid) {
      await ctx.respond.reply({ text: `RiskPilot AI REVIEW refused safely: ${trusted.reason}. No proposal was created.` });
      return { handled: true };
    }
    try {
      await renderReviewResult(await runReview(candidateId), async (text) => {
        await ctx.respond.reply({ text });
      });
    } catch {
      await ctx.respond.reply({ text: failureText() });
    }
    await ctx.respond.clearButtons();
    return { handled: true };
  };
}

export default definePluginEntry({
  id: "riskpilot-direct-review",
  name: "RiskPilot Direct Review",
  description: "Deterministic, owner-only Telegram dispatch for RiskPilot review.",
  register(api) {
    api.logger?.info?.(`[riskpilot-direct-review] extension-loaded marker=${EXTENSION_MARKER}`);
    api.registerCommand({
      name: "binance_spotguard",
      nativeNames: { telegram: "binance_spotguard" },
      description: "Run an owner-only RiskPilot action",
      channels: ["telegram"],
      acceptsArgs: true,
      requireAuth: true,
      exposeSenderIsOwner: true,
      handler: createRiskPilotReviewHandler(executeReview, api.logger),
    });
    // `before_dispatch` is a typed hook in OpenClaw 2026.8.1.  `registerHook`
    // registrations for it are explicitly not invoked; `api.on` is the
    // supported claiming API.  `{handled:true}` consumes the inbound turn.
    api.on("before_dispatch", createRiskPilotReadOnlyHook(executeReadOnly, executeTradeIntent, api.logger));
    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: "riskpilot-review",
      handler: createRiskPilotInteractiveHandler(executeReview, api),
    });
  },
});
