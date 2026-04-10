#!/usr/bin/env node
/**
 * XState Graph Bridge
 *
 * Reads an XState v5 machine definition from stdin, uses @xstate/graph to
 * compute all reachable paths, and outputs structured JSON on stdout.
 *
 * Usage:
 *   cat machine.json | node scripts/xstate_paths.js
 *   python3 scripts/formal_models.py convert model.json --format xstate | node scripts/xstate_paths.js
 *
 * Output: JSON object with { paths, summary }
 * Errors: JSON object on stderr with { error, detail }
 */

const { createMachine } = require("xstate");
const { getSimplePaths, getShortestPaths } = require("@xstate/graph");

function fatal(msg, detail) {
  process.stderr.write(JSON.stringify({ error: msg, detail }) + "\n");
  process.exit(1);
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => (data += chunk));
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

/**
 * Walk the machine config, collect guard/action names, and register stub
 * implementations so XState doesn't throw on unknown references.
 */
function collectImplementations(config) {
  const guards = {};
  const actions = {};

  function walkTransition(t) {
    if (typeof t === "string") return;
    if (Array.isArray(t)) return t.forEach(walkTransition);
    if (t.guard) guards[t.guard] = () => true;
    if (t.actions) {
      for (const a of t.actions) {
        const name = typeof a === "string" ? a : a.type;
        if (name) actions[name] = () => {};
      }
    }
  }

  for (const state of Object.values(config.states || {})) {
    if (state.on) {
      for (const t of Object.values(state.on)) walkTransition(t);
    }
    for (const arr of [state.entry, state.exit]) {
      if (!arr) continue;
      for (const a of arr) {
        const name = typeof a === "string" ? a : a.type;
        if (name) actions[name] = () => {};
      }
    }
  }

  return { guards, actions };
}

function stateValue(state) {
  return typeof state.value === "string" ? state.value : JSON.stringify(state.value);
}

async function main() {
  let raw;
  try {
    raw = await readStdin();
  } catch (e) {
    fatal("Failed to read stdin", e.message);
  }

  if (!raw.trim()) {
    fatal("Empty input", "Expected XState machine JSON on stdin");
  }

  let config;
  try {
    config = JSON.parse(raw);
  } catch (e) {
    fatal("Invalid JSON", e.message);
  }

  const impl = collectImplementations(config);

  let machine;
  try {
    machine = createMachine(config, impl);
  } catch (e) {
    fatal("Failed to create XState machine", e.message);
  }

  // getSimplePaths returns array of { state, weight, steps }
  // each step is { state, event }
  let rawPaths;
  try {
    rawPaths = getSimplePaths(machine);
  } catch (e) {
    fatal("Failed to compute paths", e.message);
  }

  const statesCovered = new Set();
  const transitionsCovered = new Set();
  const paths = [];

  for (const pathData of rawPaths) {
    const targetState = stateValue(pathData.state);
    statesCovered.add(targetState);

    // Each step has { state, event } where state is the state REACHED and
    // event is the event that caused the transition INTO that state.
    // To get (from, event, to) triples we pair consecutive steps:
    //   step[i].state --step[i+1].event--> step[i+1].state
    const steps = [];
    const rawSteps = pathData.steps;

    for (let i = 0; i < rawSteps.length - 1; i++) {
      const from = stateValue(rawSteps[i].state);
      const event = rawSteps[i + 1].event.type;
      const to = stateValue(rawSteps[i + 1].state);

      // Skip synthetic xstate.init transitions
      if (event === "xstate.init") continue;

      statesCovered.add(from);
      statesCovered.add(to);
      transitionsCovered.add(`${from}--${event}-->${to}`);

      steps.push({ state: from, event, next_state: to });
    }

    // Only include paths that have actual transitions
    if (steps.length > 0) {
      paths.push({
        target: targetState,
        steps,
        length: steps.length,
      });
    }
  }

  // Shortest paths for coverage summary
  let shortestCount = 0;
  try {
    const sp = getShortestPaths(machine);
    shortestCount = sp.length;
  } catch (_) {
    // Non-fatal
  }

  const output = {
    paths,
    summary: {
      total_paths: paths.length,
      states_covered: statesCovered.size,
      states_total: Object.keys(config.states || {}).length,
      transitions_covered: transitionsCovered.size,
      shortest_paths_count: shortestCount,
    },
  };

  process.stdout.write(JSON.stringify(output, null, 2) + "\n");
}

main();
