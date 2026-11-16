import { WAITING_LABEL } from "./generation_status.js";

// One chat turn, from its request to the refetch that settles it. Each conversation holds one turn record (`state.turn`,
// so a turn in a chat that is not on screen keeps running). Its phase alone says whether a turn runs:
//
//   idle      nothing running
//   waiting   a request, a step or a group speaker has started and shows WAITING_LABEL until it produces output
//   running   a step shows its own label
//   settling  the stream has ended; the browser refetches what the server saved, and nothing new may start
//
//   idle ──beginTurn()──▶ waiting ⇄ running ──settleTurn()──▶ settling ──idleTurn()──▶ idle
//
// A turn starts and ends by replacing the whole record, so nothing it held survives into the next one; the moves in
// between go through the functions below, which refuse any other. Stop is not a phase: stream_settle.js's operation owns
// it, and the turn keeps its phase until the stream ends.

const MOVES = {
  idle: [],
  waiting: ["waiting", "running", "settling"],
  running: ["waiting", "running", "settling"],
  settling: [],
};

/** A conversation's turn record with nothing running. */
export function idleTurn() {
  return {
    phase: "idle",
    step: "", // the label a running step shows
    nextStep: "", // a started step that shows its label only from its first token or reasoning
    cutoffIndex: null, // messages from here on are hidden behind the live reply
    content: null, // the reply text the turn last made authoritative (Writer tokens or an announced rewrite)
    pendingUserMsg: null, // the optimistic user row this turn sent
    bodyEl: null, // the live reply bubble's body, while it has one
    exchangeId: null, // a group exchange's id; "pending" until its plan arrives
    plan: null, // a group exchange's speaking plan
    speaker: null, // the group speaker now replying
    completedIds: [], // saved replies of the group speakers who already finished
    liveGroupReplies: 0, // group replies shown live before an expressive speaker began buffering
    expressionBuffering: false, // replies are held for playback, from the first speaker with expressions on
    worldProposalArrived: false,
  };
}

/** A new turn: its request is out and nothing has answered yet. */
export function beginTurn() {
  return { ...idleTurn(), phase: "waiting" };
}

function move(turn, phase, fields) {
  if (!MOVES[turn.phase].includes(phase)) return false;
  Object.assign(turn, fields, { phase });
  return true;
}

/** A step that shows only from its first output (*label*), or a new group speaker (no label): wait for it. */
export function awaitStep(turn, label = "") {
  return move(turn, "waiting", { nextStep: label });
}

/** A core step that shows its label at once. */
export function runStep(turn, label) {
  return move(turn, "running", { step: label, nextStep: "" });
}

/** A workflow hook's label for its own step. A core step still waiting for its first output shows once that arrives. */
export function workflowStep(turn, label) {
  return move(turn, "running", { step: label });
}

/** A token or reasoning delta arrived: the awaited step, if any, now shows. */
export function stepOutput(turn) {
  return !!turn.nextStep && move(turn, "running", { step: turn.nextStep, nextStep: "" });
}

/** The stream ended. The refetch now owns the reply text, the cut and the optimistic user row. */
export function settleTurn(turn) {
  return move(turn, "settling", { step: "", nextStep: "", cutoffIndex: null, content: null, pendingUserMsg: null });
}

/** The status line's text: null when no step is showing. */
export function turnLabel(turn) {
  if (turn.phase === "waiting") return WAITING_LABEL;
  if (turn.phase === "running") return turn.step;
  return null;
}
