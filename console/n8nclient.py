"""A small client for n8n's public REST API (X-N8N-API-KEY), plus the starter workflow Hermes connects to.

Standard library only. It never follows redirects and never puts the API key in an error message.
"""
import json
import urllib.error
import urllib.request


class N8NError(Exception):
    """Something went wrong talking to n8n. The message is safe to show."""

    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

_HINTS = {
    401: "n8n rejected the API key. Create a new one in n8n: Settings -> n8n API.",
    403: "n8n refused this request (the key may lack permission).",
    404: "n8n's API was not found there. Is n8n running, and is its public API enabled?",
    409: "n8n reported a conflict (a workflow or credential with that name may exist).",
}


class N8N:
    def __init__(self, base_url, api_key="", timeout=15):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout

    def _call(self, method, path, body=None):
        headers = {"Accept": "application/json"}
        if self.key:
            headers["X-N8N-API-KEY"] = self.key
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:
                raw = resp.read(2_000_000)
                status = resp.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(200_000), e.code
        except (urllib.error.URLError, TimeoutError, OSError):
            raise N8NError("n8n is not reachable (is it running?).")
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        return status, parsed

    def _ok(self, status, what):
        if not 200 <= status < 300:
            raise N8NError("%s failed: %s" % (what, _HINTS.get(status, "n8n answered with error %d." % status)), status)

    # ---- checks ----------------------------------------------------------------------
    def healthy(self):
        try:
            return self._call("GET", "/healthz")[0] == 200
        except N8NError:
            return False

    def check_key(self):
        """Returns (ok, message)."""
        try:
            status, _ = self._call("GET", "/api/v1/workflows?limit=1")
        except N8NError as e:
            return False, str(e)
        if 200 <= status < 300:
            return True, "The API key works."
        return False, _HINTS.get(status, "n8n answered with error %d." % status)

    # ---- credentials and workflows ---------------------------------------------------
    def create_credential(self, name, ctype, data):
        status, out = self._call("POST", "/api/v1/credentials", {"name": name, "type": ctype, "data": data})
        self._ok(status, "Creating the credential")
        return str(out.get("id", ""))

    def delete_credential(self, cid):
        if cid:
            try:
                self._call("DELETE", "/api/v1/credentials/" + str(cid))
            except N8NError:
                pass

    def create_workflow(self, workflow):
        status, out = self._call("POST", "/api/v1/workflows", workflow)
        self._ok(status, "Creating the workflow")
        return str(out.get("id", ""))

    def activate_workflow(self, wid):
        status, _ = self._call("POST", "/api/v1/workflows/%s/activate" % wid)
        self._ok(status, "Turning the workflow on")

    def delete_workflow(self, wid):
        """Switch the workflow off first: n8n keeps an active workflow's web address registered, so a new
        workflow on the same address would fail to turn on ("conflict")."""
        if wid:
            try:
                self._call("POST", "/api/v1/workflows/%s/deactivate" % wid)
            except N8NError:
                pass
            try:
                self._call("DELETE", "/api/v1/workflows/" + str(wid))
            except N8NError:
                pass

    def workflow_active(self, wid):
        status, out = self._call("GET", "/api/v1/workflows/" + str(wid))
        if status == 404:
            return None
        self._ok(status, "Reading the workflow")
        return bool(out.get("active"))


def mcp_workflow(path, cred_id, cred_name, name="Hermes tools (Orrery)"):
    """The workflow Hermes connects to: an MCP Server Trigger protected by a bearer credential, with one harmless tool.
    Add more tools to it in n8n (or ask Hermes to, once you enable the n8n builder)."""
    trigger = "MCP Server Trigger"
    return {
        "name": name,
        "nodes": [
            {"parameters": {"path": path, "authentication": "bearerAuth"},
             "type": "@n8n/n8n-nodes-langchain.mcpTrigger", "typeVersion": 1, "position": [0, 0],
             "name": trigger, "webhookId": path,
             "credentials": {"httpBearerAuth": {"id": cred_id, "name": cred_name}}},
            {"parameters": {}, "type": "@n8n/n8n-nodes-langchain.toolCalculator", "typeVersion": 1,
             "position": [0, 260], "name": "Calculator"},
        ],
        "connections": {"Calculator": {"ai_tool": [[{"node": trigger, "type": "ai_tool", "index": 0}]]}},
        "settings": {"executionOrder": "v1"},
    }


def agent_workflow(title, mcp_url, mcp_cred=None, ai_cred=None):
    """A ready chat agent for the n8n editor: Chat Trigger -> AI Agent, with the AI gateway as its model and one MCP server as
    its tool. It is created switched OFF; open it in n8n and use the Chat button to try it. mcp_cred / ai_cred are (id, name)."""
    chat, agent, model, tool = "When chat message received", "AI Agent", "AI model (Orrery gateway)", "MCP: " + title
    nodes = [
        {"parameters": {"options": {}}, "type": "@n8n/n8n-nodes-langchain.chatTrigger", "typeVersion": 1.1,
         "position": [0, 0], "name": chat, "webhookId": "chat-" + title.lower().replace(" ", "-")[:30]},
        {"parameters": {"options": {}}, "type": "@n8n/n8n-nodes-langchain.agent", "typeVersion": 1.7,
         "position": [300, 0], "name": agent},
        {"parameters": {"model": {"__rl": True, "mode": "id", "value": "gateway"}, "options": {}},
         "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi", "typeVersion": 1.2, "position": [220, 260], "name": model,
         **({"credentials": {"openAiApi": {"id": ai_cred[0], "name": ai_cred[1]}}} if ai_cred else {})},
        {"parameters": {"endpointUrl": mcp_url, "authentication": "bearerAuth" if mcp_cred else "none"},
         "type": "@n8n/n8n-nodes-langchain.mcpClientTool", "typeVersion": 1.1, "position": [420, 260], "name": tool,
         **({"credentials": {"httpBearerAuth": {"id": mcp_cred[0], "name": mcp_cred[1]}}} if mcp_cred else {})},
    ]
    return {
        "name": "Chat with MCP: %s (Orrery)" % title,
        "nodes": nodes,
        "connections": {
            chat: {"main": [[{"node": agent, "type": "main", "index": 0}]]},
            model: {"ai_languageModel": [[{"node": agent, "type": "ai_languageModel", "index": 0}]]},
            tool: {"ai_tool": [[{"node": agent, "type": "ai_tool", "index": 0}]]},
        },
        "settings": {"executionOrder": "v1"},
    }
