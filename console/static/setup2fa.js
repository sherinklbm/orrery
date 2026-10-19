// First sign-in: set up the authenticator app (TOTP), then show the one-time recovery codes.
var csrf = "";
var codes = [];
function $(id) { return document.getElementById(id); }

async function call(path, body) {
  var opts = { headers: {} };
  if (body) { opts.method = "POST"; opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; opts.headers["X-CSRF"] = csrf; }
  var res = await fetch(path, opts);
  if (res.status === 401) { window.location.href = "/login"; throw new Error("signed out"); }
  var data = await res.json().catch(function () { return {}; });
  if (!res.ok) throw new Error(data.error || "request failed");
  return data;
}

function say(text, bad) { var m = $("msg"); m.textContent = text; m.className = bad ? "err" : "ok"; }

async function start() {
  var s = await call("/api/session");
  csrf = s.csrf;
  if (s.two_factor && s.two_factor.passed) { window.location.href = "/app"; return; }
  var setup = await call("/api/2fa/setup");
  $("secret").textContent = setup.secret.replace(/(.{4})/g, "$1 ").trim();
  try {
    var qr = qrcode(0, "M");
    qr.addData(setup.uri);
    qr.make();
    var img = document.createElement("img");
    img.src = qr.createDataURL(5, 4);
    img.alt = "QR code";
    $("qr").replaceChildren(img);
  } catch (e) { $("qr").textContent = "The QR code could not be drawn; use the key below."; }
  $("code").focus();
}

$("enable").addEventListener("click", async function () {
  var btn = $("enable");
  btn.disabled = true;
  try {
    var out = await call("/api/2fa/enable", { code: $("code").value.trim() });
    codes = out.recovery_codes;
    $("codes").textContent = codes.join("\n");
    $("step1").classList.add("hidden");
    $("step2").classList.remove("hidden");
  } catch (e) { say(e.message, true); }
  btn.disabled = false;
});
$("code").addEventListener("keydown", function (e) { if (e.key === "Enter") $("enable").click(); });
$("saved").addEventListener("change", function () { $("continue").disabled = !$("saved").checked; });
$("continue").addEventListener("click", function () { window.location.href = "/app"; });
$("copy").addEventListener("click", function () {
  if (navigator.clipboard) navigator.clipboard.writeText(codes.join("\n")).then(function () { $("copy").textContent = "Copied"; });
});
$("download").addEventListener("click", function () {
  var a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["Orrery recovery codes (each works once)\n\n" + codes.join("\n") + "\n"], { type: "text/plain" }));
  a.download = "orrery-recovery-codes.txt";
  a.click();
});
$("logout").addEventListener("click", async function () {
  try { await call("/api/logout", {}); } catch (e) { /* already signed out */ }
  window.location.href = "/login";
});
start().catch(function (e) { say("Could not start the set-up: " + e.message, true); });
