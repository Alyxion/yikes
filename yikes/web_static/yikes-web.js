const state = {
  ws: null,
  app: null,
  term: null,
  fit: null,
  termWs: null,
  termTerminalId: "",
  terminalMode: false,
  terminalExclusive: false,
  pendingTerminalOpen: null,
  retriedTerminalOpen: false,
  dirRoot: "",
  suggestions: [],
  selectedSuggestion: -1,
};

const els = {
  status: document.getElementById("status"),
  tabs: document.getElementById("tabs"),
  terminal: document.getElementById("terminal"),
  terminalModeBar: document.getElementById("terminal-mode-bar"),
  termReturn: document.getElementById("term-return"),
  terminalPanel: document.getElementById("terminal-panel"),
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
    if (data.includes("\x02")) {
      closeTerminalMode();
      return;
    }
    sendTerminalData(data);
  });
  state.term.onResize(({ cols, rows }) => {
    if (state.termWs && state.termWs.readyState === WebSocket.OPEN) {
      state.termWs.send(JSON.stringify({ type: "resize", cols, rows }));
    }
  });
  window.addEventListener("resize", debounce(() => state.fit.fit(), 180));
}

function handleMessage(message) {
  if (message.type === "state") render(message.state);
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
      closeTerminalMode();
      showError("The browser control socket is stale. Hard-refresh this tab and retry /term.");
      return;
    }
    showError(message.message);
  }
}

function render(next) {
  state.app = next;
  renderStatus(next.status);
  renderTabs(next.sessions, next.active_session_id);
  renderWizard(next.pending_new);
  const noSession = !next.has_active_session && !next.pending_new;
  els.noSession.classList.toggle("hidden", !noSession);
  els.terminalPanel.classList.toggle("hidden", noSession);
  els.composer.classList.toggle("hidden", state.terminalExclusive);
  els.composer.classList.toggle("disabled", noSession);
  els.message.disabled = false;
  els.terminalModeBar.classList.toggle("hidden", !state.terminalMode);
  document.body.classList.toggle("terminal-exclusive", state.terminalExclusive);
  if (state.term) {
    if (!state.terminalMode) {
      state.term.clear();
      if (noSession) {
        state.term.write("");
      } else {
        state.term.write((next.output_text || "").replace(/\n/g, "\r\n"));
      }
    }
    requestAnimationFrame(() => state.fit.fit());
  }
  if (next.error) showError(next.error);
}

function renderStatus(status) {
  const items = [
    ["Backend", status.backend],
    ["Location", status.location],
    ["Driver", status.driver],
    ["Model", status.model],
    ["Complexity", status.complexity],
    ["Web", status.web],
  ];
  els.status.replaceChildren(...items.flatMap(([key, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = value;
    return [dt, dd];
  }));
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
    tab.innerHTML = `<span class="tab-title">${escapeHtml(session.id)}</span><span class="tab-meta">${escapeHtml(session.runtime)}/${escapeHtml(session.backend)} ${escapeHtml(session.state)}</span>`;
    tab.onclick = () => send("session.switch", { session_id: session.id });
    return tab;
  }), plus);
}

function renderWizard(draft) {
  els.wizard.classList.toggle("hidden", !draft);
  if (!draft) return;
  const fields = [
    ["backend", "Backend"],
    ["location", "Location"],
    ["driver", "Driver"],
    ["model", "Model"],
    ["complexity", "Complexity"],
    ["web_search", "Web search"],
  ];
  els.wizardGrid.replaceChildren(...fields.map(([key, label]) => {
    const wrap = document.createElement("label");
    wrap.className = "field";
    const title = document.createElement("span");
    title.textContent = label;
    const select = document.createElement("select");
    const values = key === "web_search" ? ["on", "off"] : (draft.choices[key] || []);
    for (const value of values) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.append(option);
    }
    select.value = key === "web_search" ? (draft.web_search ? "on" : "off") : draft[key];
    select.onchange = () => send("new.update", { changes: { [key]: select.value } });
    wrap.append(title, select);
    return wrap;
  }));
  els.browserPath.textContent = draft.root || "No file access";
  if (!state.dirRoot) send("dir.list", {});
}

function renderDirectory(data) {
  state.dirRoot = data.root || "";
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
  send("new.update", { changes: { root: path } });
}

function submitText(text) {
  if (handleLocalCommand(text)) return;
  send("submit", { text });
}

function handleLocalCommand(text) {
  const command = text.trim().split(/\s+/, 1)[0].toLowerCase();
  if (command === "/term") {
    openTerminalMode({ exclusive: false });
    return true;
  }
  if (command === "/fullscreen" || command === "/overtake") {
    openTerminalMode({ exclusive: true });
    return true;
  }
  if (command === "/resume") {
    closeTerminalMode();
    return true;
  }
  return false;
}

function openTerminalMode({ exclusive }) {
  if (!state.app?.active_session_id) {
    showError("No active tmux session is selected.");
    return;
  }
  closeTerminalSocket();
  state.terminalMode = true;
  state.terminalExclusive = Boolean(exclusive);
  document.body.classList.toggle("terminal-exclusive", state.terminalExclusive);
  els.terminalModeBar.classList.remove("hidden");
  els.composer.classList.toggle("hidden", state.terminalExclusive);
  if (!state.term) return;
  state.term.clear();
  state.term.write("Connecting to tmux...\r\n");
  requestAnimationFrame(() => {
    state.fit.fit();
    state.term.focus();
    state.pendingTerminalOpen = {
      session_id: state.app.active_session_id,
      cols: state.term.cols,
      rows: state.term.rows,
    };
    state.retriedTerminalOpen = false;
    send("term.open", state.pendingTerminalOpen);
  });
}

function connectTerminal(terminalId) {
  closeTerminalSocket();
  state.termTerminalId = terminalId;
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

function closeTerminalSocket() {
  if (state.termWs) {
    state.termWs.close();
    state.termWs = null;
  }
  if (state.termTerminalId) {
    send("term.close", { terminal_id: state.termTerminalId });
    state.termTerminalId = "";
  }
}

function closeTerminalMode() {
  closeTerminalSocket();
  state.terminalMode = false;
  state.terminalExclusive = false;
  state.pendingTerminalOpen = null;
  state.retriedTerminalOpen = false;
  document.body.classList.remove("terminal-exclusive");
  els.terminalModeBar.classList.add("hidden");
  els.composer.classList.remove("hidden");
  send("state");
  els.message.focus();
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
  if (state.term) state.term.write(`\r\n\x1b[31mError:\x1b[0m ${message}\r\n`);
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
document.getElementById("wizard-create").onclick = () => send("new.confirm");
document.getElementById("root-none").onclick = () => chooseRoot("");
document.getElementById("root-start").onclick = () => chooseRoot(state.dirRoot || "");
els.termReturn.onclick = () => closeTerminalMode();
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
