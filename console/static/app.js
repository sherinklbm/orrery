var csrf = "";
var n8nUrl = "";
var lastStatus = null;
var TABS = ["overview", "ai", "connect", "hermes"];

function $(id) { return document.getElementById(id); }

function el(tag, text, cls) {
  var e = document.createElement(tag);
  if (text != null) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}

async function api(path, body) {
  var opts = { headers: {} };
  if (body) {
    opts.method = "POST";
    opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-CSRF"] = csrf;
  }
  var res = await fetch(path, opts);
  if (res.status === 401) { window.location.href = "/login"; throw new Error("signed out"); }
  var data = await res.json();
  if (res.status === 403 && data.setup_2fa) { window.location.href = "/setup-2fa"; throw new Error("two-factor set-up needed"); }
  if (!res.ok) throw new Error(data.error || "request failed");
  return data;
}

function say(id, text, bad) {
  var box = $(id);
  box.textContent = text;
  box.className = bad ? "err" : "ok";
}

async function busy(btn, fn) {
  btn.disabled = true;
  try { return await fn(); } finally { btn.disabled = false; }
}

function showTab(name) {
  document.querySelectorAll(".tab").forEach(function (b) { b.classList.toggle("active", b.dataset.tab === name); });
  TABS.forEach(function (t) { $("tab-" + t).classList.toggle("hidden", t !== name); });
  if (name === "overview") loadStatus();
  if (name === "ai") loadAI();
  if (name === "connect") loadStatus();
  if (name === "hermes") loadHermes();
}

document.querySelectorAll(".tab").forEach(function (btn) {
  btn.addEventListener("click", function () { showTab(btn.dataset.tab); });
});

$("logout").addEventListener("click", async function () {
  try { await api("/api/logout", {}); } catch (e) { /* already signed out */ }
  window.location.href = "/login";
});

/* ---- overview ---- */
function dot(kind) { return el("span", "", "dot " + kind); }

function row(label, value, kind) {
  var tr = document.createElement("tr");
  var a = el("td", label, "cell-left");
  var b = document.createElement("td");
  if (kind) b.append(dot(kind), document.createTextNode(" "));
  b.append(document.createTextNode(value));
  tr.append(a, b);
  return tr;
}

function hermesUsesGateway(h) {
  return !!(h && h.model && h.model.provider === "custom" && /127\.0\.0\.1:8081/.test(h.model.base_url || "") && h.env && h.env.OPENAI_API_KEY);
}

function hermesAccess(h) {
  if (!h.access) return "unknown";
  if (h.access.sudo) return "full (sudo" + (h.access.docker ? " + docker" : "") + ")";
  return "sandboxed (its own user only)";
}

function renderStatus(s) {
  lastStatus = s;
  var h = s.hermes, n = s.n8n, ai = s.ai;
  var items = [
    { done: !!ai.active, text: "Choose an AI provider and paste its API key", tab: "ai" },
    { done: !!(n.api_key_set && n.key_ok), text: "Paste your n8n API key", tab: "connect" },
    { done: !!(n.connected && n.workflow_on !== false), text: "Connect Hermes and n8n", tab: "connect" },
    { done: !!n.ai_credential, text: "Let n8n use an AI provider (optional)", tab: "connect" },
    { done: hermesUsesGateway(h), text: "Point Hermes at your AI provider", tab: "hermes" },
    { done: !!(h.skills && h.skills.length), text: "Install Hermes's skills and tools", tab: "hermes" }
  ];
  var list = $("checklist");
  list.replaceChildren.apply(list, items.map(function (it) {
    var li = el("li", null, it.done ? "done" : "todo");
    var a = el("a", it.text);
    a.href = "#";
    a.addEventListener("click", function (e) { e.preventDefault(); showTab(it.tab); });
    li.append(dot(it.done ? "good" : "warn"), document.createTextNode(" "), a);
    return li;
  }));
  var left = items.filter(function (i) { return !i.done && i.text.indexOf("optional") < 0; }).length;
  $("overviewNote").textContent = left ? left + " step" + (left > 1 ? "s" : "") + " left." : "Everything is set up. Change the AI provider any time on the AI tab.";

  var t = $("statusTable");
  var rows = [
    row("AI provider for Hermes", ai.active ? ai.name + " \u00b7 " + ai.model + backupNote(ai.hermes_backups) : "none selected", ai.active ? "good" : "warn"),
    row("AI provider for n8n", (ai.n8n_same ? (ai.active ? "same as Hermes" : "none selected") : (ai.n8n_name + " \u00b7 " + ai.n8n_model)) + (ai.active ? backupNote(ai.n8n_backups) : ""), ai.n8n_same ? (ai.active ? "good" : "warn") : "good"),
    row("AI gateway", ai.gate_seen ? "used " + ago(ai.gate_seen) : "not used yet", ai.gate_seen ? "good" : "warn"),
    row("n8n", n.reachable ? "running" : "not reachable", n.reachable ? "good" : "bad"),
    row("n8n API key", n.api_key_set ? (n.key_ok ? "accepted " + n.api_key_hint : "rejected by n8n") : "not set", n.api_key_set && n.key_ok ? "good" : "warn"),
    row("Hermes ↔ n8n link", n.connected ? (n.workflow_on === false ? "workflow is off" : "connected") : "not connected", n.connected && n.workflow_on !== false ? "good" : "warn"),
    row("Hermes helper", h.helper ? "running" : "not running", h.helper ? "good" : "bad"),
    row("Hermes installed", h.installed ? "yes" : "no", h.installed ? "good" : "warn")
  ];
  if (h.helper && h.installed) {
    rows.push(row("Hermes server access", hermesAccess(h), h.access && h.access.sudo ? "warn" : "good"));
    rows.push(row("Hermes Telegram service", h.gateway_service || "unknown", h.gateway_service === "active" ? "good" : "warn"));
    rows.push(row("Hermes AI provider", hermesUsesGateway(h) ? "the AI gateway" : (h.model && h.model.provider ? h.model.provider : "unknown"), hermesUsesGateway(h) ? "good" : "warn"));
  }
  if (twoFactor && twoFactor.required) {
    rows.push(row("Two-factor sign-in", twoFactor.enabled ? "on (" + twoFactor.recovery_left + " recovery codes left)" : "off",
      twoFactor.enabled && twoFactor.recovery_left > 2 ? "good" : "warn"));
  }
  t.replaceChildren.apply(t, rows);
  renderConnect(n);
  renderN8nLists(n);
}

function backupNote(n) { return n ? " (+" + n + " backup" + (n > 1 ? "s" : "") + ")" : ""; }

function ago(iso) {
  var secs = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (secs < 90) return secs + " seconds ago";
  if (secs < 5400) return Math.round(secs / 60) + " minutes ago";
  return Math.round(secs / 3600) + " hours ago";
}

async function loadStatus() {
  try { renderStatus(await api("/api/status")); } catch (e) { $("overviewNote").textContent = "Could not load status: " + e.message; }
}

$("refresh").addEventListener("click", function () { busy($("refresh"), loadStatus); });

/* ---- connect ---- */
function renderConnect(n) {
  $("n8nKeyMsg").className = "ok";
  if (n.api_key_set && !$("n8nKeyMsg").textContent) {
    $("n8nKeyMsg").textContent = n.key_ok ? "A key is saved (" + n.api_key_hint + ") and n8n accepts it." : "A key is saved (" + n.api_key_hint + ") but n8n does not accept it now.";
  }
  var a = $("openN8n");
  if (n8nUrl) { a.href = n8nUrl; a.classList.remove("hidden"); }
  $("n8nKey").placeholder = n.api_key_set ? "Saved (" + n.api_key_hint + "). Paste a new one to replace it." : "Paste the n8n API key";
}

$("n8nSave").addEventListener("click", function () {
  busy($("n8nSave"), async function () {
    try {
      var out = await api("/api/n8n/key", { api_key: $("n8nKey").value.trim() });
      $("n8nKey").value = "";
      say("n8nKeyMsg", "Key accepted (" + out.api_key_hint + ").", false);
      loadStatus();
    } catch (e) { say("n8nKeyMsg", e.message, true); }
  });
});

$("n8nClear").addEventListener("click", function () {
  if (!window.confirm("Remove the saved n8n API key? The existing link keeps working.")) return;
  busy($("n8nClear"), async function () {
    try { await api("/api/n8n/key", { clear: true }); say("n8nKeyMsg", "Removed.", false); loadStatus(); }
    catch (e) { say("n8nKeyMsg", e.message, true); }
  });
});

$("connect").addEventListener("click", function () {
  busy($("connect"), async function () {
    var list = $("connectSteps");
    list.replaceChildren();
    say("connectMsg", "Working…", false);
    try {
      var out = await api("/api/connect", { rotate: $("rotate").checked });
      list.replaceChildren.apply(list, out.steps.map(function (s) {
        var li = el("li", null, s.ok ? "done" : "failed");
        li.append(dot(s.ok ? "good" : "bad"), document.createTextNode(" " + s.step + (s.detail ? " — " + s.detail : "")));
        return li;
      }));
      say("connectMsg", out.ok ? "Connected. Hermes is restarting to load the new tool; give it about a minute before using it." : "Not finished. Fix the step marked red and press Connect again.", !out.ok);
      $("rotate").checked = false;
      loadStatus();
    } catch (e) { say("connectMsg", e.message, true); }
  });
});

function renderN8nLists(n) {
  var list = $("n8nMcpList");
  list.replaceChildren.apply(list, (n.mcp_connections && n.mcp_connections.length) ? n.mcp_connections.map(function (c) {
    var li = el("li", null, "");
    li.append(el("b", c.name), document.createTextNode(" \u2014 " + c.url + (c.auth ? " (token saved in n8n)" : "")), document.createTextNode(" "));
    var rm = el("button", "Remove", "btn btn-ghost btn-sm");
    rm.type = "button";
    rm.addEventListener("click", function () {
      if (!window.confirm("Remove \u201c" + c.name + "\u201d and its chat agent from n8n?")) return;
      busy(rm, async function () {
        try { await api("/api/n8n/mcp/remove", { name: c.name }); say("n8nMcpMsg", "Removed.", false); loadStatus(); }
        catch (e) { say("n8nMcpMsg", e.message, true); }
      });
    });
    li.append(rm);
    return li;
  }) : [el("li", "None yet.")]);
  var hl = $("hMcpList");
  hl.replaceChildren.apply(hl, (n.hermes_remotes || []).map(function (r) {
    return el("li", r.name + " \u2014 " + r.url + (r.auth ? " (token saved)" : ""));
  }));
}

$("n8nAi").addEventListener("click", function () {
  busy($("n8nAi"), async function () {
    say("n8nAiMsg", "Working\u2026", false);
    try { var out = await api("/api/n8n/ai", {}); say("n8nAiMsg", out.message, !out.ok); loadStatus(); }
    catch (e) { say("n8nAiMsg", e.message, true); }
  });
});

$("n8nMcpAdd").addEventListener("click", function () {
  busy($("n8nMcpAdd"), async function () {
    var list = $("n8nMcpSteps");
    list.replaceChildren();
    say("n8nMcpMsg", "Working\u2026", false);
    try {
      var out = await api("/api/n8n/mcp", { name: $("n8nMcpName").value.trim(), url: $("n8nMcpUrl").value.trim(), token: $("n8nMcpToken").value.trim() || null });
      list.replaceChildren.apply(list, out.steps.map(function (s) {
        var li = el("li", null, s.ok ? "done" : "failed");
        li.append(dot(s.ok ? "good" : "bad"), document.createTextNode(" " + s.step + (s.detail ? " \u2014 " + s.detail : "")));
        return li;
      }));
      $("n8nMcpToken").value = "";
      say("n8nMcpMsg", out.ok ? "Done. Open n8n, find the new chat agent and press Chat." : "Not finished: fix the red step and try again.", !out.ok);
      loadStatus();
    } catch (e) { say("n8nMcpMsg", e.message, true); }
  });
});

/* ---- hermes ---- */
var catalog = [];

async function loadHermes() {
  await loadStatus();
  var h = lastStatus && lastStatus.hermes;
  var box = $("hermesStatus");
  var inst = $("hermesInstall");
  inst.classList.add("hidden");
  if (!h || !h.helper) {
    box.className = "callout warn";
    box.textContent = "The Hermes helper is not running on the server, so this tab can only show status. On the server (as root): bash /opt/orrery/deploy/enable-bridge.sh";
    return;
  }
  if (!h.installed) {
    box.className = "callout warn";
    box.textContent = "Hermes is not installed yet.";
    inst.classList.remove("hidden");
    inst.textContent = "On the server, as root: bash /opt/orrery/deploy/hermes-prep.sh, then follow the steps it prints (install as the hermes user, hermes setup terminal → local, hermes setup gateway for Telegram). Then come back here.";
    return;
  }
  var gw = hermesUsesGateway(h);
  box.className = "callout " + (gw ? "good" : "warn");
  box.textContent = gw ? "Hermes is installed and uses your AI provider through the gateway." : "Hermes is installed. It is not using the AI gateway yet: press “Use the AI gateway” below.";
  var t = $("hermesTable");
  t.replaceChildren.apply(t, [
    row("Telegram service", h.gateway_service || "unknown", h.gateway_service === "active" ? "good" : "warn"),
    row("Server access", hermesAccess(h), h.access && h.access.sudo ? "warn" : "good"),
    row("Provider in Hermes", (h.model && h.model.provider) || "unknown"),
    row("Telegram bot set", h.env && h.env.TELEGRAM_BOT_TOKEN ? "yes" : "no — run hermes setup gateway", h.env && h.env.TELEGRAM_BOT_TOKEN ? "good" : "warn"),
    row("Skills installed", (h.skills || []).length ? h.skills.join(", ") : "none yet"),
    row("Workspace folder", h.workspace ? "yes" : "no")
  ]);
  $("accessNote").textContent = h.access && h.access.sudo
    ? "Hermes has full server access. That also means it can read every key and secret on the server, including the AI keys saved here, and it can change SSH, the firewall and backups. Server access is only ever changed over SSH, never from this page."
    : "Hermes works inside its own user account. Server access is only ever changed over SSH, never from this page.";
  $("hermesRaw").textContent = ((h.mcp || "") + "\n\n" + (h.cron || "")).trim();
  if (!catalog.length) {
    try { catalog = (await api("/api/hermes/catalog")).servers; } catch (e) { catalog = []; }
    renderTools();
  }
}

function toolBox(s, checked) {
  var wrap = el("div", null, "check tool");
  var cb = document.createElement("input");
  cb.type = "checkbox"; cb.id = "tool-" + s.name; cb.value = s.name; cb.checked = checked;
  var label = el("label", null, "");
  label.htmlFor = cb.id;
  label.append(el("b", s.name), document.createTextNode(" — " + s.what), el("div", "Risk: " + s.risk, "meta"));
  wrap.append(cb, label);
  return wrap;
}

function renderTools() {
  var safe = $("toolsSafe"), power = $("toolsPower");
  safe.replaceChildren.apply(safe, catalog.filter(function (s) { return s.tier === 1; }).map(function (s) { return toolBox(s, true); }));
  power.replaceChildren.apply(power, catalog.filter(function (s) { return s.tier === 2; }).map(function (s) { return toolBox(s, false); }));
  var inst = document.getElementById("tool-n8n-instance");
  if (inst) inst.addEventListener("change", function () { $("instanceBox").classList.toggle("hidden", !inst.checked); });
}

$("useGateway").addEventListener("click", function () {
  busy($("useGateway"), async function () {
    say("gatewayMsg", "Working…", false);
    try {
      var out = await api("/api/hermes/use-gateway", {});
      say("gatewayMsg", out.ok ? "Done. Press Restart below so Telegram and scheduled jobs use it." : "Hermes refused a setting: " + (out.detail || ""), !out.ok);
      loadStatus();
    } catch (e) { say("gatewayMsg", e.message, true); }
  });
});

$("installSkills").addEventListener("click", function () {
  busy($("installSkills"), async function () {
    try {
      var out = await api("/api/hermes/skills", {});
      say("skillsMsg", "Installed: " + out.installed.join(", ") + ".", false);
      loadHermes();
    } catch (e) { say("skillsMsg", e.message, true); }
  });
});

$("addTools").addEventListener("click", function () {
  busy($("addTools"), async function () {
    var names = Array.prototype.map.call(document.querySelectorAll("#toolsSafe input:checked, #toolsPower input:checked"), function (c) { return c.value; });
    if (!names.length) { say("toolsMsg", "Tick at least one tool set.", true); return; }
    try {
      var out = await api("/api/hermes/mcp", { names: names, confirm: $("powerConfirm").checked, instance_token: $("instanceToken").value.trim() || null });
      $("instanceToken").value = "";
      say("toolsMsg", "Added: " + (out.added.join(", ") || "nothing new") + (out.kept.length ? ". Already there: " + out.kept.join(", ") : "") + (out.restarted ? ". Hermes is restarting to load them (about a minute)." : "."), false);
      loadHermes();
    } catch (e) { say("toolsMsg", e.message, true); }
  });
});

$("hMcpAdd").addEventListener("click", function () {
  busy($("hMcpAdd"), async function () {
    try {
      var out = await api("/api/hermes/mcp/remote", { name: $("hMcpName").value.trim(), url: $("hMcpUrl").value.trim(), token: $("hMcpToken").value.trim() || null });
      $("hMcpToken").value = "";
      say("hMcpMsg", (out.added.length ? "Added." : "Already there, kept as is.") + (out.restarted ? " Hermes is restarting to load it (about a minute)." : ""), false);
      loadStatus();
    } catch (e) { say("hMcpMsg", e.message, true); }
  });
});

$("restart").addEventListener("click", function () {
  busy($("restart"), async function () {
    try { var out = await api("/api/hermes/restart", {}); say("restartMsg", out.note, false); }
    catch (e) { say("restartMsg", e.message, true); }
  });
});

var twoFactor = null;
api("/api/session").then(function (s) { csrf = s.csrf; n8nUrl = s.n8n_url || ""; twoFactor = s.two_factor || null; showTab("overview"); }).catch(function () {});

/* ---- AI provider settings ---- */
var aiView = null;
var AI_CUSTOM = "custom";

function aiCatalog(id) {
  return aiView.providers.filter(function (p) { return p.id === id; })[0] || null;
}

function aiSelected() { return $("aiProvider").value; }

function aiFillForm() {
  var id = aiSelected();
  var cat = aiCatalog(id);
  var saved = aiView.entries[id] || null;
  var isNew = id === AI_CUSTOM;
  $("aiNameBox").classList.toggle("hidden", !(isNew || (saved && saved.custom)));
  $("aiName").value = saved && saved.custom ? saved.name : "";
  $("aiName").disabled = !!(saved && saved.custom);
  $("aiBase").value = saved ? saved.base_url : (cat ? cat.base_url : "");
  $("aiModel").value = saved ? saved.model : (cat && cat.models.length ? cat.models[0] : "");
  $("aiModelHermes").value = saved && saved.models ? saved.models.hermes || "" : "";
  $("aiModelN8n").value = saved && saved.models ? saved.models.n8n || "" : "";
  $("aiKey").value = "";
  $("aiKey").placeholder = saved && saved.key_set ? "Saved (" + saved.key_hint + "). Leave blank to keep it." : "Paste the API key";
  $("aiKeyNote").textContent = saved && saved.key_set ? "A key is saved for this provider. Paste a new one only to replace it." : "No key saved yet for this provider.";
  var list = $("aiModels");
  list.replaceChildren.apply(list, (cat ? cat.models : []).map(function (m) { var o = document.createElement("option"); o.value = m; return o; }));
  fillDatalist(aiSelected());
  var ml = $("aiModelsLink"), kl = $("aiKeyLink");
  $("aiLinks").classList.toggle("hidden", !cat);
  if (cat) { ml.href = cat.models_url; kl.href = cat.key_url; }
  $("aiRemove").disabled = !saved;
  $("aiTest").disabled = !(saved && saved.key_set);
}

function providerTag(v, id, saved) {
  var who = [];
  if (v.active === id) who.push("Hermes");
  if ((v.n8n_provider || v.active) === id) who.push("n8n");
  if (who.length) return " \u2014 used by " + who.join(" + ");
  return saved && saved.key_set ? " \u2014 key saved" : "";
}

function renderAI(v, keepSelection) {
  aiView = v;
  var sel = $("aiProvider");
  var current = keepSelection ? sel.value : (v.active || "openrouter");
  var opts = v.providers.map(function (p) {
    var saved = v.entries[p.id];
    var o = document.createElement("option");
    o.value = p.id;
    o.textContent = p.name + providerTag(v, p.id, saved);
    return o;
  });
  Object.keys(v.entries).forEach(function (id) {
    if (!v.entries[id].custom) return;
    var o = document.createElement("option");
    o.value = id;
    o.textContent = v.entries[id].name + " (custom)" + providerTag(v, id, v.entries[id]);
    opts.push(o);
  });
  var add = document.createElement("option");
  add.value = AI_CUSTOM;
  add.textContent = "+ Add another provider (custom)";
  opts.push(add);
  sel.replaceChildren.apply(sel, opts);
  sel.value = Array.prototype.some.call(sel.options, function (o) { return o.value === current; }) ? current : "openrouter";
  $("aiTokens").value = v.max_tokens;

  var act = v.active && v.entries[v.active];
  var status = $("aiStatus");
  status.className = "callout " + (act ? "good" : "warn");
  status.textContent = act
    ? "Hermes uses: " + act.name + " \u00b7 " + act.model
    : "Hermes has no provider yet. Its AI requests are refused until you save one.";
  var n8nEntry = v.n8n_provider && v.entries[v.n8n_provider];
  var n8nStatus = $("aiStatusN8n");
  n8nStatus.className = "callout " + ((n8nEntry || act) ? "good" : "warn");
  n8nStatus.textContent = n8nEntry
    ? "n8n uses its own provider: " + n8nEntry.name + " \u00b7 " + n8nEntry.model
    : (act ? "n8n uses the same provider as Hermes." : "n8n has no provider yet.");
  $("aiN8nFollow").classList.toggle("hidden", !n8nEntry);

  var gate = $("aiGate");
  if (v.gate_seen) {
    var secs = Math.max(0, Math.round((Date.now() - new Date(v.gate_seen).getTime()) / 1000));
    gate.textContent = "The gateway last picked up these settings " + (secs < 90 ? secs + " seconds" : Math.round(secs / 60) + " minutes") + " ago.";
  } else {
    gate.textContent = "The gateway has not read these settings yet. It does so with its first AI request, within about 15 seconds of a change.";
  }
  var hist = $("aiHistory");
  hist.replaceChildren.apply(hist, v.history.length ? v.history.map(function (h) {
    return el("li", new Date(h.at).toLocaleString() + " \u2014 " + h.event);
  }) : [el("li", "Nothing yet.")]);
  aiFillForm();
  renderOrder();
}

async function loadAI() {
  try { renderAI(await api("/api/ai"), aiView !== null); } catch (e) { $("aiSaved").textContent = "Could not load: " + e.message; }
}

function aiBody(activate) {
  var id = aiSelected();
  var body = {
    provider: id, base_url: $("aiBase").value.trim(), model: $("aiModel").value.trim(),
    model_hermes: $("aiModelHermes").value.trim(), model_n8n: $("aiModelN8n").value.trim(),
    max_tokens: parseInt($("aiTokens").value, 10) || null, activate: activate, target: $("aiTarget").value
  };
  if (id === AI_CUSTOM || (aiView.entries[id] && aiView.entries[id].custom)) body.name = $("aiName").value.trim();
  if ($("aiKey").value.trim()) body.api_key = $("aiKey").value.trim();
  return body;
}

function syncNote(v) {
  return v && v.hermes_synced === true ? " Hermes's own model setting was updated too; restart Hermes when convenient so it shows the new name." : "";
}

function aiMessage(text, bad) {
  var box = $("aiSaved");
  box.textContent = text;
  box.className = bad ? "err" : "ok";
}

$("aiProvider").addEventListener("change", aiFillForm);

$("aiN8nFollow").addEventListener("click", function () {
  busy($("aiN8nFollow"), async function () {
    try { renderAI(await api("/api/ai/n8n-follow", {}), true); aiMessage("n8n now uses the same provider as Hermes.", false); }
    catch (e) { aiMessage(e.message, true); }
  });
});

$("aiSave").addEventListener("click", async function () {
  try {
    var v = await api("/api/ai", aiBody(true));
    $("aiKey").value = "";
    var chosen = v.active;
    renderAI(v, false);
    if (chosen) $("aiProvider").value = chosen;
    aiFillForm();
    aiMessage("Saved. Whoever you chose will use this within about 15 seconds." + syncNote(v), false);
  } catch (e) { aiMessage("Could not save: " + e.message, true); }
});

$("aiTest").addEventListener("click", async function () {
  aiMessage("Testing\u2026", false);
  try {
    var r = await api("/api/ai/test", { provider: aiSelected() });
    aiMessage(r.ok ? "Works. The provider answered \"" + r.reply + "\" in " + r.ms + " ms (model " + r.model + ")." : "Test failed: " + r.message, !r.ok);
  } catch (e) { aiMessage("Test failed: " + e.message, true); }
});

$("aiRemove").addEventListener("click", async function () {
  var id = aiSelected();
  if (!window.confirm("Remove the saved key and settings for this provider?" + (aiView.active === id ? " It is in use, so AI requests will be refused until you pick another." : ""))) return;
  try {
    renderAI(await api("/api/ai/remove", { provider: id }), false);
    aiMessage("Removed.", false);
  } catch (e) { aiMessage("Could not remove: " + e.message, true); }
});


/* ---- model picker: every model a provider offers ---- */
var modelLists = {};   // provider id -> { models, error }
var picker = { provider: "", onPick: null, models: [] };

function money(v) { return v === 0 ? "free" : "$" + (v < 1 ? v.toFixed(3) : v.toFixed(2)); }

function modelLine(m) {
  var bits = [m.id];
  if (m.context) bits.push(m.context >= 1000 ? Math.round(m.context / 1000) + "k context" : m.context + " context");
  if (m.free) bits.push("free");
  else if (m.in != null && m.out != null) bits.push(money(m.in) + " in / " + money(m.out) + " out per 1M tokens");
  else bits.push("variable price");
  return bits.join(" · ");
}

function providerName(id) {
  if (aiView && aiView.entries[id]) return aiView.entries[id].name;
  var c = aiView && aiCatalog(id);
  return c ? c.name : id;
}

function fillDatalist(id) {
  var list = $("aiModels");
  var got = modelLists[id];
  list.replaceChildren.apply(list, ((got && got.models.length) ? got.models.slice(0, 600).map(function (m) { return m.id; })
    : ((aiCatalog(id) || { models: [] }).models)).map(function (m) { var o = document.createElement("option"); o.value = m; return o; }));
}

async function loadModels(id, refresh) {
  var out;
  try { out = await api("/api/ai/models?provider=" + encodeURIComponent(id) + (refresh ? "&refresh=1" : "")); }
  catch (e) { out = { models: [], error: e.message }; }
  modelLists[id] = { models: out.models || [], error: out.error || "" };
  return modelLists[id];
}

function pickerRender() {
  var words = $("pickerSearch").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  var vendor = $("pickerVendor").value;
  var freeOnly = $("pickerFree").checked;
  var hits = picker.models.filter(function (m) {
    if (vendor && m.id.split("/")[0] !== vendor) return false;
    if (freeOnly && !m.free) return false;
    var hay = (m.name + " " + m.id).toLowerCase();
    return words.every(function (w) { return hay.indexOf(w) >= 0; });
  });
  var shown = hits.slice(0, 80);
  $("pickerCount").textContent = picker.models.length
    ? hits.length + " of " + picker.models.length + " models" + (hits.length > shown.length ? " — showing the first " + shown.length + ". Type to narrow the list." : "")
    : "";
  var ul = $("pickerList");
  ul.replaceChildren.apply(ul, shown.map(function (m) {
    var li = document.createElement("li");
    var b = el("button", null, "picker-item");
    b.type = "button";
    b.append(el("b", m.name), el("span", modelLine(m), "meta"));
    b.addEventListener("click", function () { var pick = picker.onPick; closePicker(); if (pick) pick(m.id); });
    li.append(b);
    return li;
  }));
}

function pickerVendors() {
  var seen = {};
  picker.models.forEach(function (m) { if (m.id.indexOf("/") > 0) seen[m.id.split("/")[0]] = (seen[m.id.split("/")[0]] || 0) + 1; });
  var names = Object.keys(seen).sort();
  var sel = $("pickerVendor");
  var opts = [new Option("All makers", "")];
  names.forEach(function (n) { opts.push(new Option(n + " (" + seen[n] + ")", n)); });
  sel.replaceChildren.apply(sel, opts);
  sel.classList.toggle("hidden", names.length < 2);
}

async function pickerLoad(refresh) {
  $("pickerCount").textContent = "Loading the list…";
  $("pickerNote").textContent = "";
  $("pickerList").replaceChildren();
  var got = await loadModels(picker.provider, refresh);
  picker.models = got.models;
  $("pickerNote").textContent = got.error ? got.error + (got.models.length ? " Showing the last list we had." : " You can still type a model ID by hand.") : "";
  pickerVendors();
  pickerRender();
  if (picker.provider === aiSelected()) fillDatalist(picker.provider);
}

async function openPicker(provider, onPick) {
  if (!provider || provider === AI_CUSTOM) {
    aiMessage("Save this provider first (name, address, model and key). Then its models can be listed here.", true);
    return;
  }
  picker.provider = provider;
  picker.onPick = onPick;
  $("pickerTitle").textContent = "Choose a model — " + providerName(provider);
  $("pickerSearch").value = "";
  $("pickerFree").checked = false;
  $("picker").classList.remove("hidden");
  await pickerLoad(false);
  $("pickerSearch").focus();
}

function closePicker() { $("picker").classList.add("hidden"); }

$("pickerClose").addEventListener("click", closePicker);
$("pickerRefresh").addEventListener("click", function () { pickerLoad(true); });
$("pickerSearch").addEventListener("input", pickerRender);
$("pickerVendor").addEventListener("change", pickerRender);
$("pickerFree").addEventListener("change", pickerRender);
$("picker").addEventListener("click", function (e) { if (e.target === $("picker")) closePicker(); });
document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !$("picker").classList.contains("hidden")) closePicker(); });

function browseInto(input) {
  return function () { openPicker(aiSelected(), function (id) { $(input).value = id; }); };
}
$("aiBrowse").addEventListener("click", browseInto("aiModel"));
$("aiBrowseHermes").addEventListener("click", browseInto("aiModelHermes"));
$("aiBrowseN8n").addEventListener("click", browseInto("aiModelN8n"));
$("aiProvider").addEventListener("change", function () {
  var id = aiSelected();
  if (id !== AI_CUSTOM && !modelLists[id]) loadModels(id, false).then(function () { if (aiSelected() === id) fillDatalist(id); });
});

/* ---- model order: the main model and its backups, for Hermes and for n8n ---- */
function ordinal(i) { return ["Main", "2nd", "3rd", "4th", "5th", "6th"][i] || (i + 1) + "th"; }

function mainFor(client) {
  var pid = client === "n8n" && aiView.n8n_provider ? aiView.n8n_provider : aiView.active;
  var e = pid && aiView.entries[pid];
  return e ? { name: e.name, model: (e.models && e.models[client]) || e.model } : null;
}

function n8nOwnList() { return aiView.fallbacks.n8n !== null; }

function backupsFor(client) { return client === "n8n" && !n8nOwnList() ? aiView.fallbacks.hermes : aiView.fallbacks[client]; }

async function saveBackups(client, list, doneText) {
  try {
    var v = await api("/api/ai/chain", { client: client, backups: list === null ? null : list.map(function (b) { return { provider: b.provider, model: b.model }; }) });
    renderAI(v, true);
    say("bkMsg", (doneText || "Saved.") + " The gateway uses the new order within about 15 seconds." + syncNote(v), false);
  } catch (e) { say("bkMsg", "Could not save: " + e.message, true); }
}

function actButton(text, title, fn) {
  var b = el("button", text, "btn btn-ghost btn-sm");
  b.type = "button";
  b.title = title;
  b.addEventListener("click", function () { busy(b, fn); });
  return b;
}

function renderOrderList(client) {
  var ol = $(client === "hermes" ? "orderHermes" : "orderN8n");
  var main = mainFor(client);
  var items = backupsFor(client);
  var own = client === "hermes" || n8nOwnList();
  var rows = [];
  var first = el("li", null, "order-row");
  first.append(el("span", "Main", "order-tag"), el("span", main ? main.name + " · " + main.model : "Not chosen yet. Save a provider and model above.", "order-text"));
  rows.push(first);
  items.forEach(function (b, i) {
    var li = el("li", null, "order-row");
    li.append(el("span", ordinal(i + 1), "order-tag"), el("span", b.name + " · " + b.model, "order-text"));
    if (own) {
      var acts = el("span", null, "order-acts");
      var move = function (from, to) {
        return function () { var l = items.slice(); l.splice(to, 0, l.splice(from, 1)[0]); return saveBackups(client, l, "Order changed."); };
      };
      if (i > 0) acts.append(actButton("↑", "Move up", move(i, i - 1)));
      if (i < items.length - 1) acts.append(actButton("↓", "Move down", move(i, i + 1)));
      acts.append(actButton("Test", "Send one tiny request to this model", async function () {
        say("bkMsg", "Testing " + b.model + "…", false);
        try {
          var r = await api("/api/ai/test", { provider: b.provider, model: b.model });
          say("bkMsg", r.ok ? b.model + " works (" + r.ms + " ms)." : b.model + " failed: " + r.message, !r.ok);
        } catch (e) { say("bkMsg", "Test failed: " + e.message, true); }
      }));
      acts.append(actButton("Remove", "Remove this backup", function () {
        return saveBackups(client, items.filter(function (_, j) { return j !== i; }), "Removed.");
      }));
      li.append(acts);
    }
    rows.push(li);
  });
  if (!items.length) rows.push(el("li", own ? "No backups yet. If the main model fails, answers fail too." : "No backups (n8n uses Hermes's list, which is empty).", "order-note"));
  else if (!own) rows.push(el("li", "n8n uses the same backups as Hermes. Tick the box below to give n8n its own list.", "order-note"));
  ol.replaceChildren.apply(ol, rows);
}

function renderOrder() {
  if (!aiView || !aiView.fallbacks) return;
  renderOrderList("hermes");
  renderOrderList("n8n");
  $("n8nOwnOrder").checked = n8nOwnList();
  var keyed = Object.keys(aiView.entries).filter(function (id) { return aiView.entries[id].key_set; });
  var sel = $("bkProvider");
  var current = sel.value;
  sel.replaceChildren.apply(sel, keyed.length ? keyed.map(function (id) { return new Option(aiView.entries[id].name, id); })
    : [new Option("Save a provider and key above first", "")]);
  if (keyed.indexOf(current) >= 0) sel.value = current;
  $("bkAdd").disabled = !keyed.length;
  $("bkBrowse").disabled = !keyed.length;
}

$("n8nOwnOrder").addEventListener("change", function () {
  var box = $("n8nOwnOrder");
  box.disabled = true;
  var copy = aiView.fallbacks.hermes;
  saveBackups("n8n", box.checked ? copy : null, box.checked ? "n8n now has its own list (copied from Hermes's)." : "n8n now uses Hermes's list.").then(function () { box.disabled = false; });
});

$("bkBrowse").addEventListener("click", function () {
  openPicker($("bkProvider").value, function (id) { $("bkModel").value = id; });
});

$("bkAdd").addEventListener("click", function () {
  busy($("bkAdd"), async function () {
    var provider = $("bkProvider").value, model = $("bkModel").value.trim();
    if (!provider || !model) { say("bkMsg", "Choose a provider and a model ID.", true); return; }
    var client = $("bkClient").value;
    var list = backupsFor(client).slice();
    list.push({ provider: provider, model: model });
    await saveBackups(client, list, "Added " + model + " as backup " + list.length + ".");
    $("bkModel").value = "";
  });
});
