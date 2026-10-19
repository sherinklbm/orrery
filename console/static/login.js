document.getElementById("form").addEventListener("submit", async function (event) {
  event.preventDefault();
  var error = document.getElementById("error");
  var button = document.getElementById("submit");
  var codeBox = document.getElementById("codeBox");
  var code = document.getElementById("code");
  error.textContent = "";
  button.disabled = true;
  try {
    var body = {
      username: document.getElementById("username").value,
      password: document.getElementById("password").value,
    };
    if (!codeBox.classList.contains("hidden") && code.value.trim()) body.code = code.value.trim();
    var res = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    var data = await res.json().catch(function () { return {}; });
    if (res.ok) { window.location.href = data.setup_2fa ? "/setup-2fa" : "/app"; return; }
    if (data.need_code) {
      var first = codeBox.classList.contains("hidden");
      codeBox.classList.remove("hidden");
      code.value = "";
      code.focus();
      error.textContent = first ? "" : data.error;
      if (first) document.getElementById("codeHint") || codeBox.insertAdjacentHTML("afterbegin", '<div class="muted" id="codeHint">Password accepted. Now enter the code from your authenticator app.</div>');
    } else {
      error.textContent = data.error || "Could not sign in.";
    }
  } catch (e) {
    error.textContent = "Network error. Please try again.";
  }
  button.disabled = false;
});
