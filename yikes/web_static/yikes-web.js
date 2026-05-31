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
  terminal: document.getElementById("terminal"),
  terminalModeBar: document.getElementById("terminal-mode-bar"),
  termReturn: document.getElementById("term-return"),
  terminalPanel: document.getElementById("terminal-panel"),
  paneBar: document.getElementById("pane-bar"),
  webPane: document.getElementById("web-pane"),
  webFrame: document.getElementById("web-frame"),
  webUrl: document.getElementById("web-url"),
  webBack: document.getElementById("web-back"),
  webFwd: document.getElementById("web-fwd"),
  webReload: document.getElementById("web-reload"),
  webToggle: document.getElementById("web-toggle"),
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

function render(next) {
  state.app = next;
  renderStatus(next.status, next.active_session_activity);
  renderTabs(next.sessions, next.active_session_id);
  renderViewToggle(next.output_view);
  renderWizard(next.pending_new);
  renderLinks(next.links || []);
  const noSession = !next.has_active_session && !next.pending_new;
  const activeSession = (next.sessions || []).find(s => s.id === next.active_session_id) || null;
  const panes = (activeSession && activeSession.panes) || [];
  const activePane = applyPanes(next, activeSession, panes, noSession);
  const showPaneBar = !noSession && panes.length > 1;
  els.paneBar.classList.toggle("hidden", !showPaneBar);
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
  els.activityPill.textContent = value;
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
    empty.innerHTML = "<span class='tab-title'>new session</span><span class='tab-meta'>not connected</span>";
    empty.onclick = () => send("new.open");
    els.tabs.replaceChildren(empty, plus);
    return;
  }
  els.tabs.replaceChildren(...sessions.map(session => {
    const tab = document.createElement("button");
    tab.className = `tab ${session.id === activeId ? "active" : ""}`;
    const activity = session.activity ? ` · ${session.activity.label}` : "";
    const title = session.name || session.id;
    const dockerHint = session.runtime === "docker" ? " · docker" : "";
    tab.title = `${title} · ${session.backend}${dockerHint} (${session.id})`;
    tab.innerHTML = `<span class="tab-content"><span class="tab-title">${escapeHtml(title)}${escapeHtml(dockerHint ? " · docker" : "")}</span><span class="tab-meta">${escapeHtml(session.backend)} ${escapeHtml(session.state)}${escapeHtml(activity)}</span></span><span class="tab-close" title="Close session">×</span>`;
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
      showWebPane(session, pane);
    } else if (pane.kind === "data") {
      state.activeWebKey = "";
      showDataPane(session, pane);
    }
  }
  return pane;
}

function renderPaneBar(session, panes, activeId) {
  const key = `${session.id}:${panes.map(p => `${p.id}/${p.title}/${p.status || ""}`).join("|")}:${activeId}`;
  if (key === state.renderedPaneBarKey) return;
  state.renderedPaneBarKey = key;
  els.paneBar.replaceChildren(...panes.map(p => {
    const tab = document.createElement("button");
    tab.className = `pane-tab ${p.id === activeId ? "active" : ""}`;
    const dot = p.status ? `<span class="dot ${escapeHtml(p.status)}"></span>` : "";
    tab.innerHTML = `${dot}${escapeHtml(p.title)}`;
    tab.onclick = () => selectPane(session.id, p.id);
    return tab;
  }));
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

function showWebPane(session, pane) {
  const key = `${session.id}:${pane.id}`;
  if (state.activeWebKey === key && state.webNav[key]) return;
  state.activeWebKey = key;
  if (!state.webNav[key]) {
    const url = resolveTargetUrl(pane);
    state.webNav[key] = { stack: url ? [url] : [], index: url ? 0 : -1, loaded: true };
  }
  applyWebNav();
}

function currentWebNav() {
  return state.webNav[state.activeWebKey] || null;
}

function applyWebNav() {
  const nav = currentWebNav();
  if (!nav) return;
  const url = nav.stack[nav.index] || "";
  els.webUrl.value = url;
  els.webOpen.href = url || "#";
  els.webBack.disabled = nav.index <= 0;
  els.webFwd.disabled = nav.index >= nav.stack.length - 1;
  els.webToggle.textContent = nav.loaded ? "⏻" : "▶";
  els.webToggle.title = nav.loaded ? "Unload (stop loading the page)" : "Load the page";
  els.webPlaceholder.classList.toggle("hidden", nav.loaded);
  if (nav.loaded) {
    if (url && els.webFrame.getAttribute("src") !== url) els.webFrame.src = url;
  } else {
    els.webPlaceholder.textContent = "Page unloaded — click ▶ to load it.";
    els.webFrame.removeAttribute("src");
  }
}

function webGo(index, { reload } = {}) {
  const nav = currentWebNav();
  if (!nav) return;
  if (index !== undefined) nav.index = Math.max(0, Math.min(index, nav.stack.length - 1));
  nav.loaded = true;
  const url = nav.stack[nav.index] || "";
  if (reload && url) els.webFrame.src = url;            // force a fresh load
  applyWebNav();
  if (reload && url && els.webFrame.getAttribute("src") === url) {
    // ensure reload even when src is unchanged
    els.webFrame.contentWindow && (els.webFrame.src = url);
  }
}

function webNavigateTo(rawUrl) {
  const nav = currentWebNav();
  if (!nav) return;
  let url = rawUrl.trim();
  if (!url) return;
  if (!/^[a-zA-Z][\w+.-]*:\/\//.test(url)) url = "http://" + url;
  nav.stack = nav.stack.slice(0, nav.index + 1);
  nav.stack.push(url);
  nav.index = nav.stack.length - 1;
  nav.loaded = true;
  els.webFrame.src = url;
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

setupTerminal();
fetch("/api/state").then(resp => resp.json()).then(render).catch(() => {});
connect();
connectDevReload();
