const state = {
  ws: null,
  app: null,
  term: null,
  fit: null,
  termWs: null,
  termTerminalId: "",
  termSessionId: "",
  terminalMode: false,
  terminalExclusive: false,
  pendingTerminalOpen: null,
  retriedTerminalOpen: false,
  returnSessionId: "",
  dirRoot: "",
  suggestions: [],
  selectedSuggestion: -1,
  renderedOutputKey: "",
  renderedOutputText: "",
  renderedOutputSession: "",
  renderedOutputView: "",
  renderedLayoutKey: "",
  renderedWizardKey: "",
  lastErrorMessage: "",
  lastErrorAt: 0,
  fitPending: false,
  creatingSession: false,
  lastSentTermSize: "",
  activePaneBySession: {},   // sessionId -> paneId
  webNav: {},                // paneKey -> { stack: [url], index, loaded }
  webFrames: {},             // paneKey -> persistent <iframe> (never reloaded on switch)
  activeWebKey: "",
  renderedPaneBarKey: "",
  dataTimer: null,
};

const els = {
  status: document.getElementById("status"),
  activityPill: document.getElementById("activity-pill"),
  links: document.getElementById("links"),
  tabs: document.getElementById("tabs"),
  viewToggle: document.getElementById("view-toggle"),
  captureBtn: document.getElementById("capture-btn"),
  capturePop: document.getElementById("capture-pop"),
  captureLabels: document.getElementById("capture-labels"),
  captureNotes: document.getElementById("capture-notes"),
  captureCancel: document.getElementById("capture-cancel"),
  toast: document.getElementById("toast"),
  terminal: document.getElementById("terminal"),
  terminalModeBar: document.getElementById("terminal-mode-bar"),
  termReturn: document.getElementById("term-return"),
  terminalPanel: document.getElementById("terminal-panel"),
  paneBar: document.getElementById("pane-bar"),
  webPane: document.getElementById("web-pane"),
  webFrames: document.getElementById("web-frames"),
  webUrl: document.getElementById("web-url"),
  webBack: document.getElementById("web-back"),
  webFwd: document.getElementById("web-fwd"),
  webReload: document.getElementById("web-reload"),
  webToggle: document.getElementById("web-toggle"),
  webProc: document.getElementById("web-proc"),
  webOpen: document.getElementById("web-open"),
  webPlaceholder: document.getElementById("web-placeholder"),
  dataPane: document.getElementById("data-pane"),
  composer: document.getElementById("composer"),
  message: document.getElementById("message"),
  suggestions: document.getElementById("suggestions"),
  noSession: document.getElementById("no-session"),
  wizard: document.getElementById("wizard"),
  wizardGrid: document.getElementById("wizard-grid"),
  browserPath: document.getElementById("browser-path"),
  browserList: document.getElementById("browser-list"),
  contextMenu: document.getElementById("context-menu"),
  dialog: document.getElementById("dialog"),
  dialogTitle: document.getElementById("dialog-title"),
  dialogBody: document.getElementById("dialog-body"),
  dialogCancel: document.getElementById("dialog-cancel"),
  dialogConfirm: document.getElementById("dialog-confirm"),
};

// Per-state Phosphor icons (inline SVG, animated for working/waiting) so it is
// scannable at a glance who is waiting on you vs busy. Bold weight, 256 viewBox.
const ICON_PATHS = {
  spinner: "M236,128a108,108,0,0,1-216,0c0-42.52,24.73-81.34,63-98.9A12,12,0,1,1,93,50.91C63.24,64.57,44,94.83,44,128a84,84,0,0,0,168,0c0-33.17-19.24-63.43-49-77.09A12,12,0,1,1,173,29.1C211.27,46.66,236,85.48,236,128Z",
  hand: "M188,44a32,32,0,0,0-8,1V44a32,32,0,0,0-60.79-14A32,32,0,0,0,76,60v50.83a32,32,0,0,0-52,36.7C55.82,214.6,75.35,244,128,244a92.1,92.1,0,0,0,92-92V76A32,32,0,0,0,188,44Zm8,108a68.08,68.08,0,0,1-68,68c-35.83,0-49.71-14-82.48-83.14-.14-.29-.29-.58-.45-.86a8,8,0,0,1,13.85-8l.21.35,18.68,30A12,12,0,0,0,100,152V60a8,8,0,0,1,16,0v60a12,12,0,0,0,24,0V44a8,8,0,0,1,16,0v76a12,12,0,0,0,24,0V76a8,8,0,0,1,16,0Z",
  check: "M176.49,95.51a12,12,0,0,1,0,17l-56,56a12,12,0,0,1-17,0l-24-24a12,12,0,1,1,17-17L112,143l47.51-47.52A12,12,0,0,1,176.49,95.51ZM236,128A108,108,0,1,1,128,20,108.12,108.12,0,0,1,236,128Zm-24,0a84,84,0,1,0-84,84A84.09,84.09,0,0,0,212,128Z",
  question: "M144,180a16,16,0,1,1-16-16A16,16,0,0,1,144,180Zm92-52A108,108,0,1,1,128,20,108.12,108.12,0,0,1,236,128Zm-24,0a84,84,0,1,0-84,84A84.09,84.09,0,0,0,212,128ZM128,64c-24.26,0-44,17.94-44,40v4a12,12,0,0,0,24,0v-4c0-8.82,9-16,20-16s20,7.18,20,16-9,16-20,16a12,12,0,0,0-12,12v8a12,12,0,0,0,23.73,2.56C158.31,137.88,172,122.37,172,104,172,81.94,152.26,64,128,64Z",
};
const ACTIVITY_ICON = {
  idle: { path: ICON_PATHS.check, anim: "" },
  "awaiting-selection": { path: ICON_PATHS.hand, anim: "act-pulse" },
  thinking: { path: ICON_PATHS.spinner, anim: "act-spin" },
  streaming: { path: ICON_PATHS.spinner, anim: "act-spin" },
  unknown: { path: ICON_PATHS.question, anim: "" },
};
function activityIconSvg(stateName) {
  const def = ACTIVITY_ICON[stateName] || ACTIVITY_ICON.unknown;
  return `<svg class="act-ico act-${stateName || "unknown"} ${def.anim}" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="${def.path}"/></svg>`;
}

function connect() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  state.ws = new WebSocket(`${protocol}//${location.host}/ws`);
  state.ws.onmessage = event => handleMessage(JSON.parse(event.data));
  state.ws.onopen = () => {
    if (state.pendingTerminalOpen && state.retriedTerminalOpen) {
      send("term.open", state.pendingTerminalOpen);
    }
  };
  state.ws.onclose = () => setTimeout(connect, 800);
}

function connectDevReload() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${protocol}//${location.host}/dev/reload`);
  ws.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.type === "reload") location.reload();
  };
}

setInterval(() => {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  // Refresh even with no active session and while attached in split terminal
  // mode, so sessions started elsewhere appear in the tabs. render() leaves the
  // live terminal untouched in terminal mode; only fullscreen suppresses polling.
  if (state.terminalExclusive) return;
  send("state");
}, 650);

function send(type, payload = {}) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  state.ws.send(JSON.stringify({ type, ...payload }));
}

function setupTerminal() {
  state.term = new Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: "'SFMono-Regular', Menlo, Monaco, ui-monospace, monospace",
    letterSpacing: 0,
    lineHeight: 1,
    drawBoldTextInBrightColors: false,
    scrollback: 10000,
    convertEol: true,
    theme: {
      background: "#0b0e14",
      foreground: "#eff4ff",
      cursor: "#2f7de1",
      selectionBackground: "rgba(47,125,225,0.35)",
      black: "#15181d",
      red: "#ff5c78",
      green: "#5ee1a2",
      yellow: "#f7c969",
      blue: "#6aa7ff",
      magenta: "#d28bff",
      cyan: "#58d5ff",
      white: "#e9edf3",
    },
  });
  state.fit = new FitAddon.FitAddon();
  state.term.loadAddon(state.fit);
  state.term.loadAddon(new WebLinksAddon.WebLinksAddon());
  if (window.WebglAddon) {
    try {
      const webgl = new WebglAddon.WebglAddon();
      webgl.onContextLoss(() => webgl.dispose());
      state.term.loadAddon(webgl);
      state.term.options.rescaleOverlappingGlyphs = true;
    } catch (_err) {
      // Canvas rendering is the intended fallback.
    }
  }
  state.term.open(els.terminal);
  state.fit.fit();
  state.term.onData(data => {
    if (!state.terminalMode) return;
    // Ctrl-b leaves fullscreen; in split it passes through to tmux as the prefix.
    if (data.includes("\x02") && state.terminalExclusive) {
      exitFullscreen();
      return;
    }
    sendTerminalData(data);
  });
  state.term.onResize(({ cols, rows }) => {
    if (state.termWs && state.termWs.readyState === WebSocket.OPEN) {
      state.termWs.send(JSON.stringify({ type: "resize", cols, rows }));
    }
    if (state.terminalMode && state.termSessionId) {
      send("term.resize", { session_id: state.termSessionId, cols, rows });
    }
  });
  window.addEventListener("resize", debounce(fitAfterWindowResize, 120));
}

function fitTerminalSoon() {
  if (!state.term || !state.fit || state.fitPending) return;
  state.fitPending = true;
  requestAnimationFrame(() => {
    state.fitPending = false;
    state.fit.fit();
  });
}

function fitAfterWindowResize() {
  if (!state.term || !state.fit) return;
  if (state.terminalMode) {
    resizeActiveTerminal();
    setTimeout(resizeActiveTerminal, 120);
    return;
  }
  state.fit.fit();
}

function resizeActiveTerminalRepeatedly() {
  resizeActiveTerminal();
  setTimeout(resizeActiveTerminal, 60);
  setTimeout(resizeActiveTerminal, 160);
  setTimeout(resizeActiveTerminal, 360);
  setTimeout(resizeActiveTerminal, 800);
}

function showCreatingSession(changes) {
  state.creatingSession = true;
  els.wizard.classList.add("hidden");
  els.noSession.classList.add("hidden");
  els.terminalPanel.classList.remove("hidden");
  els.composer.classList.add("hidden");
  hideSuggestions();
  if (state.term) {
    resetRenderedOutputState();
    resetRenderedTerminal();
    state.term.write("Creating session...\r\n");
    state.term.write("Starting the runtime and interactive terminal. This can take a few seconds.\r\n");
    fitTerminalSoon();
  }
  send("new.confirm", { changes });
}

function handleMessage(message) {
  if (message.type === "state") {
    state.creatingSession = false;
    render(message.state);
  }
  if (message.type === "suggestions") renderSuggestions(message.items || []);
  if (message.type === "train.captured") handleCaptured(message.data || {});
  if (message.type === "dir.entries") renderDirectory(message.data);
  if (message.type === "term.opened") connectTerminal(message.terminal_id);
  if (message.type === "error") {
    if (String(message.message || "").includes("term.open")) {
      if (!state.retriedTerminalOpen && state.pendingTerminalOpen) {
        state.retriedTerminalOpen = true;
        showError("Reconnecting to the refreshed yikes! web server...");
        if (state.ws) state.ws.close();
        return;
      }
      detachTerminalPane();
      showError("The browser control socket is stale. Hard-refresh this tab and retry /term.");
      return;
    }
    showError(message.message);
  }
}

function pruneWebFrames(next) {
  // Drop persistent iframes (and their nav state) for panes/sessions that no
  // longer exist — otherwise closed sessions leak their iframes.
  const valid = new Set();
  for (const session of next.sessions || []) {
    for (const pane of session.panes || []) {
      if (pane.kind === "web") valid.add(`${session.id}:${pane.id}`);
    }
  }
  for (const key of Object.keys(state.webFrames)) {
    if (valid.has(key)) continue;
    const frame = state.webFrames[key];
    if (frame && frame.parentNode) frame.parentNode.removeChild(frame);
    delete state.webFrames[key];
    delete state.webNav[key];
    if (state.activeWebKey === key) state.activeWebKey = "";
  }
}

function render(next) {
  state.app = next;
  pruneWebFrames(next);
  renderStatus(next.status, next.active_session_activity);
  renderTabs(next.sessions, next.active_session_id);
  renderViewToggle(next.output_view);
  renderWizard(next.pending_new);
  renderLinks(next.links || []);
  const noSession = !next.has_active_session && !next.pending_new;
  const activeSession = (next.sessions || []).find(s => s.id === next.active_session_id) || null;
  const panes = (activeSession && activeSession.panes) || [];
  const activePane = applyPanes(next, activeSession, panes, noSession);
  // Always show the sub-tab bar for a session with panes (even just Terminal),
  // so the "＋ web" add affordance is available without any yikes.toml.
  const showPaneBar = !noSession && panes.length > 0;
  els.paneBar.classList.toggle("hidden", !showPaneBar);
  // The "label this state" affordance is available whenever a live session is in
  // view; hide its popover if the session went away.
  // The training-label affordance is developer-only (off for normal users).
  const showCapture = !noSession && !!next.active_session_id && !!next.developer;
  els.captureBtn.classList.toggle("hidden", !showCapture);
  if (!showCapture) closeCapturePop();
  els.noSession.classList.toggle("hidden", !noSession);
  // Terminal pane (or a non-pane CLI session) uses the terminal panel; web/data
  // panes replace it.
  const terminalPaneActive = !activePane || activePane.kind === "terminal";
  els.terminalPanel.classList.toggle("hidden", noSession || !terminalPaneActive);
  els.webPane.classList.toggle("hidden", noSession || !activePane || activePane.kind !== "web");
  els.dataPane.classList.toggle("hidden", noSession || !activePane || activePane.kind !== "data");
  // Composer is only for non-interactive (no-pane) chat sessions; interactive
  // sessions are driven through their panes.
  const composerHidden = noSession || panes.length > 0;
  els.composer.classList.toggle("hidden", composerHidden);
  els.composer.classList.toggle("disabled", noSession);
  els.message.disabled = noSession;
  // The return-to-split control only matters in fullscreen.
  els.terminalModeBar.classList.toggle("hidden", !state.terminalExclusive);
  document.body.classList.toggle("terminal-exclusive", state.terminalExclusive);
  if (state.term) {
    let outputChanged = false;
    if (!state.terminalMode) {
      const output = noSession ? "" : (next.output_text || "");
      const outputSession = next.active_session_id || "";
      const outputView = next.output_view || "";
      const outputKey = `${noSession ? "no-session" : "session"}:${outputSession}:${outputView}:${output}`;
      if (outputKey !== state.renderedOutputKey) {
        state.renderedOutputKey = outputKey;
        outputChanged = true;
        const canAppend =
          !noSession &&
          outputSession === state.renderedOutputSession &&
          outputView === state.renderedOutputView &&
          output.startsWith(state.renderedOutputText);
        if (canAppend) {
          const delta = output.slice(state.renderedOutputText.length);
          state.term.write(delta.replace(/\n/g, "\r\n"), () => {
            state.term.scrollToBottom();
          });
        } else {
          resetRenderedTerminal();
          if (!noSession) {
            state.term.write(output.replace(/\n/g, "\r\n"), () => {
              state.term.scrollToBottom();
            });
          } else {
            state.term.write("", () => {
              state.term.scrollToBottom();
            });
          }
        }
        state.renderedOutputText = output;
        state.renderedOutputSession = outputSession;
        state.renderedOutputView = outputView;
      }
    }
    const layoutKey = [
      noSession ? "no-session" : "session",
      state.terminalMode ? "term" : "normal",
      state.terminalExclusive ? "exclusive" : "inline",
      next.active_session_id || "",
      next.pending_new ? "wizard" : "stable",
    ].join(":");
    if (outputChanged || layoutKey !== state.renderedLayoutKey) {
      state.renderedLayoutKey = layoutKey;
      fitTerminalSoon();
    }
  }
  if (next.error) showError(next.error);
}

function resetRenderedTerminal() {
  // xterm.clear() clears visible cells but can preserve the cursor position.
  // On tab switches that makes a shorter CLI transcript start after the last
  // tmux line. Reset keeps normal yikes! rendering independent per session.
  state.term.reset();
  state.term.write("\x1b[H\x1b[2J\x1b[3J");
}

function resetRenderedOutputState() {
  state.renderedOutputKey = "";
  state.renderedOutputText = "";
  state.renderedOutputSession = "";
  state.renderedOutputView = "";
}

function renderViewToggle(view) {
  const isDev = view === "dev";
  els.viewToggle.textContent = isDev ? "</>" : "{}";
  els.viewToggle.classList.toggle("active", isDev);
  els.viewToggle.title = isDev ? "Dev view: showing raw terminal pane" : "High-level view: showing prompts and answers";
}

function renderStatus(status, activity) {
  const items = [
    ["Backend", status.backend],
    ["Location", status.location],
    ["Driver", status.driver],
    ["Model", status.model, "model"],
    ["Complexity", status.complexity],
    ["Web", status.web, "web_search"],
    ["Capture", status.capture],
    ["Activity", activity?.label || "unknown"],
  ];
  els.status.replaceChildren(...items.flatMap(([key, value, control]) => {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    const editor = renderStatusControl(control, value);
    if (editor) dd.append(editor);
    else dd.textContent = value;
    return [dt, dd];
  }));
  renderActivity(activity);
}

function renderStatusControl(control, value) {
  const controls = state.app?.controls || {};
  if (!controls.editable) return null;
  if (control === "model") {
    const select = document.createElement("select");
    select.className = "status-select";
    const options = controls.model_options || ["default"];
    for (const item of options) {
      const option = document.createElement("option");
      option.value = item;
      option.textContent = item === "default" ? "(default)" : item;
      select.append(option);
    }
    select.value = controls.model || "default";
    select.onchange = () => send("config.update", { changes: { model: select.value } });
    return select;
  }
  if (control === "web_search") {
    const select = document.createElement("select");
    select.className = "status-select";
    for (const item of ["on", "off"]) {
      const option = document.createElement("option");
      option.value = item;
      option.textContent = item;
      select.append(option);
    }
    select.value = controls.web_search || (value === "enabled" ? "on" : "off");
    select.onchange = () => send("config.update", { changes: { web_search: select.value } });
    return select;
  }
  return null;
}

function renderActivity(activity) {
  const value = activity?.label || "unknown";
  const stateName = activity?.state || "unknown";
  els.activityPill.innerHTML = `${activityIconSvg(stateName)}<span>${escapeHtml(value)}</span>`;
  els.activityPill.title = activity?.reason || "";
  els.activityPill.className = `activity-pill activity-${stateName}`;
}

function renderTabs(sessions, activeId) {
  const plus = document.createElement("button");
  plus.className = "tab new-tab";
  plus.textContent = "+";
  plus.title = "New Session";
  plus.onclick = () => send("new.open");

  if (!sessions.length) {
    const empty = document.createElement("button");
    empty.className = "tab active";
    empty.style.display = "block";
    empty.innerHTML = "<span class='tab-title'>new session</span><span class='tab-meta'>not connected</span>";
    empty.onclick = () => send("new.open");
    els.tabs.replaceChildren(empty, plus);
    return;
  }
  els.tabs.replaceChildren(...sessions.map(session => {
    const tab = document.createElement("button");
    tab.className = `tab ${session.id === activeId ? "active" : ""}`;
    const actState = session.activity?.state || "unknown";
    const actLabel = session.activity ? ` · ${session.activity.label}` : "";
    const title = session.name || session.id;
    const dockerHint = session.runtime === "docker" ? " · docker" : "";
    tab.title = `${title} · ${session.backend}${dockerHint}${actLabel} (${session.id})`;
    tab.innerHTML = `${activityIconSvg(actState)}<span class="tab-content"><span class="tab-title">${escapeHtml(title)}${escapeHtml(dockerHint ? " · docker" : "")}</span><span class="tab-meta">${escapeHtml(session.backend)} ${escapeHtml(session.state)}${escapeHtml(actLabel)}</span></span><span class="tab-close" title="Close session">×</span>`;
    tab.onclick = () => {
      if (session.id !== activeId) send("session.switch", { session_id: session.id });
    };
    tab.querySelector(".tab-close").onclick = event => {
      event.stopPropagation();
      send("session.close", { session_id: session.id });
    };
    return tab;
  }), plus);
}

function renderWizard(draft) {
  els.wizard.classList.toggle("hidden", !draft);
  if (!draft) {
    state.renderedWizardKey = "";
    return;
  }
  const wizardKey = JSON.stringify({
    backend: draft.backend,
    location: draft.location,
    driver: draft.driver,
    model: draft.model,
    complexity: draft.complexity,
    web_search: draft.web_search,
    managed_output: draft.managed_output,
    root: draft.root || "",
    choices: draft.choices,
  });
  const active = document.activeElement;
  const focusedSelectInWizard = active?.tagName === "SELECT" && els.wizard.contains(active);
  if (wizardKey === state.renderedWizardKey || focusedSelectInWizard) {
    els.browserPath.textContent = draft.root || "No file access";
    return;
  }
  state.renderedWizardKey = wizardKey;
  const fields = [
    ["backend", "Backend"],
    ["location", "Location"],
    ["driver", "Driver"],
    ["model", "Model"],
    ["complexity", "Complexity"],
    ["web_search", "Web search"],
    ["managed_output", "Capture"],
  ];
  els.wizardGrid.replaceChildren(...fields.map(([key, label]) => {
    const wrap = document.createElement("label");
    wrap.className = "field";
    const title = document.createElement("span");
    title.textContent = label;
    const select = document.createElement("select");
    select.dataset.key = key;
    const values = (key === "web_search" || key === "managed_output") ? ["on", "off"] : (draft.choices[key] || []);
    for (const value of values) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.append(option);
    }
    select.value = key === "web_search"
      ? (draft.web_search ? "on" : "off")
      : key === "managed_output"
        ? (draft.managed_output ? "on" : "off")
        : draft[key];
    select.onchange = () => send("new.update", { changes: { [key]: select.value } });
    wrap.append(title, select);
    return wrap;
  }));
  els.browserPath.textContent = draft.root || "No file access";
}

function renderDirectory(data) {
  state.dirRoot = data.root || "";
  els.browserList.classList.remove("hidden");
  document.getElementById("root-current").classList.remove("hidden");
  els.browserPath.textContent = state.app?.pending_new?.root || data.root || "";
  const buttons = [];
  if (data.parent) {
    const up = document.createElement("button");
    up.className = "dir";
    up.textContent = "../";
    up.onclick = () => send("dir.list", { root: data.parent });
    buttons.push(up);
  }
  for (const entry of data.entries || []) {
    const button = document.createElement("button");
    button.className = "dir";
    button.textContent = entry.name;
    button.title = entry.path;
    button.onclick = () => send("dir.list", { root: entry.path });
    button.ondblclick = () => chooseRoot(entry.path);
    buttons.push(button);
  }
  els.browserList.replaceChildren(...buttons);
}

function chooseRoot(path) {
  els.browserList.classList.add("hidden");
  document.getElementById("root-current").classList.add("hidden");
  send("new.update", { changes: { root: path } });
}

function submitText(text) {
  if (handleLocalCommand(text)) return;
  send("submit", { text });
}

function handleLocalCommand(text) {
  const rawCommand = text.trim().split(/\s+/, 1)[0].toLowerCase();
  const command = resolveLocalCommand(rawCommand);
  if (command === "/term") {
    const active = state.app?.active_session_id;
    const session = (state.app?.sessions || []).find(s => s.id === active);
    const panes = (session && session.panes) || [];
    const terminalPane = panes.find(p => p.kind === "terminal");
    if (session && terminalPane) selectPane(session.id, terminalPane.id);
    return true;
  }
  if (command === "/fullscreen" || command === "/overtake") {
    if (!state.terminalMode) attachTerminalPane(state.app?.sessions?.find(s => s.id === state.app?.active_session_id) || {});
    enterFullscreen();
    return true;
  }
  if (command === "/resume") {
    exitFullscreen();
    return true;
  }
  return false;
}

function resolveLocalCommand(rawCommand) {
  const commands = ["/term", "/terminal", "/fullscreen", "/overtake", "/resume"];
  if (commands.includes(rawCommand)) return rawCommand;
  const matches = commands.filter(command => command.startsWith(rawCommand));
  return matches.length === 1 ? matches[0] : rawCommand;
}

// ---- Panes (sub-tabs) ----------------------------------------------------

function activePaneId(session, panes) {
  if (!session || !panes.length) return null;
  const stored = state.activePaneBySession[session.id];
  if (stored && panes.some(p => p.id === stored)) return stored;
  return panes[0].id;
}

function selectPane(sessionId, paneId) {
  state.activePaneBySession[sessionId] = paneId;
  state.renderedPaneBarKey = "";
  if (state.app) render(state.app);
}

function applyPanes(next, session, panes, noSession) {
  if (noSession || !session || !panes.length) {
    stopDataTimer();
    state.activeWebKey = "";
    return null;
  }
  const paneId = activePaneId(session, panes);
  const pane = panes.find(p => p.id === paneId) || panes[0];
  renderPaneBar(session, panes, pane.id);
  if (pane.kind === "terminal") {
    stopDataTimer();
    state.activeWebKey = "";
    attachTerminalPane(session);
  } else {
    detachTerminalPane();
    if (pane.kind === "web") {
      stopDataTimer();
      state.activeWebPane = { session, pane };
      showWebPane(session, pane);
      applyProcessControl();
    } else if (pane.kind === "data") {
      state.activeWebKey = "";
      showDataPane(session, pane);
    }
  }
  return pane;
}

function paneStatus(session, pane) {
  if (!pane.canControl) return "";
  return (state.app?.processes?.[`${session.id}:${pane.id}`] || {}).status || "stopped";
}

function renderPaneBar(session, panes, activeId) {
  const key = `${session.id}:${panes.map(p => `${p.id}/${p.title}/${paneStatus(session, p)}`).join("|")}:${activeId}`;
  if (key === state.renderedPaneBarKey) return;
  state.renderedPaneBarKey = key;
  const tabs = panes.map(p => {
    const tab = document.createElement("button");
    tab.className = `pane-tab ${p.id === activeId ? "active" : ""}`;
    const status = paneStatus(session, p);
    const dot = status ? `<span class="dot ${escapeHtml(status)}"></span>` : "";
    tab.innerHTML = `${dot}${escapeHtml(p.title)}`;
    tab.onclick = () => selectPane(session.id, p.id);
    return tab;
  });
  const add = document.createElement("button");
  add.className = "pane-tab pane-add";
  add.textContent = "＋ web";
  add.title = "Add a web view (enter a port or full URL)";
  add.onclick = () => showAddPaneInput(session.id);
  els.paneBar.replaceChildren(...tabs, add);
}

function showAddPaneInput(sessionId) {
  if (els.paneBar.querySelector(".pane-add-input")) return;
  const input = document.createElement("input");
  input.className = "pane-add-input";
  input.placeholder = "port or URL · Enter to add";
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") {
      const value = input.value.trim();
      input.remove();
      if (value) {
        state.renderedPaneBarKey = "";
        send("pane.add", { session_id: sessionId, value });
      }
    } else if (e.key === "Escape") {
      input.remove();
    }
  });
  els.paneBar.appendChild(input);
  input.focus();
}

function applyProcessControl() {
  const info = state.activeWebPane;
  if (!info || !info.pane.canControl) {
    els.webProc.classList.add("hidden");
    return;
  }
  const status = paneStatus(info.session, info.pane);
  const running = status === "running" || status === "starting";
  els.webProc.classList.remove("hidden");
  els.webProc.textContent = running ? "■ Stop" : "▶ Start";
  els.webProc.title = running ? "Stop the process" : "Start the process";
  els.webProc.onclick = () =>
    send(running ? "process.stop" : "process.start", { session_id: info.session.id, pane_id: info.pane.id });
}

function attachTerminalPane(session) {
  const interactive = session.runtime === "tmux" || session.runtime === "docker";
  if (!interactive || !state.term || state.creatingSession) return;
  // (Re)attach when not attached, or attached to a different session's terminal.
  if (state.terminalMode && state.attachTargetSession === session.id) return;
  openTerminalMode();
}

function detachTerminalPane() {
  if (!state.terminalMode) return;
  closeTerminalSocket();
  state.lastSentTermSize = "";
  state.terminalMode = false;
  state.terminalExclusive = false;
  state.pendingTerminalOpen = null;
  state.attachTargetSession = "";
  document.body.classList.remove("terminal-exclusive");
}

function enterFullscreen() {
  if (!state.terminalMode) return;
  state.terminalExclusive = true;
  document.body.classList.add("terminal-exclusive");
  resizeActiveTerminalRepeatedly();
}

function exitFullscreen() {
  state.terminalExclusive = false;
  document.body.classList.remove("terminal-exclusive");
  resizeActiveTerminalRepeatedly();
}

// ---- Web pane ------------------------------------------------------------

function resolveTargetUrl(spec) {
  const host = location.hostname || "localhost";
  if (spec.url) {
    let url = spec.url.split("{host}").join(host);
    if (spec.port) url = url.split("{port}").join(spec.port);
    return url;
  }
  if (spec.port) return `${location.protocol}//${host}:${spec.port}${spec.path || ""}`;
  return "";
}

// Each web pane keeps its OWN iframe, created once and only hidden when another
// pane is active — so switching panes never reloads the page or loses its state.
function webFrameFor(key, initialUrl) {
  let frame = state.webFrames[key];
  if (!frame) {
    frame = document.createElement("iframe");
    frame.title = "embedded page";
    frame.setAttribute("referrerpolicy", "no-referrer");
    if (initialUrl) frame.src = initialUrl;
    els.webFrames.appendChild(frame);
    state.webFrames[key] = frame;
  }
  return frame;
}

function showWebPane(session, pane) {
  const key = `${session.id}:${pane.id}`;
  if (!state.webNav[key]) {
    const url = resolveTargetUrl(pane);
    state.webNav[key] = { stack: url ? [url] : [], index: url ? 0 : -1, loaded: true };
    webFrameFor(key, url);
  }
  state.activeWebKey = key;
  // Show only this pane's iframe.
  for (const [frameKey, frame] of Object.entries(state.webFrames)) {
    frame.classList.toggle("hidden", frameKey !== key);
  }
  applyWebNav();
}

function currentWebNav() {
  return state.webNav[state.activeWebKey] || null;
}

function schemeFor(hostPart) {
  const host = hostPart.split(":")[0].toLowerCase();
  const isLocal =
    host === "localhost" ||
    /^127\./.test(host) ||
    /^10\./.test(host) ||
    /^192\.168\./.test(host) ||
    /^172\.(1[6-9]|2\d|3[01])\./.test(host) ||
    !host.includes(".");
  return isLocal ? "http" : "https"; // public hosts default to https
}

function applyWebNav() {
  const nav = currentWebNav();
  const frame = state.webFrames[state.activeWebKey];
  if (!nav || !frame) return;
  const url = nav.stack[nav.index] || "";
  // Don't clobber the URL field while the user is editing it.
  if (document.activeElement !== els.webUrl) els.webUrl.value = url;
  els.webOpen.href = url || "#";
  els.webBack.disabled = nav.index <= 0;
  els.webFwd.disabled = nav.index >= nav.stack.length - 1;
  els.webToggle.textContent = nav.loaded ? "⏻" : "▶";
  els.webToggle.title = nav.loaded ? "Unload (stop loading the page)" : "Load the page";
  els.webPlaceholder.classList.toggle("hidden", nav.loaded);
  if (nav.loaded) {
    frame.classList.remove("hidden");
  } else {
    els.webPlaceholder.textContent = "Page unloaded — click ▶ to load it.";
    frame.classList.add("hidden");
    if (frame.getAttribute("src")) frame.removeAttribute("src");
  }
}

function webGo(index, { reload } = {}) {
  const nav = currentWebNav();
  const frame = state.webFrames[state.activeWebKey];
  if (!nav || !frame) return;
  if (index !== undefined) nav.index = Math.max(0, Math.min(index, nav.stack.length - 1));
  nav.loaded = true;
  const url = nav.stack[nav.index] || "";
  if (url) frame.src = url; // back/forward/reload explicitly (re)load
  applyWebNav();
}

function webNavigateTo(rawUrl) {
  const nav = currentWebNav();
  const frame = state.webFrames[state.activeWebKey];
  if (!nav || !frame) return;
  let url = rawUrl.trim();
  if (!url) return;
  if (!/^[a-zA-Z][\w+.-]*:\/\//.test(url)) url = schemeFor(url.split("/")[0]) + "://" + url;
  nav.stack = nav.stack.slice(0, nav.index + 1);
  nav.stack.push(url);
  nav.index = nav.stack.length - 1;
  nav.loaded = true;
  frame.src = url;
  applyWebNav();
}

// ---- Data pane -----------------------------------------------------------

function stopDataTimer() {
  if (state.dataTimer) {
    clearInterval(state.dataTimer);
    state.dataTimer = null;
  }
}

function showDataPane(session, pane) {
  const key = `${session.id}:${pane.id}`;
  if (state.activeDataKey === key) return;
  state.activeDataKey = key;
  stopDataTimer();
  const refresh = Math.max(1, Number(pane.refresh) || 5);
  const load = () => loadDataPane(session, pane);
  load();
  state.dataTimer = setInterval(load, refresh * 1000);
}

function loadDataPane(session, pane) {
  const url = pane.source && pane.source.startsWith("builtin:") ? null : resolveTargetUrl(pane);
  if (!url) {
    // builtin: render from the session summary we already have
    renderDataTable([
      { key: "session", value: session.name || session.id },
      { key: "backend", value: session.backend },
      { key: "runtime", value: session.runtime },
      { key: "state", value: session.state },
      { key: "activity", value: (session.activity && session.activity.label) || state.app?.active_session_activity?.label || "—" },
    ]);
    return;
  }
  fetch(url, { headers: { accept: "application/json" } })
    .then(r => r.json())
    .then(data => {
      const rows = Array.isArray(data)
        ? data.map((v, i) => ({ key: String(i), value: JSON.stringify(v) }))
        : Object.entries(data).map(([k, v]) => ({ key: k, value: typeof v === "object" ? JSON.stringify(v) : String(v) }));
      renderDataTable(rows);
    })
    .catch(err => renderDataTable([{ key: "error", value: String(err) }]));
}

function renderDataTable(rows) {
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>field</th><th>value</th></tr></thead>";
  const body = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(row.key)}</td><td>${escapeHtml(row.value)}</td>`;
    body.appendChild(tr);
  }
  table.appendChild(body);
  els.dataPane.replaceChildren(table);
}

// ---- Sidebar links -------------------------------------------------------

function renderLinks(links) {
  if (!els.links) return;
  els.links.replaceChildren(...links.map(link => {
    const a = document.createElement("a");
    a.className = "sidebar-link";
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = link.title;
    a.href = resolveTargetUrl(link) || "#";
    return a;
  }));
  els.links.classList.toggle("hidden", !links.length);
}

function openTerminalMode() {
  // Attach the live terminal for the active session (split). Visibility (the
  // pane bar, composer, fullscreen) is owned by render()/applyPanes.
  if (!state.app?.active_session_id) return;
  closeTerminalSocket();
  state.lastSentTermSize = "";
  state.terminalMode = true;
  state.attachTargetSession = state.app.active_session_id || "";
  state.returnSessionId = state.app.active_session_id || "";
  resetRenderedOutputState();
  state.renderedLayoutKey = "";
  if (!state.term) return;
  state.term.clear();
  state.term.write("Connecting to tmux...\r\n");
  requestAnimationFrame(() => {
    resizeActiveTerminalRepeatedly();
    state.term.focus();
    state.pendingTerminalOpen = {
      session_id: state.app.active_session_id,
      cols: state.term.cols,
      rows: state.term.rows,
    };
    state.retriedTerminalOpen = false;
    send("term.open", state.pendingTerminalOpen);
    resizeActiveTerminalRepeatedly();
  });
}

function connectTerminal(terminalId) {
  closeTerminalSocket();
  state.termTerminalId = terminalId;
  state.termSessionId = state.pendingTerminalOpen?.session_id || state.app?.active_session_id || "";
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const cols = state.term ? state.term.cols : 120;
  const rows = state.term ? state.term.rows : 34;
  state.termWs = new WebSocket(`${protocol}//${location.host}/ws/terminal/${terminalId}?cols=${cols}&rows=${rows}`);
  state.termWs.binaryType = "arraybuffer";
  state.termWs.onopen = () => {
    state.pendingTerminalOpen = null;
    state.retriedTerminalOpen = false;
    if (!state.term) return;
    state.term.clear();
    state.term.focus();
    resizeActiveTerminalRepeatedly();
  };
  state.termWs.onmessage = event => {
    if (!state.term || !(event.data instanceof ArrayBuffer)) return;
    state.term.write(new TextDecoder().decode(event.data));
  };
  state.termWs.onclose = () => {
    state.termWs = null;
  };
}

function sendTerminalData(data) {
  if (!state.termWs || state.termWs.readyState !== WebSocket.OPEN) return;
  state.termWs.send(new TextEncoder().encode(data));
}

function resizeActiveTerminal() {
  if (!state.term || !state.terminalMode) return;
  state.fit.fit(); // reflow xterm locally now; defer the (expensive) backend resize
  queueTerminalResizeSend();
}

let _resizeSendTimer = null;
function queueTerminalResizeSend() {
  // Collapse the burst of staggered fits into a single resize sent once the
  // layout has settled. Each resize sent to tmux makes the backend TUI repaint
  // its whole screen, so sending intermediate sizes stacks duplicate frames.
  if (_resizeSendTimer) clearTimeout(_resizeSendTimer);
  _resizeSendTimer = setTimeout(() => {
    if (!state.term || !state.terminalMode) return;
    const cols = state.term.cols;
    const rows = state.term.rows;
    const size = `${cols}x${rows}`;
    if (size === state.lastSentTermSize) return;
    state.lastSentTermSize = size;
    if (state.termWs && state.termWs.readyState === WebSocket.OPEN) {
      state.termWs.send(JSON.stringify({ type: "resize", cols, rows }));
    }
    if (state.termSessionId) {
      send("term.resize", { session_id: state.termSessionId, cols, rows });
    }
  }, 150);
}

function closeTerminalSocket() {
  if (state.termWs) {
    state.termWs.close();
    state.termWs = null;
  }
  if (state.termTerminalId) {
    send("term.close", { terminal_id: state.termTerminalId });
    state.termTerminalId = "";
  }
  state.termSessionId = "";
}

function renderSuggestions(items) {
  state.suggestions = items;
  if (state.selectedSuggestion >= items.length) state.selectedSuggestion = items.length - 1;
  if (state.selectedSuggestion < 0 && items.length) state.selectedSuggestion = 0;
  els.suggestions.classList.toggle("hidden", !items.length);
  els.suggestions.replaceChildren(...items.map((item, index) => {
    const row = document.createElement("div");
    row.className = `suggestion ${index === state.selectedSuggestion ? "selected" : ""}`;
    row.innerHTML = `<code>${escapeHtml(item.value)}</code><span>${escapeHtml(item.description || "")}</span>`;
    row.onmouseenter = () => selectSuggestion(index);
    row.onclick = () => chooseSuggestion(item);
    return row;
  }));
}

function selectSuggestion(index) {
  if (!state.suggestions.length) return;
  state.selectedSuggestion = Math.max(0, Math.min(index, state.suggestions.length - 1));
  renderSuggestions(state.suggestions);
}

function moveSuggestion(delta) {
  if (!state.suggestions.length) return;
  const next = state.selectedSuggestion < 0 ? 0 : state.selectedSuggestion + delta;
  state.selectedSuggestion = (next + state.suggestions.length) % state.suggestions.length;
  renderSuggestions(state.suggestions);
}

function chooseSelectedSuggestion() {
  if (state.selectedSuggestion < 0 || !state.suggestions[state.selectedSuggestion]) return false;
  chooseSuggestion(state.suggestions[state.selectedSuggestion]);
  return true;
}

function chooseSuggestion(item) {
  const completion = item.completion || item.value;
  hideSuggestions();
  if (completion.endsWith(" ")) {
    els.message.value = completion;
    els.message.focus();
    return;
  }
  els.message.value = "";
  submitText(completion);
}

function hideSuggestions() {
  state.suggestions = [];
  state.selectedSuggestion = -1;
  els.suggestions.classList.add("hidden");
  els.suggestions.replaceChildren();
}

function showError(message) {
  if (!message) return;
  const now = Date.now();
  if (message === state.lastErrorMessage && now - state.lastErrorAt < 5000) return;
  state.lastErrorMessage = message;
  state.lastErrorAt = now;
  if (state.term) state.term.write(`\r\n\x1b[31mError:\x1b[0m ${message}\r\n`);
}

function wizardFormChanges() {
  const changes = {};
  els.wizard.querySelectorAll("select[data-key]").forEach(select => {
    changes[select.dataset.key] = select.value;
  });
  return changes;
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);
}

function debounce(fn, wait) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

function openContextMenu(event) {
  event.preventDefault();
  els.contextMenu.replaceChildren();
  const closeAll = document.createElement("button");
  closeAll.textContent = "Close All Sessions";
  closeAll.onclick = () => {
    hideContextMenu();
    openConfirmDialog({
      title: "Close all sessions?",
      body: "This will close every known yikes! tmux, Docker, and remote session.",
      confirmText: "Close All",
      onConfirm: () => send("session.close_all"),
    });
  };
  els.contextMenu.append(closeAll);
  els.contextMenu.style.left = `${event.clientX}px`;
  els.contextMenu.style.top = `${event.clientY}px`;
  els.contextMenu.classList.remove("hidden");
}

function hideContextMenu() {
  els.contextMenu.classList.add("hidden");
}

function openConfirmDialog({ title, body, confirmText, onConfirm }) {
  els.dialogTitle.textContent = title;
  els.dialogBody.textContent = body;
  els.dialogConfirm.textContent = confirmText;
  els.dialogConfirm.onclick = () => {
    els.dialog.classList.add("hidden");
    onConfirm();
  };
  els.dialog.classList.remove("hidden");
}

document.getElementById("empty-new").onclick = () => send("new.open");
document.getElementById("wizard-cancel").onclick = () => send("new.cancel");
document.getElementById("wizard-create").onclick = () => showCreatingSession(wizardFormChanges());
document.getElementById("root-none").onclick = () => chooseRoot("");
document.getElementById("root-start").onclick = () => chooseRoot(state.app?.start_cwd || "");
document.getElementById("root-browse").onclick = () => send("dir.list", { root: state.app?.pending_new?.root || "" });
document.getElementById("root-current").onclick = () => chooseRoot(state.dirRoot || "");
els.viewToggle.onclick = () => {
  const next = state.app?.output_view === "dev" ? "high" : "dev";
  submitText(`/view ${next}`);
};
els.termReturn.onclick = () => exitFullscreen();

// Web pane navigation
els.webBack.onclick = () => { const n = currentWebNav(); if (n) webGo(n.index - 1, { reload: true }); };
els.webFwd.onclick = () => { const n = currentWebNav(); if (n) webGo(n.index + 1, { reload: true }); };
els.webReload.onclick = () => webGo(undefined, { reload: true });
els.webToggle.onclick = () => { const n = currentWebNav(); if (!n) return; n.loaded = !n.loaded; if (n.loaded) webGo(undefined, { reload: true }); else applyWebNav(); };
els.webUrl.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); webNavigateTo(els.webUrl.value); } });
els.tabs.addEventListener("contextmenu", openContextMenu);
document.addEventListener("click", event => {
  if (!els.contextMenu.contains(event.target)) hideContextMenu();
});
els.dialogCancel.onclick = () => els.dialog.classList.add("hidden");
els.dialog.addEventListener("click", event => {
  if (event.target === els.dialog) els.dialog.classList.add("hidden");
});

els.composer.onsubmit = event => {
  event.preventDefault();
  const text = els.message.value.trim();
  if (!text) return;
  submitText(text);
  els.message.value = "";
  hideSuggestions();
};

els.message.addEventListener("input", () => {
  const text = els.message.value;
  if (text.startsWith("/")) {
    state.selectedSuggestion = 0;
    send("suggest", { text });
  } else {
    hideSuggestions();
  }
});

els.message.addEventListener("keydown", event => {
  if (event.key === "Escape") {
    hideSuggestions();
    hideContextMenu();
    els.dialog.classList.add("hidden");
    return;
  }
  if (!state.suggestions.length) return;
  if (event.key === "ArrowDown") {
    event.preventDefault();
    moveSuggestion(1);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    moveSuggestion(-1);
  } else if (event.key === "Enter") {
    if (chooseSelectedSuggestion()) event.preventDefault();
  }
});

// ── Training capture: label the live terminal state ──────────────────────────
const CAPTURE_LABELS = ["idle", "thinking", "streaming", "awaiting-selection", "unknown"];
let toastTimer = null;

function showToast(message, kind = "info") {
  els.toast.textContent = message;
  els.toast.className = `toast toast-${kind}`;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toast.classList.add("hidden"), 4200);
}

function buildCaptureLabels() {
  els.captureLabels.replaceChildren(...CAPTURE_LABELS.map(label => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `capture-label activity-${label}`;
    btn.textContent = label;
    btn.onclick = () => captureWith(label);
    return btn;
  }));
}

function openCapturePop() {
  const sessionId = state.app?.active_session_id;
  if (!sessionId) return;
  els.capturePop.classList.remove("hidden");
  els.captureNotes.value = "";
  // Let the user type a note without the live terminal swallowing keystrokes.
  setTimeout(() => els.captureNotes.focus(), 0);
}

function closeCapturePop() {
  els.capturePop.classList.add("hidden");
}

function captureWith(label) {
  const sessionId = state.app?.active_session_id;
  if (!sessionId) return;
  closeCapturePop();
  showToast(`Capturing "${label}"…`, "info");
  send("train.capture", { session_id: sessionId, label, notes: els.captureNotes.value.trim() });
}

function handleCaptured(data) {
  if (!data.ok) {
    showToast(`Capture failed: ${data.error || "unknown error"}`, "error");
    return;
  }
  const verdict = data.matches
    ? `yikes agreed (${data.predicted})`
    : `yikes had predicted ${data.predicted}`;
  showToast(`Saved ${data.frames} frame(s) as "${data.label}" — ${verdict}`, data.matches ? "ok" : "warn");
}

els.captureBtn.addEventListener("click", () => {
  if (els.capturePop.classList.contains("hidden")) openCapturePop();
  else closeCapturePop();
});
els.captureCancel.addEventListener("click", closeCapturePop);
// Global hotkey (Ctrl+Alt+L) — capture phase so it fires even while the live
// terminal has focus and would otherwise swallow the keystroke.
document.addEventListener("keydown", event => {
  if (event.ctrlKey && event.altKey && (event.key === "l" || event.key === "L")) {
    event.preventDefault();
    event.stopPropagation();
    if (state.app?.active_session_id) openCapturePop();
  } else if (event.key === "Escape" && !els.capturePop.classList.contains("hidden")) {
    closeCapturePop();
  }
}, true);
buildCaptureLabels();

setupTerminal();
fetch("/api/state").then(resp => resp.json()).then(render).catch(() => {});
connect();
connectDevReload();
